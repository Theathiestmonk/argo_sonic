"""
Argo Mini — Gazebo Simulation (base layer)
============================================
Spawns the Argo Mini robot (same URDF/xacro as the real robot, with Gazebo
sim sensor/plugin tags) into a simulated restaurant in Gazebo Harmonic, and
bridges its topics to ROS 2. This launch file on its own gives you the robot
driveable with teleop — SLAM and Nav2 are layered on top by slam.launch.py
and nav.launch.py, which both include this file.

Usage:
    ros2 launch argo_mini_sim gazebo.launch.py
    ros2 run teleop_twist_keyboard teleop_twist_keyboard   # drive it around

Args:
  world       (default: worlds/argo_restaurant.sdf)
  use_rviz    (default: true)
  rviz_config (default: rviz/slam.rviz)
  headless    (default: false) — run gz sim server-only, no GUI window
  x, y, z, yaw — spawn pose (default puts the robot just inside the
                 restaurant's entrance, facing into the dining area)
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, TimerAction
from launch.conditions import IfCondition, UnlessCondition
from launch.substitutions import Command, LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    pkg = get_package_share_directory('argo_mini_sim')

    default_world = os.path.join(pkg, 'worlds', 'argo_restaurant.sdf')
    default_rviz = os.path.join(pkg, 'rviz', 'slam.rviz')
    xacro_file = os.path.join(pkg, 'urdf', 'argo_mini.xacro')
    bridge_config = os.path.join(pkg, 'config', 'gz_bridge.yaml')

    world = LaunchConfiguration('world')
    use_rviz = LaunchConfiguration('use_rviz')
    rviz_config = LaunchConfiguration('rviz_config')
    headless = LaunchConfiguration('headless')
    spawn_x = LaunchConfiguration('x')
    spawn_y = LaunchConfiguration('y')
    spawn_z = LaunchConfiguration('z')
    spawn_yaw = LaunchConfiguration('yaw')

    robot_description = Command(['xacro ', xacro_file])

    robot_state_publisher = Node(
        package='robot_state_publisher',
        executable='robot_state_publisher',
        name='robot_state_publisher',
        output='screen',
        parameters=[{
            'robot_description': robot_description,
            'use_sim_time': True,
        }],
    )

    gz_sim_gui = ExecuteProcess(
        condition=UnlessCondition(headless),
        cmd=['gz', 'sim', '-r', world],
        output='screen',
    )
    gz_sim_headless = ExecuteProcess(
        condition=IfCondition(headless),
        cmd=['gz', 'sim', '-s', '-r', world],
        output='screen',
    )

    bridge = Node(
        package='ros_gz_bridge',
        executable='parameter_bridge',
        name='gz_ros2_bridge',
        output='screen',
        parameters=[{
            'config_file': bridge_config,
            'use_sim_time': True,
        }],
    )

    # Publishes the odom->base_footprint TF from /odom (see gz_bridge.yaml
    # for why the bridge's own /tf isn't used directly).
    odom_tf_broadcaster = Node(
        package='argo_mini_sim',
        executable='odom_tf_broadcaster',
        name='odom_tf_broadcaster',
        output='screen',
        parameters=[{'use_sim_time': True}],
    )

    spawn_robot = TimerAction(
        period=8.0,
        actions=[
            Node(
                package='ros_gz_sim',
                executable='create',
                name='spawn_argo_mini',
                output='screen',
                arguments=[
                    '-name', 'argo_mini',
                    '-topic', 'robot_description',
                    '-x', spawn_x,
                    '-y', spawn_y,
                    '-z', spawn_z,
                    '-Y', spawn_yaw,
                ],
            )
        ],
    )

    rviz = Node(
        condition=IfCondition(use_rviz),
        package='rviz2',
        executable='rviz2',
        name='rviz2',
        output='screen',
        arguments=['-d', rviz_config],
        parameters=[{'use_sim_time': True}],
    )

    return LaunchDescription([
        DeclareLaunchArgument('world', default_value=default_world,
                               description='Path to the Gazebo world SDF file'),
        DeclareLaunchArgument('use_rviz', default_value='true'),
        DeclareLaunchArgument('rviz_config', default_value=default_rviz),
        DeclareLaunchArgument('headless', default_value='false',
                               description='Run gz sim server-only (no GUI window)'),
        DeclareLaunchArgument('x', default_value='-6.5'),
        DeclareLaunchArgument('y', default_value='1.0'),
        DeclareLaunchArgument('z', default_value='0.15'),
        DeclareLaunchArgument('yaw', default_value='0.0'),

        robot_state_publisher,
        gz_sim_gui,
        gz_sim_headless,
        bridge,
        odom_tf_broadcaster,
        spawn_robot,
        rviz,
    ])
