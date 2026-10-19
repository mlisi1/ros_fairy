"""mission_status display. Read-only, instant, no prompts."""

from datetime import datetime, timezone
from pathlib import Path
from typing import TypeGuard

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from fairy.ui.review import human_size
from fairy.utils import fsio, paths
from fairy.utils.topic_health import humanize_duration

STALE_HEARTBEAT_S = 300

_MODULE_LABELS = {
    "robot_identity": "robot identity",
    "system_info": "computer details",
    "python_env": "Python environment",
    "hardware_devices": "connected hardware",
    "ros_graph": "software versions and settings",
    "ros_descriptions": "robot description",
    "docker_info": "container software",
}


def _pid_alive(pid) -> bool:
    return isinstance(pid, int) and Path(f"/proc/{pid}").exists()


def watchdog_alive(state: dict | None) -> TypeGuard[dict]:
    """Whether the watchdog that wrote ``state`` is still running.

    The state file names the process by pid *and* start time, so a stale file
    whose pid now belongs to another process (after a reboot), or to a zombie,
    doesn't count. Files from older versions carry only the pid.
    """
    if state is None or not isinstance(state.get("pid"), int):
        return False
    from fairy.watchdog import recorder_scan
    return _pid_alive(state["pid"]) and recorder_scan.pid_alive(
        state["pid"], state.get("proc_start"))


def assistant_line(state: dict | None) -> str:
    """One plain-language line about the watchdog."""
    if not watchdog_alive(state):
        if not paths.robot_identity_path().is_file():
            return "not installed — this robot isn't set up yet (ask your "\
                   "engineer to run: ros2 fairy setup)"
        return "not running — recordings will still work, but background "\
               "details won't be captured"
    try:
        heartbeat = datetime.fromisoformat(state.get("heartbeat_at", ""))
        age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
    except (TypeError, ValueError):
        age = None
    if state.get("state") == "RECORDING":
        if age is not None and age > STALE_HEARTBEAT_S:
            return "not responding — ask your engineer to check it"
        since = ""
        try:
            started = datetime.fromisoformat(state["since"]).astimezone()
            elapsed = (datetime.now(timezone.utc) -
                       datetime.fromisoformat(state["since"])).total_seconds()
            since = (f" (started {started.strftime('%H:%M')}, "
                     f"{humanize_duration(elapsed)} ago)")
        except (KeyError, TypeError, ValueError):
            pass
        return f"recording{since}"
    if state.get("state") == "FINALISING":
        return "wrapping up the last recording"
    return "watching — ready for the next recording"


def harvest_lines(state: dict | None) -> list[str]:
    if not state or not state.get("harvest_status"):
        return []
    lines = []
    for module, result in state["harvest_status"].items():
        label = _MODULE_LABELS.get(module, module)
        if result == "ok":
            lines.append(f"✓ {label}")
        elif result == "partial":
            lines.append(f"⚠ {label} (partial)")
        elif result in ("skipped", "absent"):
            lines.append(f"– {label} (not used on this robot)")
        else:
            lines.append(f"✗ {label} — will keep trying")
    return lines


def show_status(state: dict | None, context: dict | None,
                console: Console | None = None) -> None:
    console = console or Console()
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Assistant", assistant_line(state))

    if context:
        identity = context.get("identity", {})
        intent = context.get("intent", {})
        table.add_row("Briefing", f"{identity.get('operator_name', '?')} — "
                                  f"{intent.get('goal', '?')}")
    else:
        table.add_row("Briefing", "not started yet — run: "
                                  "ros2 fairy mission_start")

    active = _live_recording(state)
    waiting = [r for r in recording_list() if r["path"] != active]
    if active is not None:
        written = fsio.dir_size_bytes(active) if active.is_dir() else None
        if written is None:
            progress = "size unknown"
        elif written < 1000:
            # rosbag2 writes in batches: the first one lands after a while.
            progress = "started, nothing written to disk yet"
        else:
            progress = f"{human_size(written)} so far, growing"
        table.add_row("Recording", f"{active.name} — {progress}")
    if waiting:
        total = sum(fsio.dir_size_bytes(r["path"]) for r in waiting
                    if r["path"].is_dir())
        table.add_row("Recordings waiting",
                      f"{len(waiting)} ({human_size(total)}) — run: "
                      f"ros2 fairy mission_close")
        table.add_row("", "\n".join(recording_line(r) for r in waiting))
        table.add_row("", "[dim]take one out with: ros2 fairy drop <#>[/dim]")
    elif active is None:
        table.add_row("Recording", "none")

    lines = harvest_lines(state)
    if lines:
        table.add_row("Context captured", "\n".join(lines))
    console.print(Panel(table, title="ros-fairy status", border_style="cyan"))


def _live_recording(state: dict | None) -> Path | None:
    """The bag being recorded right now — only if the watchdog that says so
    is really running (a crashed one leaves RECORDING in its state file)."""
    if not state or state.get("state") != "RECORDING" or \
            not state.get("active_bag_dir") or not watchdog_alive(state):
        return None
    return Path(state["active_bag_dir"])


def mission_recordings() -> list[Path]:
    """Every recording of the open mission: the spool's, and those made
    outside ros-fairy (record_all on Jo) that the watchdog listed."""
    from fairy.manifest import builder
    found = sorted(p for p in paths.bags_dir().glob("*") if p.is_dir()) \
        if paths.bags_dir().is_dir() else []
    harvest = builder.load_spool()[0]
    for bag in (harvest or {}).get("bags", []):
        path = Path(bag.get("path", ""))
        if path not in found:
            found.append(path)
    return found


def recording_list() -> list[dict]:
    """The open mission's recordings, in mission_recordings' order: path,
    whether it's in the spool, its harvest record (None until the watchdog
    has processed it) and its number (``recording_id``, None until then)."""
    from fairy.manifest import builder
    records = {b.get("path"): b
               for b in (builder.load_spool()[0] or {}).get("bags", [])}
    found = []
    for path in mission_recordings():
        record = records.get(str(path))
        found.append({"path": path, "in_spool": path.parent == paths.bags_dir(),
                      "record": record,
                      "id": (record or {}).get("recording_id")})
    return found


def recording_line(rec: dict) -> str:
    """One recording for people: #number, start, length, size, and where
    it is when it isn't in the spool."""
    record = rec["record"] or {}
    number = f"#{rec['id']}" if rec["id"] else "– "
    bits = []
    if record.get("start_time"):
        try:
            bits.append(datetime.fromisoformat(record["start_time"])
                        .astimezone().strftime("%H:%M:%S"))
        except ValueError:
            pass
    if record.get("duration_s"):
        bits.append(humanize_duration(record["duration_s"]))
    try:
        bits.append(human_size(fsio.dir_size_bytes(rec["path"])))
    except OSError:
        bits.append("missing")
    if not rec["record"]:
        bits.append("being processed")
    if not rec["in_spool"]:
        bits.append(str(rec["path"]))
    return f"{number:>3}  " + ", ".join(bits)


def status_as_dict(state: dict | None, context: dict | None) -> dict:
    """Machine-readable status for --json (the one sanctioned JSON output)."""
    bags = sorted(str(p) for p in paths.bags_dir().glob("*") if p.is_dir()) \
        if paths.bags_dir().is_dir() else []
    active = _live_recording(state)
    return {
        "assistant": assistant_line(state),
        "watchdog_state": state,
        "mission_context": context,
        "spool_bags": bags,
        "recordings": [str(p) for p in mission_recordings()],
        "recording_ids": {str(r["path"]): r["id"] for r in recording_list()
                          if r["id"]},
        "recording_now": str(active) if active else None,
    }
