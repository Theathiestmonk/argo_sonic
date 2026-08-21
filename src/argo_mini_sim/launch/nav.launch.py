"""
Argo Mini — Gazebo Nav2 Demo
==============================
Brings up the Gazebo simulation (gazebo.launch.py), SLAM Toolbox in
LOCALIZATION mode against a previously saved posegraph map (built with
slam.launch.py), and the full Nav2 stack — same MPPI controller/costmap/
behavior tuning as the real robot's config/nav2.yaml. Just like on the real
robot, slam_toolbox (not AMCL) serves /map and handles map->odom.

slam_toolbox's map_start_pose (config/slam_toolbox_localization.yaml) is
fixed to this launch file's default spawn pose (x/y/yaw below) — like a
robot placed at its charging dock, it must start localization from the
same pose it was at when the map was built. If you spawn somewhere else
(x:=/y:=/yaw:= overrides), update map_start_pose in that yaml to match, or
localization will believe it's still at the old pose.

Usage:
    ros2 launch argo_mini_sim nav.launch.py map:=/path/to/demo_map

Then in RViz: click "Nav2 Goal" (or use the docked Navigation 2 panel) and
click a point in the dining area — the robot will plan around the tables
and drive there.

Args:
  map        (required) — base path to a .posegraph map (no extension),
                            saved via slam.launch.py's serialize_map service
  use_rviz   (default true)
  world, x, y, z, yaw, headless — forwarded to gazebo.launch.py (x/y/yaw
                                   default must match map_start_pose above)
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, IncludeLaunchDescription, TimerAction
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import LifecycleNode, Node


def generate_launch_description():
    pkg = get_package_share_directory('argo_mini_sim')

    slam_yaml = os.path.join(pkg, 'config', 'slam_toolbox_localization.yaml')
    nav2_yaml = os.path.join(pkg, 'config', 'nav2_params.yaml')
    bt_xml = os.path.join(pkg, 'config', 'bt', 'navigate_to_pose.xml')
    default_rviz = os.path.join(pkg, 'rviz', 'nav2.rviz')
    default_map = os.path.join(pkg, 'maps', 'demo_map')

    map_path = LaunchConfiguration('map')

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

    slam_toolbox_localization = TimerAction(
        period=10.0,
        actions=[
            Node(
                package='slam_toolbox',
                executable='localization_slam_toolbox_node',
                name='slam_toolbox',
                output='screen',
                parameters=[slam_yaml, {'map_file_name': map_path}],
            )
        ],
    )

    # localization_slam_toolbox_node is a lifecycle node on this slam_toolbox
    # build — it won't load the map or start localizing until explicitly
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

    nav2_nodes = [
        'behavior_server', 'controller_server', 'planner_server',
        'velocity_smoother', 'bt_navigator',
    ]

    nav2_stack = TimerAction(
        period=19.0,
        actions=[
            LifecycleNode(
                package='nav2_behaviors', executable='behavior_server',
                name='behavior_server', namespace='', output='screen',
                parameters=[nav2_yaml],
                remappings=[('cmd_vel', '/cmd_vel_raw')],
            ),
            LifecycleNode(
                package='nav2_planner', executable='planner_server',
                name='planner_server', namespace='', output='screen',
                parameters=[nav2_yaml],
            ),
            LifecycleNode(
                package='nav2_controller', executable='controller_server',
                name='controller_server', namespace='', output='screen',
                parameters=[nav2_yaml],
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
                parameters=[nav2_yaml, {
                    'default_nav_to_pose_bt_xml': bt_xml,
                    'default_nav_through_poses_bt_xml': bt_xml,
                }],
            ),
            Node(
                package='nav2_lifecycle_manager', executable='lifecycle_manager',
                name='lifecycle_manager_nav', output='screen',
                parameters=[{'use_sim_time': True, 'autostart': True,
                             'node_names': nav2_nodes}],
            ),
        ],
    )

    return LaunchDescription([
        DeclareLaunchArgument('map', default_value=default_map,
                               description='Base path to a .posegraph map (no extension)'),
        DeclareLaunchArgument('world', default_value=os.path.join(
            pkg, 'worlds', 'argo_restaurant.sdf')),
        DeclareLaunchArgument('use_rviz', default_value='true'),
        DeclareLaunchArgument('headless', default_value='false'),
        DeclareLaunchArgument('x', default_value='-6.5'),
        DeclareLaunchArgument('y', default_value='1.0'),
        DeclareLaunchArgument('z', default_value='0.15'),
        DeclareLaunchArgument('yaw', default_value='0.0'),

        gazebo,
        slam_toolbox_localization,
        slam_toolbox_configure,
        slam_toolbox_activate,
        nav2_stack,
    ])
