"""The DDS settings the robot's containerised ROS processes really run with.

``docker inspect`` knows a container's configuration (the image's ENV,
``-e``, compose ``environment:``), not what its start-up scripts export: an
entrypoint that runs ``export ROS_DOMAIN_ID=7`` before starting the nodes is
invisible to it. The processes' own environment (``/proc/<pid>/environ``)
has it, but only root may read another account's, and the operators'
``doctor`` and ``setup`` aren't root. So the watchdog (root) scans the
running processes every :data:`SCAN_INTERVAL_S` and writes what it finds to
``ros_processes.json`` in the data folder, world-readable: per container,
the discovery settings (:data:`ros_env.SESSION_ADOPT_KEYS`) of the processes
that have ROS in their environment. Nothing else from their environment is
kept.

A container is named by the host pid of its first process (pid 1 inside
it), which is what ``docker inspect`` reports as ``State.Pid``. Processes
in the watchdog's own pid namespace (the host's) are left out: shells on the
host are compared with the service directly.

Pure ``/proc`` reading; :func:`scan` never raises.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from ros_fairy.utils import fsio, paths, ros_env

log = logging.getLogger("ros_fairy.watchdog.ros_processes")

SCAN_INTERVAL_S = 10
# Root of procfs; tests point this at a fabricated tree.
PROC = Path("/proc")
# Started by any `ros2` command, with whatever environment that shell had:
# not the robot's stack.
_IGNORED = ("_ros2_daemon",)


def _environ(pid: str) -> dict[str, str] | None:
    try:
        raw = (PROC / pid / "environ").read_bytes()
    except OSError:
        return None
    env = {}
    for entry in raw.decode("utf-8", "replace").split("\0"):
        key, sep, value = entry.partition("=")
        if sep:
            env[key] = value
    return env


def _namespace(pid: str) -> str | None:
    try:
        return os.readlink(PROC / pid / "ns" / "pid")
    except OSError:
        return None


def _inner_pid(pid: str) -> str | None:
    """The process's pid in its own (innermost) namespace."""
    try:
        for line in (PROC / pid / "status").read_text().splitlines():
            if line.startswith("NSpid:"):
                return line.split()[-1]
    except OSError:
        pass
    return None


def _ignored(pid: str) -> bool:
    try:
        cmdline = (PROC / pid / "cmdline").read_bytes()
    except OSError:
        return True
    return any(name.encode() in cmdline for name in _IGNORED)


def scan() -> list[dict]:
    """``[{"pid": <container's first process>, "settings": [<distinct
    discovery settings>], "processes": <how many>}]``, by pid."""
    own = _namespace("self")
    by_ns: dict[str, dict] = {}
    try:
        pids = [p for p in os.listdir(PROC) if p.isdigit()]
    except OSError:
        return []
    for pid in pids:
        ns = _namespace(pid)
        if ns is None or ns == own:
            continue
        entry = by_ns.setdefault(ns, {"pid": None, "settings": [],
                                      "processes": 0})
        if _inner_pid(pid) == "1":
            entry["pid"] = int(pid)
        env = _environ(pid)
        if not env or "ROS_DISTRO" not in env or _ignored(pid):
            continue
        settings = {k: env[k] for k in ("ROS_DISTRO",
                                        *ros_env.SESSION_ADOPT_KEYS)
                    if k in env}
        if settings not in entry["settings"]:
            entry["settings"].append(settings)
        entry["processes"] += 1
    found = [e for e in by_ns.values() if e["processes"] and e["pid"]]
    for entry in found:
        entry["settings"].sort(key=lambda s: sorted(s.items()))
    return sorted(found, key=lambda e: e["pid"])


def write_if_changed(found: list[dict], path: Path | None = None) -> bool:
    """Write ``found`` unless the file already says the same; True when
    written. Rewriting only on a change keeps an SD card from being worn
    every ten seconds."""
    path = path or paths.ros_processes_path()
    try:
        if json.loads(path.read_text()).get("containers") == found:
            return False
    except (OSError, ValueError, AttributeError):
        pass
    doc = {"containers": found,
           "since": datetime.now(timezone.utc).isoformat()}
    try:
        fsio.atomic_write_text(path, json.dumps(doc, indent=2) + "\n",
                               mode=0o644)
    except OSError:
        log.warning("couldn't write %s", path, exc_info=True)
        return False
    return True


def read(path: Path | None = None) -> dict[int, list[dict]]:
    """What the watchdog last wrote: container pid -> distinct settings.
    Empty when there is no file (yet) or it can't be read."""
    path = path or paths.ros_processes_path()
    try:
        doc = json.loads(path.read_text())
        return {int(c["pid"]): list(c["settings"])
                for c in doc.get("containers") or []}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}
