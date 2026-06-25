import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist, TransformStamped
from nav_msgs.msg import Odometry
from sensor_msgs.msg import Range
from tf2_ros import TransformBroadcaster
import serial
import math
import time

WHEEL_RADIUS    = 0.0762
WHEEL_BASE      = 0.41
POLE_PAIRS      = 10
TICKS_PER_REV   = POLE_PAIRS * 6   # 60 ticks/rev (10 pole pairs ? 6 Hall edges)
METERS_PER_TICK = (2 * math.pi * WHEEL_RADIUS) / TICKS_PER_REV

# IMU complementary filter ? how much to trust IMU gyro vs wheel odometry for angle
# 0.0 = 100% wheels,  1.0 = 100% IMU,  0.95 = recommended
IMU_ALPHA = 0.95

# Velocity limits
VMAX   = 0.40    # m/s ? cap wheel speed to match nav2 vx_max
V_DEAD = 0.02    # m/s ? below this send 0 RPM (stops motor)


class SerialBridge(Node):
    def __init__(self):
        super().__init__('serial_bridge')

        self.declare_parameter('port', '/dev/ttyUSB1')
        self.declare_parameter('baud', 115200)
        self.declare_parameter('forward_only', False)
        self.declare_parameter('left_tick_scale', 1.0)
        self.declare_parameter('disable_tank_turns', False)
        self.declare_parameter('imu_flip_z', False)
        port = self.get_parameter('port').value
        baud = self.get_parameter('baud').value
        self.forward_only       = self.get_parameter('forward_only').value
        self.left_tick_scale    = self.get_parameter('left_tick_scale').value
        self.disable_tank_turns = self.get_parameter('disable_tank_turns').value
        self.imu_flip_z         = self.get_parameter('imu_flip_z').value

        if self.forward_only:
            self.get_logger().info('forward_only=true: reverse commands blocked')
        if self.left_tick_scale != 1.0:
            self.get_logger().info(f'left_tick_scale={self.left_tick_scale:.3f} (odometry only)')
        if self.disable_tank_turns:
            self.get_logger().info('disable_tank_turns=true')

        try:
            self.ser = serial.Serial(port, baud, timeout=0.05)
            time.sleep(2.0)
            self.ser.reset_input_buffer()
            self.get_logger().info(f'Connected to ESP32 on {port}')
        except serial.SerialException as e:
            self.get_logger().error(f'Cannot open {port}: {e}')
            raise

        self.odom_pub       = self.create_publisher(Odometry, '/odom', 10)
        self.tf_broadcaster = TransformBroadcaster(self)
        self.cmd_sub        = self.create_subscription(
            Twist, '/cmd_vel', self.cmd_cb, 10)

        # Ultrasonic Range publishers ? FL, FR, BL, BR
        _us_names = ['front_left', 'front_right', 'back_left', 'back_right']
        self._us_frame_ids = ['us_fl', 'us_fr', 'us_bl', 'us_br']
        self._us_pubs = [
            self.create_publisher(Range, f'/us/{n}', 10) for n in _us_names
        ]

        self.x          = 0.0
        self.y          = 0.0
        self.theta      = 0.0
        self.prev_left  = None
        self.prev_right = None
        self.last_time  = self.get_clock().now()
        self.last_cmd        = self.get_clock().now()
        self._last_watchdog_stop = False
        self._out_rpm_l = 0.0   # last RPM actually sent (used for direction-change ramp)
        self._out_rpm_r = 0.0
        self._RPM_RAMP  = 5.0   # RPM per cmd_cb call when stepping through zero

        self.create_timer(0.02, self.publish_tf)
        self.create_timer(0.01, self.read_serial)
        self.create_timer(0.10, self.watchdog)

        self.get_logger().info('SerialBridge ready (RPM mode).')

    def publish_tf(self):
        now = self.get_clock().now()
        qz  = math.sin(self.theta / 2.0)
        qw  = math.cos(self.theta / 2.0)
        tf = TransformStamped()
        tf.header.stamp    = now.to_msg()
        tf.header.frame_id = 'odom'
        tf.child_frame_id  = 'base_footprint'
        tf.transform.translation.x = self.x
        tf.transform.translation.y = self.y
        tf.transform.translation.z = 0.0
        tf.transform.rotation.x    = 0.0
        tf.transform.rotation.y    = 0.0
        tf.transform.rotation.z    = qz
        tf.transform.rotation.w    = qw
        self.tf_broadcaster.sendTransform(tf)

    def watchdog(self):
        elapsed = (self.get_clock().now() - self.last_cmd).nanoseconds / 1e9
        if elapsed > 1.0 and not self._last_watchdog_stop:
            try:
                self.ser.write(b'S\n')
                self.ser.flush()
                self._last_watchdog_stop = True
            except Exception:
                pass

    def _ramp_rpm(self, current: float, target: float) -> float:
        """Step RPM toward target; on direction flip, reset through zero first.
        Nav2 commands already smoothed by velocity_smoother arrive in tiny steps
        (< _RPM_RAMP) so they pass through unchanged. Only large jumps (direct
        teleop, or any sudden sign change) are slowed down ? matching backup."""
        if current * target < 0:   # direction change: treat current as zero
            current = 0.0
        diff = target - current
        if abs(diff) <= self._RPM_RAMP:
            return target
        return current + math.copysign(self._RPM_RAMP, diff)

    def _v_to_rpm(self, v: float) -> float:
        """Convert wheel velocity (m/s) to RPM. Returns 0.0 below V_DEAD."""
        if abs(v) < V_DEAD:
            return 0.0
        return v * 60.0 / (2.0 * math.pi * WHEEL_RADIUS)

    def cmd_cb(self, msg: Twist):
        self.last_cmd = self.get_clock().now()
        self._last_watchdog_stop = False
        lin = msg.linear.x
        ang = msg.angular.z
        self.get_logger().info(f'cmd_cb: lin={lin:.3f} ang={ang:.3f}')

        if self.forward_only and lin < 0.0:
            lin = 0.0

        if abs(lin) < 0.01 and abs(ang) < 0.01:
            rpm_l, rpm_r = 0.0, 0.0
        else:
            # Differential-drive kinematics
            v_l = lin - ang * (WHEEL_BASE / 2.0)
            v_r = lin + ang * (WHEEL_BASE / 2.0)

            if self.disable_tank_turns:
                if v_l * v_r < 0:
                    max_ang = abs(lin) * (WHEEL_BASE / 2.0) if abs(lin) > 0.05 else 0.0
                    if abs(ang) > max_ang and max_ang < 0.01:
                        ang = 0.0
                        v_l = lin
                        v_r = lin

            # Cap to VMAX ? scale both wheels proportionally if either exceeds limit
            peak = max(abs(v_l), abs(v_r))
            if peak > VMAX:
                v_l = v_l / peak * VMAX
                v_r = v_r / peak * VMAX

            rpm_l = self._v_to_rpm(v_l)
            rpm_r = self._v_to_rpm(v_r)

        # Ramp through zero on direction change so ESC accepts reverse
        rpm_l = self._ramp_rpm(self._out_rpm_l, rpm_l)
        rpm_r = self._ramp_rpm(self._out_rpm_r, rpm_r)
        self._out_rpm_l = rpm_l
        self._out_rpm_r = rpm_r

        try:
            cmd_str = f"V {rpm_l:.2f} {rpm_r:.2f}\n"
            self.get_logger().info(f'Sending: {cmd_str.strip()}')
            self.ser.write(cmd_str.encode())
            self.ser.flush()
        except serial.SerialException as e:
            self.get_logger().error(f'Write error: {e}')

    def read_serial(self):
        try:
            if self.ser.in_waiting > 400:
                self.ser.reset_input_buffer()
                return
            while self.ser.in_waiting:
                raw = self.ser.readline().decode(
                    'utf-8', errors='ignore').strip()
                if not raw:
                    break
                if raw.startswith('O '):
                    parts = raw.split()
                    if len(parts) == 4:
                        gz = float(parts[3])
                        if self.imu_flip_z:
                            gz = -gz
                        self.update_odom(int(parts[1]), int(parts[2]), gz)
                    elif len(parts) == 3:
                        self.update_odom(int(parts[1]), int(parts[2]))
                elif raw.startswith('U '):
                    self._publish_us(raw)
        except Exception as e:
            self.get_logger().warn(f'Read error: {e}')

    def _publish_us(self, raw: str):
        parts = raw.split()
        if len(parts) != 5:
            return
        now = self.get_clock().now().to_msg()
        for i in range(4):
            cm = int(parts[i + 1])
            msg = Range()
            msg.header.stamp = now
            msg.header.frame_id = self._us_frame_ids[i]
            msg.radiation_type = Range.ULTRASOUND
            msg.field_of_view = 0.26   # ~15� ? typical HC-SR04 cone
            msg.min_range = 0.02
            msg.max_range = 4.0
            # -1 from firmware means no echo / out of range ? publish +inf
            msg.range = (cm / 100.0) if cm > 0 else float('inf')
            self._us_pubs[i].publish(msg)

    def update_odom(self, left_ticks, right_ticks, gyro_z=None):
        if self.prev_left is None:
            self.prev_left  = left_ticks
            self.prev_right = right_ticks
            return

        dl = (left_ticks  - self.prev_left)  * METERS_PER_TICK * self.left_tick_scale
        dr = (right_ticks - self.prev_right) * METERS_PER_TICK
        self.prev_left  = left_ticks
        self.prev_right = right_ticks

        if abs(dl) > 0.10 or abs(dr) > 0.10:
            self.get_logger().warn(
                f'Implausible tick delta dl={dl:.3f} dr={dr:.3f} ? '
                'skipping (ESP32 reboot or serial glitch)')
            return

        d_center      = (dl + dr) / 2.0
        d_theta_wheel = (dr - dl) / WHEEL_BASE

        now = self.get_clock().now()
        dt  = (now - self.last_time).nanoseconds / 1e9
        self.last_time = now

        if gyro_z is not None and dt > 0:
            d_theta = IMU_ALPHA * (gyro_z * dt) + (1.0 - IMU_ALPHA) * d_theta_wheel
        else:
            d_theta = d_theta_wheel

        self.x     += d_center * math.cos(self.theta + d_theta / 2.0)
        self.y     += d_center * math.sin(self.theta + d_theta / 2.0)
        self.theta += d_theta

        v  = d_center / dt if dt > 0 else 0.0
        w  = d_theta  / dt if dt > 0 else 0.0
        qz = math.sin(self.theta / 2.0)
        qw = math.cos(self.theta / 2.0)

        odom = Odometry()
        odom.header.stamp            = now.to_msg()
        odom.header.frame_id         = 'odom'
        odom.child_frame_id          = 'base_footprint'
        odom.pose.pose.position.x    = self.x
        odom.pose.pose.position.y    = self.y
        odom.pose.pose.position.z    = 0.0
        odom.pose.pose.orientation.x = 0.0
        odom.pose.pose.orientation.y = 0.0
        odom.pose.pose.orientation.z = qz
        odom.pose.pose.orientation.w = qw
        odom.twist.twist.linear.x    = v
        odom.twist.twist.angular.z   = w
        odom.pose.covariance[0]      = 0.05
        odom.pose.covariance[7]      = 0.05
        odom.pose.covariance[35]     = 0.1
        odom.twist.covariance[0]     = 0.05
        odom.twist.covariance[35]    = 0.1
        self.odom_pub.publish(odom)


def main():
    rclpy.init()
    node = SerialBridge()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            node.ser.write(b"S\n")
            node.ser.flush()
            node.destroy_node()
        except Exception:
            pass
        rclpy.shutdown()


if __name__ == '__main__':
    main()
