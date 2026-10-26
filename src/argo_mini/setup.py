from setuptools import setup
from glob import glob

package_name = 'argo_mini'

setup(
    name=package_name,
    version='0.0.1',
    packages=[
        package_name,
    ],
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        ('share/' + package_name + '/config', glob('config/*.yaml')),
        ('share/' + package_name + '/maps', glob('maps/*.pgm') + glob('maps/*.yaml') + glob('maps/*.posegraph') + glob('maps/*.data') + glob('maps/*.json')),
        ('share/' + package_name + '/waypoints', glob('waypoints/*.json')),
        ('share/' + package_name + '/config/bt', glob('config/bt/*.xml')),
        ('share/' + package_name + '/urdf', glob('urdf/*.urdf')),
        ('share/' + package_name + '/launch', glob('launch/*.py')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='argo',
    maintainer_email='argo@example.com',
    description='Argo Mini autonomous delivery robot',
    license='Apache License 2.0',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'serial_bridge=argo_mini.serial_bridge:main',
            'scan_relay=argo_mini.scan_relay:main',
            'safety_shield=argo_mini.safety_shield:main',
            'patrol_manager=argo_mini.patrol_manager:main',
            'pointcloud_restamper=argo_mini.pointcloud_restamper:main',
            'waypoint_manager=argo_mini.waypoint_manager:main',
            'slam_teleop=argo_mini.slam_teleop:main',
            'pose_init=argo_mini.pose_init:main',
            'ntfields_planner_node=argo_mini.ntfields_planner_node:main',
            'ceiling_features=argo_mini.ceiling_features:main',
            'ceiling_calibrate=argo_mini.ceiling_calibrate:main',
            'ceiling_map_builder=argo_mini.ceiling_map_builder:main',
            'ceiling_azimuth_scan=argo_mini.ceiling_azimuth_scan:main',
        ],
    },
)
