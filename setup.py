from setuptools import find_packages, setup

package_name = "ros_fairy"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["tests", "tests.*"]),
    package_data={
        "ros_fairy.watchdog": ["ros-fairy-watchdog.service"],
    },
    data_files=[
        ("share/ament_index/resource_index/packages",
         ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/systemd",
         ["systemd/ros-fairy-watchdog.service"]),
    ],
    install_requires=[
        "setuptools",
        "pydantic>=2.5",
        "rich>=13",
        "PyYAML>=6",
        "inotify_simple>=1.3",
        # MCAP is rosbag2's default storage from Jazzy on; the watchdog needs it
        # to read per-message timestamps for bag duration and topic health.
        # The code still degrades gracefully if it is somehow absent.
        "mcap",
    ],
    extras_require={
        "test": ["pytest", "rocrate"],
        "dev": ["pytest", "rocrate", "ruff", "mypy"],
    },
    zip_safe=False,
    author="ros-fairy contributors",
    maintainer="ros-fairy contributors",
    maintainer_email="fleet@example.org",
    description="Make ROS 2 field mission data FAIR-compliant with zero "
                "friction: automatic context capture, plain-language "
                "briefings, RO-Crate archives.",
    license="Apache-2.0",
    entry_points={
        "ros2cli.command": [
            "fairy = ros_fairy.command.fairy:FairyCommand",
        ],
        "ros2cli.extension_point": [
            "fairy.verb = ros_fairy.subcommands:VerbExtension",
        ],
        "fairy.verb": [
            "setup = ros_fairy.subcommands.setup:SetupVerb",
            "mission_start = ros_fairy.subcommands.mission_start:"
            "MissionStartVerb",
            "mission_record = ros_fairy.subcommands.mission_record:"
            "MissionRecordVerb",
            "mission_close = ros_fairy.subcommands.mission_close:"
            "MissionCloseVerb",
            "mission_abort = ros_fairy.subcommands.mission_abort:"
            "MissionAbortVerb",
            "mission_delete = ros_fairy.subcommands.mission_delete:"
            "MissionDeleteVerb",
            "mission_status = ros_fairy.subcommands.mission_status:"
            "MissionStatusVerb",
            "list = ros_fairy.subcommands.list_missions:ListVerb",
            "diff = ros_fairy.subcommands.mission_diff:DiffVerb",
            "verify = ros_fairy.subcommands.verify:VerifyVerb",
            "doctor = ros_fairy.subcommands.doctor:DoctorVerb",
            "export = ros_fairy.subcommands.export:ExportVerb",
            "repair = ros_fairy.subcommands.repair:RepairVerb",
            "adopt = ros_fairy.subcommands.adopt:AdoptVerb",
            "reindex = ros_fairy.subcommands.reindex:ReindexVerb",
        ],
        "console_scripts": [
            "ros-fairy-watchdog = ros_fairy.watchdog.watchdog:main",
            "ros-fairy-setup = ros_fairy.subcommands.setup:main",
        ],
    },
)
