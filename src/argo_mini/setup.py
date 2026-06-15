from setuptools import setup
from glob import glob

package_name = 'argo_mini'

setup(
    name=package_name,
    version='0.0.1',
    packages=[
        package_name,
        package_name + '.restaurant_agent',
        package_name + '.restaurant_agent.nodes',
        package_name + '.restaurant_agent.subgraphs',
        package_name + '.restaurant_agent.integrations',
        package_name + '.stt',
    ],
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        ('share/' + package_name + '/config', glob('config/*.yaml')),
        ('share/' + package_name + '/maps', glob('maps/*.pgm') + glob('maps/*.yaml') + glob('maps/*.posegraph') + glob('maps/*.data')),
        ('share/' + package_name + '/config/bt', glob('config/bt/*.xml')),
        ('share/' + package_name + '/urdf', glob('urdf/*.urdf')),
        ('share/' + package_name + '/launch', glob('launch/*.py')),
        ('share/' + package_name + '/agent_data',
            glob('argo_mini/restaurant_agent/data/*.json')),
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
            'depth_safety_shield=argo_mini.depth_safety_shield:main',
            'dashboard=argo_mini.dashboard:main',
            'waypoint_ui=argo_mini.waypoint_ui:main',
            'waypoint_manager=argo_mini.waypoint_manager:main',
            'voice_agent=argo_mini.voice_agent:main',
            'voice_agent2=argo_mini.voice_agent2:main',
            'slam_teleop=argo_mini.slam_teleop:main',
            'pose_setter=argo_mini.pose_setter:main',
            'restaurant_agent=argo_mini.restaurant_agent_node:main',
        ],
    },
)
