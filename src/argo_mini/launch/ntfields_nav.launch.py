"""
Argo Mini — NTFields Navigation Launch
=======================================
Extends the base nav.launch.py with:
  • NTFields Trainer    — auto-trains on /map, saves model to ~/ntfields_model.pt
  • NTFields Navigator  — action server /ntfields/navigate_to_pose
  • NTFields Social Shield — replaces binary depth_safety_shield

Usage
-----
  ros2 launch argo_mini ntfields_nav.launch.py map:=/home/argo/maps/indoor_map

  After ~15-20 min the trainer will finish and the navigator will
  automatically load the model.  Send goals via:
    ros2 action send_goal /ntfields/navigate_to_pose \\
      nav2_msgs/action/NavigateToPose \\
      "{pose: {header: {frame_id: map}, pose: {position: {x: 2.0, y: 1.0}}}}"

  Or use the existing restaurant_agent which calls /ntfields/navigate_to_pose.

Upgrade path
------------
  Phase 1 (now)    : NTFields navigator + keep SmacHybrid as fallback
  Phase 2 (future) : Remove SmacHybrid, NTFields is sole planner
  Phase 3 (future) : NTFields time field as MPPI critic cost function
"""

import os
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    pkg = get_package_share_directory('argo_mini')

    map_path = LaunchConfiguration('map',
        default=os.path.join(pkg, 'maps', 'indoor_map'))
    device   = LaunchConfiguration('device', default='cuda')

    return LaunchDescription([

        DeclareLaunchArgument('map',    default_value=map_path),
        DeclareLaunchArgument('device', default_value='cuda',
            description='PyTorch device for training/inference (cuda or cpu)'),

        # ── 1. Full base nav stack (SLAM, Nav2, serial bridge, etc.) ─────
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                os.path.join(pkg, 'launch', 'nav.launch.py')),
            launch_arguments={
                'map':        map_path,
                'use_camera': 'true',
                'use_rviz':   'true',
            }.items(),
        ),

        # ── 2. NTFields Trainer (background GPU training on /map) ────────
        Node(
            package='argo_mini',
            executable='ntfields_trainer',
            name='ntfields_trainer',
            output='screen',
            parameters=[{
                'device':           device,
                'num_epochs':       800,
                'steps_per_epoch':  150,
                'batch_size':       512,
                'lr':               1e-3,
                'fourier_features': 256,
                'hidden_dim':       256,
                'n_sample_points':  60000,
                'min_clearance_m':  0.12,
                'epsilon':          0.35,
                'lam':              2.0,
                'change_threshold': 0.05,
            }],
        ),

        # ── 3. NTFields Navigator (action server) ─────────────────────────
        Node(
            package='argo_mini',
            executable='ntfields_navigator',
            name='ntfields_navigator',
            output='screen',
            parameters=[{
                'device':          device,
                'alpha':           0.03,
                'goal_radius':     0.12,
                'max_steps':       600,
                'waypoint_stride': 4,
                'global_frame':    'map',
                'robot_frame':     'base_link',
            }],
        ),

        # ── 4. NTFields Social Shield (replaces depth_safety_shield) ─────
        #
        # NOTE: depth_safety_shield is still launched by nav.launch.py.
        # To switch to the social shield:
        #   a) Set use_camera:=false to disable depth_safety_shield launching,
        #      then add the camera node separately.
        #   b) Or remap: this node takes /cmd_vel_smoothed → /cmd_vel
        #      while depth_safety_shield is disabled via parameter.
        #
        # For now: social shield runs on /cmd_vel_smoothed2 → /cmd_vel2
        # so both can coexist for testing.  Set active_shield below.
        Node(
            package='argo_mini',
            executable='ntfields_social_shield',
            name='ntfields_social_shield',
            output='screen',
            parameters=[{
                'input_topic':      '/cmd_vel_smoothed',
                'output_topic':     '/cmd_vel',
                'depth_topic':
                    '/ascamera_hp60c/camera_publisher/depth0/points',
                'scan_topic':       '/scan_corrected',
                'use_optical_frame': False,
                # Static speed field
                'epsilon':           0.35,
                'lam':               2.0,
                'min_speed_static':  0.05,
                # Social
                'sigma_human':       0.70,
                'amplitude_human':   0.95,
                'social_max_range':  2.00,
                # Close-range depth
                'depth_stop_dist':   0.30,
                'depth_slow_dist':   0.70,
                'depth_height_min':  0.10,
                'depth_height_max':  1.80,
                'depth_width':       0.30,
                'depth_min_points':  5,
                'depth_stale_s':     1.0,
                # Leg detector
                'leg_cluster_gap':   0.10,
                'leg_min_diam':      0.04,
                'leg_max_diam':      0.25,
                'person_pair_min':   0.10,
                'person_pair_max':   0.80,
            }],
        ),
    ])
