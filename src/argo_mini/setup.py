from setuptools import setup
import os

package_name = 'argo_mini'

data_files = [
    ('share/ament_index/resource_index/packages',
     ['resource/' + package_name]),
    ('share/' + package_name, ['package.xml']),
    ('share/' + package_name + '/launch',
     ['launch/slam.launch.py', 'launch/nav.launch.py']),
    ('share/' + package_name + '/config',
     ['config/slam_toolbox.yaml', 'config/nav2.yaml']),
    ('share/' + package_name + '/config/bt',
     ['config/bt/navigate_to_pose.xml']),
]

if os.path.exists('maps/indoor_map.yaml'):
    data_files.append(
        ('share/' + package_name + '/maps',
         ['maps/indoor_map.yaml', 'maps/indoor_map.pgm']))

if os.path.exists('waypoints/waypoints.json'):
    data_files.append(
        ('share/' + package_name + '/waypoints',
         ['waypoints/waypoints.json']))

setup(
    name=package_name,
    version='1.0.0',
    packages=[package_name],
    data_files=data_files,
    install_requires=['setuptools'],
    entry_points={
        'console_scripts': [
            'serial_bridge    = argo_mini.serial_bridge:main',
            'waypoint_manager = argo_mini.waypoint_manager:main',
        ],
    },
)
