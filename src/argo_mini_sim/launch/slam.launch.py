"""
Argo Mini — Gazebo SLAM (mapping) Demo
=========================================
Brings up the Gazebo simulation (gazebo.launch.py) and runs SLAM Toolbox in
mapping mode, using the same tuning as the real robot's config/slam_mapping.yaml.
Drive the robot around the simulated restaurant to build a map, then save it.

Usage — manual teleop (run in a second terminal):
    ros2 launch argo_mini_sim slam.launch.py
    ros2 run teleop_twist_keyboard teleop_twist_keyboard

Usage — autonomous frontier exploration (no driving required, good for an
unattended recording — the robot maps the whole restaurant on its own):
    ros2 launch argo_mini_sim slam.launch.py use_frontier_exploration:=true

Saving the map (posegraph format — required for nav.launch.py's
slam_toolbox localization mode; do NOT use nav2_map_server's map_saver,
it only saves a flat PGM that localization mode can't reload):
    ros2 service call /slam_toolbox/serialize_map \\
        slam_toolbox/srv/SerializePoseGraph \\
        "{filename: '/home/shivu/argo_sonic/src/argo_mini_sim/maps/demo_map'}"
Or click "Serialize Map" in the SlamToolboxPlugin panel docked in RViz.

Args:
  use_rviz                  (default true)
  use_frontier_exploration  (default false) — autonomous mapping via Nav2 +
                                              the same frontier_explorer node
                                              the real robot uses
  world, x, y, z, yaw, headless — forwarded to gazebo.launch.py
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, GroupAction, IncludeLaunchDescription, TimerAction
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import LifecycleNode, Node


def generate_launch_description():
    pkg = get_package_share_directory('argo_mini_sim')

    slam_yaml = os.path.join(pkg, 'config', 'slam_toolbox_mapping.yaml')
    nav2_yaml = os.path.join(pkg, 'config', 'nav2_params.yaml')
    exploration_yaml = os.path.join(pkg, 'config', 'exploration_nav2.yaml')
    bt_xml = os.path.join(pkg, 'config', 'bt', 'explore_navigate_to_pose.xml')
    default_rviz = os.path.join(pkg, 'rviz', 'slam.rviz')

    use_frontier_expl = LaunchConfiguration('use_frontier_exploration')

    gazebo = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg, 'launch', 'gazebo.launch.py')),
        launch_arguments={
            'world': LaunchConfiguration('world'),
            'use_rviz': LaunchConfiguration('use_rviz'),
            'rviz_config': default_rviz,
            'headless': LaunchConfiguration('headless'),
            'x': LaunchConfiguration('x'),
            'y': LaunchConfiguration('y'),
            'z': LaunchConfiguration('z'),
            'yaw': LaunchConfiguration('yaw'),
        }.items(),
    )

    slam_toolbox = TimerAction(
        period=10.0,
        actions=[
            Node(
                package='slam_toolbox',
                executable='async_slam_toolbox_node',
                name='slam_toolbox',
                output='screen',
                parameters=[slam_yaml],
            )
        ],
    )

    # async_slam_toolbox_node is a lifecycle node on this slam_toolbox build —
    # it advertises no /scan subscription or /map publisher until explicitly
    # configured and activated.
    slam_toolbox_configure = TimerAction(
        period=13.0,
        actions=[ExecuteProcess(
            cmd=['ros2', 'lifecycle', 'set', '/slam_toolbox', 'configure'],
            output='screen')],
    )
    slam_toolbox_activate = TimerAction(
        period=16.0,
        actions=[ExecuteProcess(
            cmd=['ros2', 'lifecycle', 'set', '/slam_toolbox', 'activate'],
            output='screen')],
    )

    # ── Autonomous frontier exploration (optional) ─────────────────────────
    # Nav2 drives the robot toward unexplored space; frontier_explorer (the
    # exact node the real robot runs) picks the goals. No <Spin>/<BackUp> in
    # the exploration BT — physical recovery motion distorts the pose graph.
    exploration_nodes = [
        'behavior_server', 'controller_server', 'planner_server',
        'velocity_smoother', 'bt_navigator',
    ]

    exploration_stack = TimerAction(
        period=19.0,
        actions=[GroupAction(
            condition=IfCondition(use_frontier_expl),
            actions=[
                LifecycleNode(
                    package='nav2_behaviors', executable='behavior_server',
                    name='behavior_server', namespace='', output='screen',
                    parameters=[nav2_yaml, exploration_yaml],
                    remappings=[('cmd_vel', '/cmd_vel_raw')],
                ),
                LifecycleNode(
                    package='nav2_planner', executable='planner_server',
                    name='planner_server', namespace='', output='screen',
                    parameters=[nav2_yaml, exploration_yaml],
                ),
                LifecycleNode(
                    package='nav2_controller', executable='controller_server',
                    name='controller_server', namespace='', output='screen',
                    parameters=[nav2_yaml, exploration_yaml],
                    remappings=[('cmd_vel', '/cmd_vel_raw')],
                ),
                LifecycleNode(
                    package='nav2_velocity_smoother', executable='velocity_smoother',
                    name='velocity_smoother', namespace='', output='screen',
                    parameters=[nav2_yaml],
                    remappings=[('cmd_vel', '/cmd_vel_raw'),
                                ('cmd_vel_smoothed', '/cmd_vel')],
                ),
                LifecycleNode(
                    package='nav2_bt_navigator', executable='bt_navigator',
                    name='bt_navigator', namespace='', output='screen',
                    parameters=[nav2_yaml, exploration_yaml, {
                        'default_nav_to_pose_bt_xml': bt_xml,
                        'default_nav_through_poses_bt_xml': bt_xml,
                    }],
                ),
                Node(
                    package='nav2_lifecycle_manager', executable='lifecycle_manager',
                    name='lifecycle_manager_exploration', output='screen',
                    parameters=[{'use_sim_time': True, 'autostart': True,
                                 'node_names': exploration_nodes}],
                ),
                Node(
                    package='argo_mini', executable='frontier_explorer',
                    name='frontier_explorer', output='screen',
                    parameters=[{
                        'use_sim_time': True,
                        'free_threshold': 25,
                        'min_frontier_size': 8,
                        'goal_tolerance': 0.40,
                        'nav_timeout': 60.0,
                        'update_rate': 1.0,
                        'blacklist_radius': 0.35,
                    }],
                ),
            ],
        )],
    )

    return LaunchDescription([
        DeclareLaunchArgument('world', default_value=os.path.join(
            pkg, 'worlds', 'argo_restaurant.sdf')),
        DeclareLaunchArgument('use_rviz', default_value='true'),
        DeclareLaunchArgument('headless', default_value='false'),
        DeclareLaunchArgument('x', default_value='-6.5'),
        DeclareLaunchArgument('y', default_value='1.0'),
        DeclareLaunchArgument('z', default_value='0.15'),
        DeclareLaunchArgument('yaw', default_value='0.0'),
        DeclareLaunchArgument('use_frontier_exploration', default_value='false',
                               description='Autonomous mapping via Nav2 + frontier_explorer'),

        gazebo,
        slam_toolbox,
        slam_toolbox_configure,
        slam_toolbox_activate,
        exploration_stack,
    ])
