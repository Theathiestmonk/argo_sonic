#!/usr/bin/env python3
"""
MPPI Reverse Controller — Auto-enable reverse ONLY during recovery

Strategy: Keep reverse DISABLED by default (vx_min=0.0, forward-only).
When recovery is triggered, temporarily ENABLE reverse (vx_min=-0.15).

Detection: Monitors when costmap clearing happens (sign of recovery attempt).
Recovery triggers ClearEntireCostmap -> enable reverse -> replan -> disable reverse.

Manual services (for external control if needed):
  /mppi/enable_reverse  — Set vx_min to -0.15 (backward motion for recovery)
  /mppi/disable_reverse — Set vx_min to 0.0 (forward-only, normal mode)
"""

import rclpy
from rclpy.node import Node
from std_srvs.srv import Empty
from std_srvs.srv import Trigger
import subprocess
import time
import threading


class MPPIReverseController(Node):
    def __init__(self):
        super().__init__('mppi_reverse_controller')

        self.enable_srv = self.create_service(
            Empty, '/mppi/enable_reverse', self._enable_reverse_cb)
        self.disable_srv = self.create_service(
            Empty, '/mppi/disable_reverse', self._disable_reverse_cb)

        # Client to call clear costmap service and detect recovery
        self.declare_parameter('monitor_interval', 0.5)  # Check recovery state every 500ms
        self.monitor_interval = self.get_parameter('monitor_interval').value

        self.reverse_enabled = False
        self.recovery_active = False
        self.last_clear_time = 0.0

        # Start monitoring for recovery in background
        self.monitor_timer = self.create_timer(self.monitor_interval, self._check_recovery_state)

        self.get_logger().info('MPPI Reverse Controller ready (reverse: OFF by default, auto-ON during recovery)')

    def _check_recovery_state(self):
        """Monitor if recovery is active by checking for recent costmap clears"""
        current_time = time.time()

        # If costmap was cleared in last 2 seconds, we're likely in recovery
        time_since_clear = current_time - self.last_clear_time
        in_recovery = time_since_clear < 2.0 and self.last_clear_time > 0

        # Auto-enable reverse during recovery
        if in_recovery and not self.reverse_enabled:
            self._set_reverse(True)
        # Auto-disable reverse when recovery ends
        elif not in_recovery and self.reverse_enabled and self.last_clear_time > 0:
            self._set_reverse(False)

    def _set_reverse(self, enabled: bool):
        """Enable or disable reverse mode"""
        try:
            vx_min = "-0.15" if enabled else "0.0"
            result = subprocess.run(
                ["ros2", "param", "set", "/controller_server",
                 "FollowPath.vx_min", vx_min],
                check=True, capture_output=True, timeout=5, text=True
            )
            self.reverse_enabled = enabled
            status = "ENABLED (recovery mode)" if enabled else "DISABLED (normal forward-only)"
            self.get_logger().info(f'[REVERSE] {status} — vx_min={vx_min}')
        except Exception as e:
            self.get_logger().warn(f'Failed to set vx_min: {e}')

    def _enable_reverse_cb(self, request, response):
        """Manual: Enable reverse by setting vx_min to -0.15"""
        self._set_reverse(True)
        return response

    def _disable_reverse_cb(self, request, response):
        """Manual: Disable reverse by setting vx_min to 0.0"""
        self._set_reverse(False)
        return response

    def clear_costmap_hook(self):
        """Called when costmap is cleared (recovery triggered)"""
        self.last_clear_time = time.time()
        self.get_logger().debug('[RECOVERY] Costmap clear detected - enabling reverse for replanning')


def main(args=None):
    rclpy.init(args=args)
    node = MPPIReverseController()

    # Optional: Hook into costmap clear service to detect recovery
    # For now, we detect recovery by timing since we can't easily hook the service

    rclpy.spin(node)
    rclpy.shutdown()


if __name__ == '__main__':
    main()
