import subprocess
from datetime import datetime, timezone
from pathlib import Path

from setuptools import find_packages, setup
from setuptools.command.build_py import build_py

package_name = "ros_fairy"
HERE = Path(__file__).resolve().parent


def _git(*args):
    """Output of a git command in this checkout, or None. safe.directory: a
    root install (sudo ./install.sh) of a checkout owned by the operator is
    otherwise refused as "dubious ownership". Same logic as
    ros_fairy/build_info.py's git_details (setup can't import the package it
    is building)."""
    try:
        out = subprocess.run(
            ["git", "-c", f"safe.directory={HERE}", "-C", str(HERE), *args],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _build_details():
    commit = _git("rev-parse", "HEAD")
    details = {"built_at": datetime.now(timezone.utc).isoformat()}
    if commit:
        status = _git("status", "--porcelain", "--untracked-files=no")
        details.update(
            commit=commit,
            describe=_git("describe", "--always", "--dirty", "--tags"),
            branch=_git("rev-parse", "--abbrev-ref", "HEAD"),
            dirty=bool(status) if status is not None else None)
    return details


class BuildPyWithBuildInfo(build_py):
    """Write ros_fairy/_build_info.py (git commit, describe, branch, dirty,
    build time) into the built package: an installed package has no .git,
    so this is the only moment the commit can be recorded. Written into the
    build directory only; the source tree is left untouched."""

    def run(self):
        super().run()
        target = Path(self.build_lib) / package_name / "_build_info.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            '"""Written by setup.py at build time; do not edit."""\n\n'
            f"BUILD = {_build_details()!r}\n")


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
    cmdclass={"build_py": BuildPyWithBuildInfo},
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
