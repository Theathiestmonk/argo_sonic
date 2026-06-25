"""
Argo Mini — Mapping Session Launch
====================================
Run this ONCE to build the posegraph map of your venue.
Navigate every area the robot will ever need to reach.

Usage — manual teleoperation:
    ros2 launch argo_mini slam.launch.py

Usage — autonomous frontier exploration (no human required):
    ros2 launch argo_mini slam.launch.py use_frontier_exploration:=true

    The robot will automatically seek unexplored frontiers and map the entire
    reachable environment.  When it announces "EXPLORATION COMPLETE", save the
    map (see below).

Saving the map (two options):
  A) RViz plugin  →  "Serialize Map" button  →  enter output path
  B) CLI service:
       ros2 service call /slam_toolbox/serialize_map \\
           slam_toolbox/srv/SerializePoseGraph \\
           "{filename: '/home/argo/maps/indoor_map'}"

The service creates two files:
    indoor_map.posegraph
    indoor_map.data

Pass the same base path (without extension) to nav.launch.py:
    ros2 launch argo_mini nav.launch.py map:=/home/argo/maps/indoor_map

Do NOT use ros2 run nav2_map_server map_saver — that saves a flat PGM which
SLAM Toolbox localization cannot use.  Always use the serialize service.

Arguments:
  use_rviz                  (default true)  — launch RViz2
  use_frontier_exploration  (default false) — launch Nav2 + FrontierExplorer
                                              for autonomous mapping
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, GroupAction
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import LifecycleNode, Node


def generate_launch_description():
    pkg = get_package_share_directory('argo_mini')

    slam_mapping_yaml   = os.path.join(pkg, 'config', 'slam_mapping.yaml')
    nav2_yaml           = os.path.join(pkg, 'config', 'nav2.yaml')
    exploration_nav2    = os.path.join(pkg, 'config', 'exploration_nav2.yaml')
    urdf_file           = os.path.join(pkg, 'urdf',   'argo_mini.urdf')

    with open(urdf_file, 'r') as f:
        robot_desc = f.read()

    use_rviz               = LaunchConfiguration('use_rviz',               default='true')
    use_frontier_expl      = LaunchConfiguration('use_frontier_exploration', default='false')

    # Nav2 lifecycle node names — managed together by the exploration lifecycle manager
    nav2_exploration_nodes = [
        'behavior_server',
        'controller_server',
        'planner_server',
        'velocity_smoother',
        'bt_navigator',
    ]

    return LaunchDescription([

        # ── Launch arguments ──────────────────────────────────────────────────
        DeclareLaunchArgument(
            'use_rviz', default_value='true',
            description='Launch RViz2 to visualise the map being built'),

        DeclareLaunchArgument(
            'use_frontier_exploration', default_value='false',
            description=(
                'Launch Nav2 + FrontierExplorer for autonomous mapping. '
                'When true the robot will automatically seek unexplored frontiers '
                'until the entire reachable area is mapped.'
            )),

        # ── 1. Robot State Publisher (URDF → TF tree) ─────────────────────────
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

        # ── 2. Serial Bridge (wheel odometry needed for SLAM) ─────────────────
        Node(
            package='argo_mini',
            executable='serial_bridge',
            name='serial_bridge',
            output='screen',
            parameters=[{
                'port':            '/dev/ttyUSB1',
                'baud':            115200,
                'left_tick_scale': 2.1714,
                'fixed_dac':       112,
            }],
        ),

        # ── 3. RPLidar A1 ─────────────────────────────────────────────────────
        # frame_id must match the URDF joint child frame so robot_state_publisher
        # can provide the base_link → lidar_link TF that SLAM Toolbox needs.
        Node(
            package='rplidar_ros',
            executable='rplidar_composition',
            name='rplidar',
            output='screen',
            parameters=[{
                'serial_port':      '/dev/ttyUSB0',
                'serial_baudrate':  115200,
                'frame_id':         'lidar_link',
                'inverted':         False,
                'angle_compensate': True,
                'scan_mode':        'Boost',
            }],
        ),

        # ── 4. Scan Relay (timestamp correction) ──────────────────────────────
        Node(
            package='argo_mini',
            executable='scan_relay',
            name='scan_relay',
            output='screen',
        ),

        # ── 5. SLAM Toolbox — async mapping ───────────────────────────────────
        # async = non-blocking; the node processes scans as fast as it can
        # without stalling the ROS executor.
        Node(
            package='slam_toolbox',
            executable='async_slam_toolbox_node',
            name='slam_toolbox',
            output='screen',
            parameters=[slam_mapping_yaml],
        ),

        # ── 6. RViz2 (optional) — load SLAM Toolbox plugin for Serialize button
        Node(
            condition=IfCondition(use_rviz),
            package='rviz2',
            executable='rviz2',
            name='rviz2',
            output='screen',
        ),

        # ── 7. Autonomous frontier exploration stack (optional) ───────────────
        #
        # Launched only when use_frontier_exploration:=true.
        #
        # Velocity pipeline during exploration (no depth safety shield in SLAM
        # mode since no depth camera is running):
        #
        #   frontier_explorer → NavigateToPose → bt_navigator
        #       → controller_server → /cmd_vel_raw
        #       → velocity_smoother → /cmd_vel
        #       → serial_bridge → ESP32
        #
        GroupAction(
            condition=IfCondition(use_frontier_expl),
            actions=[

                # 7a. Behavior Server (Spin / BackUp recovery behaviors)
                LifecycleNode(
                    package='nav2_behaviors',
                    executable='behavior_server',
                    name='behavior_server',
                    namespace='',
                    output='screen',
                    parameters=[nav2_yaml],
                    remappings=[('cmd_vel', '/cmd_vel_raw')],
                ),

                # 7b. Planner Server — NavFn with allow_unknown:true
                #     exploration_nav2 OVERRIDES the planner plugin from nav2.yaml
                #     so that the robot can plan into unexplored territory.
                LifecycleNode(
                    package='nav2_planner',
                    executable='planner_server',
                    name='planner_server',
                    namespace='',
                    output='screen',
                    parameters=[nav2_yaml, exploration_nav2],
                ),

                # 7c. Controller Server (MPPI — same as navigation mode)
                LifecycleNode(
                    package='nav2_controller',
                    executable='controller_server',
                    name='controller_server',
                    namespace='',
                    output='screen',
                    parameters=[nav2_yaml],
                    remappings=[('cmd_vel', '/cmd_vel_raw')],
                ),

                # 7d. Velocity Smoother
                #     /cmd_vel_raw → smoothed → /cmd_vel (no safety shield in slam mode)
                LifecycleNode(
                    package='nav2_velocity_smoother',
                    executable='velocity_smoother',
                    name='velocity_smoother',
                    namespace='',
                    output='screen',
                    parameters=[nav2_yaml],
                    remappings=[
                        ('cmd_vel',          '/cmd_vel_raw'),
                        ('cmd_vel_smoothed', '/cmd_vel'),
                    ],
                ),

                # 7e. BT Navigator — executes the navigate_to_pose behavior tree
                LifecycleNode(
                    package='nav2_bt_navigator',
                    executable='bt_navigator',
                    name='bt_navigator',
                    namespace='',
                    output='screen',
                    parameters=[nav2_yaml],
                ),

                # 7f. Nav2 Lifecycle Manager — brings up 7a–7e in order
                Node(
                    package='nav2_lifecycle_manager',
                    executable='lifecycle_manager',
                    name='lifecycle_manager_exploration',
                    output='screen',
                    parameters=[{
                        'use_sim_time': False,
                        'autostart':    True,
                        'node_names':   nav2_exploration_nodes,
                    }],
                ),

                # 7g. Frontier Explorer — the exploration brain
                #     Detects frontier clusters on /map, scores them, and
                #     dispatches NavigateToPose goals to bt_navigator.
                Node(
                    package='argo_mini',
                    executable='frontier_explorer',
                    name='frontier_explorer',
                    output='screen',
                    parameters=[{
                        'free_threshold':    25,
                        'min_frontier_size': 8,
                        'goal_tolerance':    0.40,
                        'nav_timeout':       60.0,
                        'update_rate':       1.0,
                        'blacklist_radius':  0.35,
                    }],
                ),
            ],
        ),
    ])
