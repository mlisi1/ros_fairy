"""Host-level facts: hostname, kernel, arch, ROS distro, installed ROS debs."""

import platform
import socket
import subprocess
from typing import Any

from ros_fairy.utils import clock, ros_distro

DPKG_TIMEOUT_S = 10


def _ros_deb_versions() -> dict[str, str] | None:
    """Installed ros-* Debian packages -> version.

    ``{}`` when dpkg answered that there are none; None when it couldn't be
    asked (no dpkg, a timeout, an error) — "not captured", which the diff
    must not read as "every package removed". Only packages dpkg reports as
    installed count: entries left in the database after removal ("rc",
    "un") are not.
    """
    try:
        out = subprocess.run(
            ["dpkg-query", "-W", "-f", "${db:Status-Abbrev}|${Package} "
             "${Version}\n", "ros-*"],
            capture_output=True, text=True, timeout=DPKG_TIMEOUT_S,
            encoding="utf-8", errors="replace")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        # dpkg-query exits 1 when nothing matches the pattern.
        return {} if "no packages found" in out.stderr else None
    versions = {}
    for line in out.stdout.splitlines():
        status, _, rest = line.partition("|")
        parts = rest.split(maxsplit=1)
        if len(status) >= 2 and status[1] == "i" and len(parts) == 2:
            versions[parts[0]] = parts[1]
    return versions


def harvest() -> dict[str, Any]:
    uname = platform.uname()
    apt_ros_versions = _ros_deb_versions()
    return {
        "hostname": socket.gethostname(),
        "kernel": f"{uname.system} {uname.release}",
        "arch": uname.machine,
        # The watchdog often runs unsourced, so $ROS_DISTRO is empty; fall back
        # to inferring the distro from the installed ros-<distro>-* packages.
        "ros_distro": ros_distro.detect()
        or ros_distro.infer_from_packages(apt_ros_versions or {}),
        "apt_ros_versions": apt_ros_versions,
        # Recorded regardless of outcome so the archive shows the clock-sync
        # check was performed (True/False, or None when it couldn't be told).
        "clock_synchronized": clock.is_synchronized(),
    }
