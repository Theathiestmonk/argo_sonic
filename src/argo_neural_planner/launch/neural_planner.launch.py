from launch import LaunchDescription
from launch_ros.actions import Node

def generate_launch_description():
    return LaunchDescription([
        Node(
            package='argo_neural_planner',
            executable='planner_node',
            name='active_neural_planner_node',
            output='screen',
            parameters=[{
                'use_sim_time': True # Ensures compatibility with simulators like Gazebo or Isaac Sim
            }]
        )
    ])