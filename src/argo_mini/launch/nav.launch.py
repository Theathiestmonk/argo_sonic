"""
Argo Mini ? Navigation Launch (SLAM Toolbox localization)
==========================================================
Requires a posegraph map saved during a prior mapping session.
Pass the map base path (without extension) via the map:= argument:

    ros2 launch argo_mini nav.launch.py map:=/home/argo/maps/indoor_map

The robot will relocalize automatically from the first LiDAR scan ?
no manual initial pose needed.

Topic pipeline for velocity commands:
  Nav2 controller_server  ?  /cmd_vel_raw
  Nav2 velocity_smoother  ?  /cmd_vel_raw  ?  /cmd_vel_smoothed
  depth_safety_shield     ?  /cmd_vel_smoothed  ?  /cmd_vel
  serial_bridge           ?  /cmd_vel  ?  ESP32 motors

Depth-camera integration:
  HP60C SDK  ?  /ascamera_hp60c/camera_publisher/depth0/points
  depth_safety_shield:
    ? STOP / SLOW / CLEAR state machine on /cmd_vel_smoothed
    ? re-publishes /depth_filtered (base_link frame) for Nav2 local costmap

Args:
  map        (required) ? base path to .posegraph map (no extension)
  use_camera (default true)  ? launch the EAI HP60C camera node
  use_rviz   (default true)  ? launch RViz2
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, GroupAction, SetEnvironmentVariable, TimerAction
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import LifecycleNode, Node


def generate_launch_description():
    pkg = get_package_share_directory('argo_mini')
    nav2_yaml  = os.path.join(pkg, 'config', 'nav2.yaml')
    slam_yaml  = os.path.join(pkg, 'config', 'slam_toolbox.yaml')
    urdf_file  = os.path.join(pkg, 'urdf',   'argo_mini.urdf')

    # Default map path ? update after your first mapping session
    default_map = os.path.join(pkg, 'maps', 'indoor_map')

    # nav2.yaml hardcodes default_nav_to_pose_bt_xml/default_nav_through_poses_bt_xml
    # as an absolute /home/argo/my_project/argo_sonic/... path (plain YAML params
    # files can't read $ENV_VARS ? that's a launch-file-only substitution in
    # ROS2), so it breaks for any other clone location/username. Overriding it
    # here with get_package_share_directory, which nav2_yaml itself already
    # relies on, resolves correctly regardless of where this workspace lives.
    bt_xml_path = os.path.join(pkg, 'config', 'bt', 'navigate_to_pose.xml')

    with open(urdf_file, 'r') as f:
        robot_desc = f.read()

    use_camera = LaunchConfiguration('use_camera', default='true')
    use_rviz   = LaunchConfiguration('use_rviz',   default='true')
    map_path   = LaunchConfiguration('map',         default=default_map)

    # Nav2 lifecycle nodes ? slam_toolbox is NOT a lifecycle node; it manages itself
    # Order matches upstream nav2_bringup's navigation_launch.py: controller,
    # planner, and behavior are configured+activated before bt_navigator
    # (which holds action clients to all three), velocity_smoother last.
    nav2_nodes = [
        'controller_server',
        'planner_server',
        'behavior_server',
        'bt_navigator',
        'velocity_smoother',
    ]

    return LaunchDescription([
        # ?? Environment setup for camera SDK ????????????????????????????????
        SetEnvironmentVariable(
            'LD_LIBRARY_PATH',
            '/home/argo/EaiCameraSdk_v1.2.28.20241015/demo/linux_ros/ros2/ascamera/libs/lib/aarch64-linux-gnu:${LD_LIBRARY_PATH}'),

        # ?? launch arguments ????????????????????????????????????????????????
        DeclareLaunchArgument(
            'map',
            default_value=default_map,
            description='Base path to serialized posegraph map (no .posegraph extension)'),
        DeclareLaunchArgument(
            'use_camera', default_value='true',
            description='Launch the EAI HP60C depth camera node'),
        DeclareLaunchArgument(
            'use_rviz', default_value='true',
            description='Launch RViz2 for visualisation'),

        # ?? 1. Robot State Publisher (URDF ? TF tree) ???????????????????????
        Node(
            package='robot_state_publisher',
            executable='robot_state_publisher',
            name='robot_state_publisher',
            output='screen',
            parameters=[{
                'robot_description': robot_desc,
                'use_sim_time': False,
            }],
        ),

        # ?? 2. Serial Bridge (ESP32 motor control + wheel odometry) ?????????
        Node(
            package='argo_mini',
            executable='serial_bridge',
            name='serial_bridge',
            output='screen',
            parameters=[{
                'port':            '/dev/esp32',
                'baud':            115200,
                'left_tick_scale': 2.1714,
            }],
        ),

        # ?? 3. RPLidar A1 ????????????????????????????????????????????????????
        # frame_id = lidar_link matches the URDF joint child frame so that
        # robot_state_publisher provides the base_link ? lidar_link TF that
        # SLAM Toolbox needs for scan-matching.
        Node(
            package='rplidar_ros',
            executable='rplidar_composition',
            name='rplidar',
            output='screen',
            parameters=[{
                'serial_port':      '/dev/lidar',
                'serial_baudrate':  115200,
                'frame_id':         'lidar_link',
                'inverted':         False,
                'angle_compensate': True,
                'scan_mode':        'Standard',
            }],
        ),

        # ?? 4. Scan Relay (LiDAR timestamp correction) ???????????????????????
        Node(
            package='argo_mini',
            executable='scan_relay',
            name='scan_relay',
            output='screen',
        ),

        # ?? 5. SLAM Toolbox ? localization mode ??????????????????????????????
        # Replaces both map_server and amcl:
        #   ? serves /map from the serialized posegraph
        #   ? broadcasts map ? odom TF via scan-matching (no initial pose needed)
        # map_file_name is overridden here so the launch map:= argument takes effect.
        Node(
            package='slam_toolbox',
            executable='localization_slam_toolbox_node',
            name='slam_toolbox',
            output='screen',
            parameters=[
                slam_yaml,
                {'map_file_name': map_path},
            ],
        ),

        # ?? 5.5. Pose Initializer (Auto-set kitchen pose) ?????????????????????
        # Reads kitchen location from office_map.json and initializes robot pose
        # Runs once at startup, then exits cleanly.
        # Delayed so slam_toolbox is up and serving the pose-set service it
        # depends on (avoids a startup race on slower Jetson boots).
        TimerAction(
            period=5.0,
            actions=[
                Node(
                    package='argo_mini',
                    executable='pose_init',
                    name='pose_init',
                    output='screen',
                ),
            ],
        ),

        # ?? 6-11. Nav2 Lifecycle Nodes + Lifecycle Manager ??????????????????
        # Delayed ~6s so sensor/localization nodes (lidar, serial bridge,
        # slam_toolbox, camera) get a head start. Launching everything at
        # once on the Jetson caused some nodes to miss the lifecycle
        # manager's bond_timeout window under startup CPU contention, which
        # triggers a hard reset of ALL 5 managed nodes ? that's why nav only
        # came up every other launch instead of every time.
        # Node order matches upstream nav2_bringup (see nav2_nodes above).
        TimerAction(
            period=6.0,
            actions=[
                # Controller Server ? /cmd_vel_raw (remapped)
                LifecycleNode(
                    package='nav2_controller',
                    executable='controller_server',
                    name='controller_server',
                    namespace='',
                    output='screen',
                    parameters=[nav2_yaml],
                    remappings=[('cmd_vel', '/cmd_vel_raw')],
                ),

                # Planner Server
                LifecycleNode(
                    package='nav2_planner',
                    executable='planner_server',
                    name='planner_server',
                    namespace='',
                    output='screen',
                    parameters=[nav2_yaml],
                ),

                # Behavior Server (Spin / BackUp / Wait recoveries)
                LifecycleNode(
                    package='nav2_behaviors',
                    executable='behavior_server',
                    name='behavior_server',
                    namespace='',
                    output='screen',
                    parameters=[nav2_yaml],
                    remappings=[('cmd_vel', '/cmd_vel_raw')],
                ),

                # BT Navigator ? bt_xml_path override (see above) takes
                # precedence over nav2.yaml's hardcoded absolute path.
                LifecycleNode(
                    package='nav2_bt_navigator',
                    executable='bt_navigator',
                    name='bt_navigator',
                    namespace='',
                    output='screen',
                    parameters=[
                        nav2_yaml,
                        {
                            'default_nav_to_pose_bt_xml': bt_xml_path,
                            'default_nav_through_poses_bt_xml': bt_xml_path,
                        },
                    ],
                ),

                # Velocity Smoother  /cmd_vel_raw ? /cmd_vel_smoothed
                LifecycleNode(
                    package='nav2_velocity_smoother',
                    executable='velocity_smoother',
                    name='velocity_smoother',
                    namespace='',
                    output='screen',
                    parameters=[nav2_yaml],
                    remappings=[
                        ('cmd_vel',          '/cmd_vel_raw'),
                        ('cmd_vel_smoothed', '/cmd_vel_smoothed'),
                    ],
                ),

                # Nav2 Lifecycle Manager ? configures then activates all 5
                # nodes above together, in nav2_nodes order.
                Node(
                    package='nav2_lifecycle_manager',
                    executable='lifecycle_manager',
                    name='lifecycle_manager_nav',
                    output='screen',
                    parameters=[{
                        'use_sim_time':              False,
                        'autostart':                 True,
                        'node_names':                nav2_nodes,
                        # Raised from the 4.0s default ? on the Jetson,
                        # starting everything at once left too little
                        # margin and occasionally missed this, forcing a
                        # full reset of all 5 nodes.
                        'bond_timeout':              10.0,
                        'bond_respawn_max_duration': 15.0,
                    }],
                ),
            ],
        ),

        # ?? 12. Depth Safety Shield  /cmd_vel_smoothed ? /cmd_vel ????????????
        # Acts as the safety layer between Nav2's smoothed output and the motors.
        # Reads depth PointCloud2, stops/slows the robot if an obstacle is close,
        # and re-publishes a filtered cloud on /depth_filtered for the costmap.
        Node(
            package='argo_mini',
            executable='depth_safety_shield',
            name='depth_safety_shield',
            output='screen',
            parameters=[{
                'stop_distance':       0.35,
                'slow_distance':       0.65,
                'slow_factor':         0.40,
                'lateral_margin':      0.28,
                'min_obstacle_height': 0.05,
                'max_obstacle_height': 1.60,
                'depth_timeout':       3.0,
                'downsample_stride':   4,
                'input_topic':  '/cmd_vel_smoothed',
                'output_topic': '/cmd_vel',
                'depth_topic':
                    '/ascamera_hp60c/camera_publisher/depth0/points',
            }],
        ),

        # ?? 13. Camera static TF bridge ??????????????????????????????????????
        # HP60C SDK publishes depth0/points with frame_id: ascamera_hp60c_camera_link_0
        # Our URDF defines depth_camera_optical_frame at the same physical location.
        Node(
            package='tf2_ros',
            executable='static_transform_publisher',
            name='camera_tf_bridge',
            output='screen',
            arguments=[
                '--x', '0.0', '--y', '0.0', '--z', '0.0',
                '--roll', '0.0', '--pitch', '0.0', '--yaw', '0.0',
                '--frame-id', 'depth_camera_optical_frame',
                '--child-frame-id', 'ascamera_hp60c_camera_link_0',
            ],
        ),

        # ?? 14. HP60C Depth Camera (optional) ???????????????????????????????
        # Sources SDK setup.bash to ensure all camera libraries are available.
        # Publishes PointCloud2 on /ascamera_hp60c/camera_publisher/depth0/points
        GroupAction(
            condition=IfCondition(use_camera),
            actions=[
                Node(
                    package='ascamera',
                    executable='ascamera_node',
                    name='ascamera_hp60c',
                    output='screen',
                    shell=True,
                    prefix='bash -c "source /home/argo/EaiCameraSdk_v1.2.28.20241015/demo/linux_ros/ros2/install/setup.bash && exec "$0"" --',
                ),
            ],
        ),

        # ?? 15. RViz2 (optional) ?????????????????????????????????????????????
        Node(
            condition=IfCondition(use_rviz),
            package='rviz2',
            executable='rviz2',
            name='rviz2',
            output='screen',
        ),
    ])
