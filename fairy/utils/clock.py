"""Best-effort detection of whether the system clock is synchronised.

An unsynchronised clock (recording started before NTP/chrony stepped the time)
leaves rosbag2 messages stamped near the epoch. Such bags are effectively
unusable: ``ros2 bag play`` honours the timestamps and stalls on the resulting
~56-year timeline, and the data is worthless for any time-critical processing.

ros-fairy checks this *before* recording so the operator can wait for sync rather
than discovering the problem after the mission (the dashcam should fail safe and
warn early). Detection is best-effort and must never raise: ``None`` means "I
couldn't tell", which callers treat as "don't nag".
"""

import subprocess

CHECK_TIMEOUT_S = 5


def is_synchronized() -> bool | None:
    """Whether the kernel clock is NTP-synchronised.

    Uses ``timedatectl`` (present on the systemd-based distros ROS 2 targets);
    it reports the kernel's sync status regardless of whether chrony or
    systemd-timesyncd is the daemon. Returns ``None`` when the answer can't be
    determined (no ``timedatectl``, command failed, unexpected output).
    """
    try:
        result = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=CHECK_TIMEOUT_S)
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip().lower()
    if value in ("yes", "true", "1"):
        return True
    if value in ("no", "false", "0"):
        return False
    return None


WARNING = (
    "The robot's clock doesn't look synchronised yet. If you record now, the "
    "data may be stamped with the wrong time, which can make it unplayable and "
    "unusable later. Wait a minute for the clock to sync, if you can. If you "
    "must record now, you can re-stamp the recording afterward with "
    "'ros2 fairy repair' so it plays back correctly."
)
