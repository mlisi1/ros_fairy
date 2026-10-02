"""Detect rosbag2 recorder processes started outside the spool.

The watchdog's inotify only sees the spool, where ``ros2 fairy mission_record``
records. A plain ``ros2 bag record`` in another terminal lands in the operator's
cwd and is invisible to it. The same goes for the bare recorder node
(``ros2 run rosbag2_transport recorder --ros-args -p storage.uri:=...``), which
takes its output directory from ROS parameters rather than ``-o``. This module scans ``/proc`` for a live rosbag2
*recorder* process and resolves where it is writing — pure ``/proc`` reading, no
ROS and no sourced environment needed, so it stays version-agnostic.

A recorder inside a container lives in another mount namespace: its
``/proc/<pid>/cwd`` names a path that does not exist on the host. Activity is
therefore checked through the ``/proc/<pid>/root`` portal, and the bag directory
is translated to the equivalent host path by matching the recorder's mount table
against our own (``/proc/<pid>/mountinfo``). A container path the host has no
mount for (e.g. the container's private overlay) cannot be harvested; it is
skipped with a one-time plain-language warning pointing at ``ros2 fairy adopt``.

``scan()`` is injected into the :class:`~ros_fairy.watchdog.watchdog.Watchdog` so
tests can fake it; the real implementation never raises (a transient
``/proc`` read race yields a partial result, never a crash).
"""

import logging
import os
import re
from pathlib import Path
from typing import TypedDict

import yaml

from ros_fairy.utils import ros_env

log = logging.getLogger("ros_fairy.watchdog.recorder_scan")

STORAGE_SUFFIXES = (".db3", ".mcap")

# Root of procfs; tests point this at a fabricated tree.
PROC = Path("/proc")

# One-time log keys (per process lifetime), so a recorder we skip on every
# 5-second scan is reported once, not forever.
_reported: set[str] = set()


class FoundRecorder(TypedDict):
    pid: int
    output_dir: Path
    discovery: dict[str, str]


def _report_once(key: str, level: int, msg: str, *args) -> None:
    if key in _reported:
        return
    _reported.add(key)
    log.log(level, msg, *args)


def _read_cmdline(pid: str) -> list[str]:
    try:
        raw = (PROC / pid / "cmdline").read_bytes()
    except OSError:
        return []
    return [tok for tok in raw.decode("utf-8", "replace").split("\0") if tok]


def _is_recorder_node(argv: list[str]) -> bool:
    """True when argv is the bare rosbag2 recorder node executable.

    ``ros2 run rosbag2_transport recorder`` spawns
    ``<prefix>/lib/rosbag2_transport/recorder``; only that child is matched, not
    the ``ros2 run`` wrapper, so one recording yields one process.
    """
    if not argv:
        return False
    exe = Path(argv[0])
    return exe.name == "recorder" and exe.parent.name == "rosbag2_transport"


def _is_record_cmd(argv: list[str]) -> bool:
    """True when argv is a ``... bag record ...`` invocation (not play/info/...)
    or the bare recorder node.

    The verb immediately after ``bag`` is the authoritative discriminator, so an
    output dir or topic named like another verb (e.g. ``-o info``) is not
    mistaken for it.
    """
    if _is_recorder_node(argv):
        return True
    try:
        bag_i = argv.index("bag")
    except ValueError:
        return False
    verb = argv[bag_i + 1] if bag_i + 1 < len(argv) else ""
    return verb == "record"


def _output_arg(argv: list[str]) -> str | None:
    """The value of ``-o`` / ``--output`` (space- or ``=``-separated), if any."""
    for i, tok in enumerate(argv):
        if tok in ("-o", "--output"):
            return argv[i + 1] if i + 1 < len(argv) else None
        if tok.startswith("--output="):
            return tok.split("=", 1)[1]
        if tok.startswith("-o="):
            return tok.split("=", 1)[1]
    return None


def _ros_args(argv: list[str]) -> list[str]:
    """The tokens inside ``--ros-args`` blocks (up to each closing ``--``)."""
    out, inside = [], False
    for tok in argv:
        if tok == "--ros-args":
            inside = True
        elif tok == "--":
            inside = False
        elif inside:
            out.append(tok)
    return out


def _param_uri(tokens: list[str]) -> str | None:
    """``storage.uri`` from ``-p``/``--param storage.uri:=X`` (last one wins)."""
    uri = None
    for i, tok in enumerate(tokens[:-1]):
        if tok in ("-p", "--param"):
            name, sep, val = tokens[i + 1].partition(":=")
            if sep and name == "storage.uri":
                uri = val
    return uri


def _params_file_uri(path: Path) -> str | None:
    """``storage.uri`` from a ROS params file, under any node key.

    Accepts both the nested (``storage: {uri: X}``) and the flat dotted
    (``storage.uri: X``) spelling.
    """
    try:
        doc = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(doc, dict):
        return None
    for node in doc.values():
        params = node.get("ros__parameters") if isinstance(node, dict) else None
        if not isinstance(params, dict):
            continue
        storage = params.get("storage")
        uri = storage.get("uri") if isinstance(storage, dict) else None
        uri = uri or params.get("storage.uri")
        if isinstance(uri, str) and uri:
            return uri
    return None


def _node_output(argv: list[str], cwd: Path, view=lambda p: p) -> str | None:
    """The recorder node's ``storage.uri``: a ``-p`` override beats params files.

    Params-file paths are in the recorder's namespace, so they are read
    through ``view``.
    """
    tokens = _ros_args(argv)
    uri = _param_uri(tokens)
    if uri:
        return uri
    files = [tokens[i + 1] for i, tok in enumerate(tokens[:-1])
             if tok == "--params-file"]
    for f in reversed(files):  # later files override earlier ones
        p = Path(f)
        uri = _params_file_uri(view(p if p.is_absolute() else cwd / p))
        if uri:
            return uri
    return None


def _proc_cwd(pid: str) -> Path | None:
    try:
        return Path(os.readlink(PROC / pid / "cwd"))
    except OSError:
        return None


def _mnt_ns(pid: str) -> int | None:
    """The mount-namespace identity of a process, or None if unreadable."""
    try:
        return (PROC / pid / "ns" / "mnt").stat().st_ino
    except OSError:
        return None


def _portal(pid: str, path: Path) -> Path:
    """``path`` (in the recorder's mount namespace) as readable from ours.

    ``/proc/<pid>/root`` is the kernel's window into the process's own mount
    namespace; it works only while the process is alive, so it is used for
    liveness checks, never stored.
    """
    return PROC / pid / "root" / path.relative_to("/")


def _discovery_env(pid: str) -> dict[str, str]:
    """DDS discovery keys from the recorder's own environment.

    Only :data:`ros_env.SESSION_ADOPT_KEYS` are returned — never loader paths —
    so adopting them into the root watchdog cannot load code (same rule as
    ``session.env``). The recorder is on the partition we want to harvest, so its
    environment is the authoritative source.
    """
    try:
        raw = (PROC / pid / "environ").read_bytes()
    except OSError:
        return {}
    env: dict[str, str] = {}
    for entry in raw.decode("utf-8", "replace").split("\0"):
        key, sep, val = entry.partition("=")
        if sep and key in ros_env.SESSION_ADOPT_KEYS:
            env[key] = val
    return env


# A recorder started with this set (to anything but "" or "0") is not
# captured: an operator's throwaway recording, or a test suite running on a
# robot, whose bags would otherwise join the open mission (2026-10-02).
# Module-level so tests that exercise the scanner itself can rename it.
IGNORE_ENV = "ROS_FAIRY_IGNORE"


def _opted_out(pid: str) -> bool:
    try:
        raw = (PROC / pid / "environ").read_bytes()
    except OSError:
        return False
    prefix = IGNORE_ENV.encode() + b"="
    return any(entry.startswith(prefix) and entry[len(prefix):] not in
               (b"", b"0") for entry in raw.split(b"\0"))


def _is_active_bag(bag_dir: Path) -> bool:
    """A directory currently being recorded: storage file present, no metadata.

    rosbag2 writes ``metadata.yaml`` only on close, so its absence (with a
    storage file present) marks a live recording — and lets the existing
    finalise machinery take over once it appears.
    """
    try:
        if (bag_dir / "metadata.yaml").is_file():
            return False
        return any(f.name.endswith(STORAGE_SUFFIXES) for f in bag_dir.iterdir())
    except OSError:
        return False


def _resolve_output(argv: list[str], cwd: Path,
                    view=lambda p: p) -> Path | None:
    """The bag directory the recorder is writing into, or None if not yet known.

    The result is in the *recorder's* namespace; ``view`` maps such a path to
    one readable from ours (identity on the host, the ``/proc/<pid>/root``
    portal for a containerised recorder).
    """
    arg = _node_output(argv, cwd, view) if _is_recorder_node(argv) \
        else _output_arg(argv)
    if arg is not None:
        bag_dir = Path(arg)
        if not bag_dir.is_absolute():
            bag_dir = cwd / bag_dir
        return bag_dir if _is_active_bag(view(bag_dir)) else None
    # No -o: rosbag2 creates rosbag2_<timestamp>/ in the cwd. Pick the active one.
    try:
        candidates = [d for d in view(cwd).glob("rosbag2_*")
                      if d.is_dir() and _is_active_bag(d)]
        if not candidates:
            return None
        newest = max(candidates, key=lambda d: d.stat().st_mtime)
    except OSError:
        return None
    return cwd / newest.name


_MOUNTINFO_ESC = re.compile(r"\\([0-7]{3})")


def _unescape(field: str) -> str:
    """Undo mountinfo's octal escaping (``\\040`` for space, etc.)."""
    return _MOUNTINFO_ESC.sub(lambda m: chr(int(m.group(1), 8)), field)


def _parse_mountinfo(text: str) -> list[tuple[Path, str, Path]]:
    """(mount point, major:minor, root-within-filesystem) per mountinfo line."""
    entries = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        entries.append((Path(_unescape(fields[4])), fields[2],
                        Path(_unescape(fields[3]))))
    return entries


def _host_path(path: Path, pid_mountinfo: str,
               self_mountinfo: str) -> Path | None:
    """Translate a recorder-namespace path to the equivalent path in ours.

    The mount the path lives on (longest mount-point prefix in the recorder's
    table) names a filesystem by device number and a root within it; any of our
    own mounts of the same filesystem whose root prefixes that location can
    reach it. None when we have no such mount — the path is container-private.
    """
    def longest_prefix(entries, target, key):
        best = None
        for entry in entries:
            prefix = key(entry)
            if prefix == target or prefix in target.parents:
                if best is None or len(prefix.parts) > len(key(best).parts):
                    best = entry
        return best

    src = longest_prefix(_parse_mountinfo(pid_mountinfo), path,
                         key=lambda e: e[0])
    if src is None:
        return None
    mount_point, device, fs_root = src
    within_fs = fs_root / path.relative_to(mount_point)
    ours = [e for e in _parse_mountinfo(self_mountinfo) if e[1] == device]
    dst = longest_prefix(ours, within_fs, key=lambda e: e[2])
    if dst is None:
        return None
    our_mount_point, _, our_root = dst
    return our_mount_point / within_fs.relative_to(our_root)


def _translate(pid: str, bag_dir: Path) -> Path | None:
    """Host-visible equivalent of a containerised recorder's bag dir, or None."""
    try:
        pid_mi = (PROC / pid / "mountinfo").read_text()
        self_mi = (PROC / "self" / "mountinfo").read_text()
    except OSError:
        return None
    host = _host_path(bag_dir, pid_mi, self_mi)
    if host is None or not host.is_dir():
        return None
    try:  # both names must be the same directory, not a lookalike
        if host.stat().st_ino != _portal(pid, bag_dir).stat().st_ino:
            return None
    except OSError:
        pass  # portal race (recorder just exited): trust the mount table
    return host


def scan() -> list[FoundRecorder]:
    """Every live rosbag2 recorder whose output directory can be resolved."""
    found: list[FoundRecorder] = []
    try:
        pids = [p for p in os.listdir(PROC) if p.isdigit()]
    except OSError:
        return found
    own_ns = _mnt_ns("self")
    for pid in pids:
        argv = _read_cmdline(pid)
        if not _is_record_cmd(argv):
            continue
        if _opted_out(pid):
            _report_once(f"ignored:{pid}", logging.INFO,
                         "recorder process %s has %s set; not capturing it",
                         pid, IGNORE_ENV)
            continue
        cwd = _proc_cwd(pid)
        if cwd is None:
            continue
        rec_ns = _mnt_ns(pid)
        foreign_ns = None not in (own_ns, rec_ns) and rec_ns != own_ns
        view = (lambda p, pid=pid: _portal(pid, p)) if foreign_ns \
            else (lambda p: p)
        bag_dir = _resolve_output(argv, cwd, view)
        if bag_dir is None:
            _report_once(f"unresolved:{pid}", logging.INFO,
                         "recorder process %s found but its output directory "
                         "is not visible yet; will keep checking", pid)
            continue
        if foreign_ns:
            host_dir = _translate(pid, bag_dir)
            if host_dir is None:
                _report_once(
                    f"unreachable:{bag_dir}", logging.WARNING,
                    "a recording (%s, pid %s) is running inside a container "
                    "at a folder this computer cannot see; record into a "
                    "shared folder, or run 'ros2 fairy adopt' on the finished "
                    "recording after copying it out", bag_dir, pid)
                continue
            bag_dir = host_dir
        found.append(FoundRecorder(pid=int(pid),
                                   output_dir=bag_dir.resolve(),
                                   discovery=_discovery_env(pid)))
    return found


def pid_alive(pid: int) -> bool:
    """Whether a recorder process is still running (used as a finalise hint)."""
    return (PROC / str(pid)).exists()
