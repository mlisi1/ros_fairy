"""Capture, serialise and read back the ROS 2 environment.

The watchdog runs as a system service with no login shell, so the ROS
environment the operator had sourced is snapshotted into a systemd-style
``KEY=value`` file. Two such files exist:

- ``/etc/ros-fairy/watchdog.env`` — frozen at ``ros2 fairy setup``, loaded by the
  unit's ``EnvironmentFile=``. This is what the service starts with.
- ``<spool>/session.env`` — refreshed by ``mission_start`` / ``mission_record``
  from the *live recording shell* and applied by the watchdog at harvest time,
  so the harvest always matches the session actually recording even if the
  frozen snapshot has drifted (different ROS_DOMAIN_ID / RMW / overlay).

This module is the single source of truth for which variables count as "the ROS
environment" and how the files are written and parsed.
"""

import os
import shlex
import subprocess
from collections.abc import Mapping
from pathlib import Path

from ros_fairy.utils import fsio

# Every ROS/build-tool variable plus the search paths ros2 and rclpy need to
# find their plugins and libraries. Keep in sync with the unit documentation.
ROS_ENV_PREFIXES = ("ROS_", "AMENT_", "RMW_", "COLCON_")

# ros_fairy's own directory-override env vars (utils/paths.py) happen to start
# with "ROS_" too, so ROS_ENV_PREFIXES would otherwise scoop them into the
# watchdog's persisted environment file — silently redirecting the service's
# spool/archive/index paths for anyone who has one of these set in their
# shell. They carry no ROS meaning, so they're excluded explicitly.
_NOT_ROS_ENV_PREFIX = "ROS_FAIRY_"
ROS_ENV_NAMES = (
    "PATH", "LD_LIBRARY_PATH", "PYTHONPATH", "CMAKE_PREFIX_PATH",
    "CYCLONEDDS_URI", "FASTRTPS_DEFAULT_PROFILES_FILE",
    "FASTDDS_DEFAULT_PROFILES_FILE",
)

# Variables whose mismatch puts the watchdog on a different DDS partition than
# the recorder (an empty graph even though everything is "running").
DISCOVERY_KEYS = ("ROS_DOMAIN_ID", "RMW_IMPLEMENTATION")

# The only keys the watchdog adopts at *runtime* from <spool>/session.env. That
# file is group-writable and the watchdog runs as root, so honouring loader
# paths (PATH / LD_LIBRARY_PATH / PYTHONPATH / AMENT_PREFIX_PATH ...) from it
# would let any spool writer run code as root. These keys only steer DDS
# discovery — which partition the harvest sees — and cannot load code. A missing
# or unusable base ROS environment is a setup/doctor failure, not something to
# paper over by trusting the spool.
SESSION_ADOPT_KEYS = (
    "ROS_DOMAIN_ID", "RMW_IMPLEMENTATION", "ROS_LOCALHOST_ONLY",
    "ROS_AUTOMATIC_DISCOVERY_RANGE", "ROS_STATIC_PEERS",
)


def safe_session_env(env: Mapping[str, str]) -> dict[str, str]:
    """The subset of a (possibly untrusted) session.env safe to adopt as root.

    Restricted to :data:`SESSION_ADOPT_KEYS`; deliberately drops every loader
    path so a group-writable ``session.env`` cannot inject libraries/modules
    into the root watchdog process.
    """
    return {key: env[key] for key in SESSION_ADOPT_KEYS if key in env}


def capture(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The ROS-relevant subset of an environment (defaults to the process)."""
    source: Mapping[str, str] = os.environ if environ is None else environ
    return {
        key: val for key, val in source.items()
        if not key.startswith(_NOT_ROS_ENV_PREFIX)
        and (key in ROS_ENV_NAMES or key.startswith(ROS_ENV_PREFIXES))
    }


def serialize(env: dict[str, str]) -> str:
    """Render as a systemd EnvironmentFile (``KEY=value`` per line, sorted)."""
    lines = [f"{key}={env[key]}" for key in sorted(env)]
    return "\n".join(lines) + ("\n" if lines else "")


def parse(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines; ignores blanks and ``#`` comments."""
    env: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, val = line.partition("=")
        if sep:
            env[key.strip()] = val.strip()
    return env


def find_setup_bash(search_root: Path = Path("/opt/ros")) -> list[Path]:
    """ROS 2 distro ``setup.bash`` scripts found under ``search_root``.

    Lets ``setup`` offer to source ROS 2 itself instead of requiring the
    caller to have already done it (see :func:`source_setup_bash`).
    """
    if not search_root.is_dir():
        return []
    return sorted(search_root.glob("*/setup.bash"))


def source_setup_bash(path: Path, timeout: float = 15.0) -> dict[str, str]:
    """Source a ROS 2 ``setup.bash`` in a subshell; return what it changed.

    Only the environment variables that differ from this process's current
    ``os.environ`` are returned (not the subshell's full environment), so the
    caller can merge just the ROS-related additions without picking up
    unrelated bash-internal noise.

    This is what lets ``sudo ros-fairy-setup`` work as a single command: sudo's
    ``env_reset`` (and a ``secure_path`` that usually excludes
    ``/opt/ros/<distro>/bin``) means a root shell often can't even resolve
    ``ros2`` unless ROS was sourced *before* sudo stripped the environment —
    so the historical workaround was ``sudo su`` + manually sourcing. Doing
    the sourcing here, in Python, after root is already established, removes
    that step.

    Only additions/changes are detected — a variable the script *unsets*
    isn't reported (it's simply absent from ``after``, indistinguishable from
    "never touched"), so a stale value from an earlier call in the same
    process wouldn't be cleared by ``os.environ.update(changed)``. Harmless
    for the one-shot-per-process way this is actually called (``setup``
    sources at most once), but not a general-purpose environment diff.
    """
    script = f"source {shlex.quote(str(path))} && env -0"
    result = subprocess.run(["bash", "-c", script], capture_output=True,
                            timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace").strip()
                           or f"sourcing {path} failed")
    after: dict[str, str] = {}
    for entry in result.stdout.split(b"\0"):
        if not entry:
            continue
        key, _, val = entry.decode(errors="replace").partition("=")
        after[key] = val
    return {k: v for k, v in after.items() if os.environ.get(k) != v}


def read_file(path: Path) -> dict[str, str]:
    """Parsed env file, or ``{}`` if it is absent or unreadable."""
    try:
        return parse(path.read_text())
    except OSError:
        return {}


def write_file(path: Path, env: dict[str, str], mode: int = 0o644) -> None:
    """Write an env file atomically with ``mode``.

    Atomic so a reader (the watchdog) never sees half a file, and by rename
    so a file left by another operator is replaced even when this account
    can't write to it (the spool directory is group-writable).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fsio.atomic_write_text(path, serialize(env), mode=mode)


# Search-path variables a root process must not take from user-writable
# directories: an entry like ~/.local/bin would let that user's files run as
# root (the watchdog resolves `ros2`, `docker` and `bash` through PATH).
ROOT_SAFE_PATH_KEYS = ("PATH", "PYTHONPATH", "LD_LIBRARY_PATH")


def _root_safe_dir(entry: str) -> bool:
    """A directory only root can change: root-owned, not writable by others
    (group-writable only when the group is root too)."""
    try:
        st = os.stat(entry)
    except OSError:
        return False
    if st.st_uid != 0 or st.st_mode & 0o002:
        return False
    return not (st.st_mode & 0o020) or st.st_gid == 0


def root_safe_env(env: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """``env`` with user-writable entries dropped from the search paths, and
    the dropped entries. ``LD_PRELOAD`` is dropped entirely."""
    out = dict(env)
    dropped: list[str] = []
    out.pop("LD_PRELOAD", None)
    for key in ROOT_SAFE_PATH_KEYS:
        if key not in out:
            continue
        entries = [e for e in out[key].split(os.pathsep) if e]
        keep = [e for e in entries if _root_safe_dir(e)]
        dropped += [e for e in entries if e not in keep]
        out[key] = os.pathsep.join(keep)
    return out, dropped
