"""mission_status display. Read-only, instant, no prompts."""

from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ros_fairy.ui.review import human_size
from ros_fairy.utils import fsio, paths
from ros_fairy.utils.topic_health import humanize_duration

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


def watchdog_alive(state: dict | None) -> bool:
    """Whether the watchdog that wrote ``state`` is still running.

    The state file names the process by pid *and* start time, so a stale file
    whose pid now belongs to another process (after a reboot), or to a zombie,
    doesn't count. Files from older versions carry only the pid.
    """
    if state is None or not isinstance(state.get("pid"), int):
        return False
    from ros_fairy.watchdog import recorder_scan
    return _pid_alive(state["pid"]) and recorder_scan.pid_alive(
        state["pid"], state.get("proc_start"))


def assistant_line(state: dict | None) -> str:
    """One plain-language line about the watchdog."""
    if not watchdog_alive(state):
        return "not running — recordings will still work, but background "\
               "details won't be captured"
    try:
        heartbeat = datetime.fromisoformat(state.get("heartbeat_at", ""))
        age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
    except ValueError:
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
        except (KeyError, ValueError):
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

    bags = sorted(p for p in paths.bags_dir().glob("*") if p.is_dir()) \
        if paths.bags_dir().is_dir() else []
    if state and state.get("active_bag_dir"):
        active = Path(state["active_bag_dir"])
        size = human_size(fsio.dir_size_bytes(active)) \
            if active.is_dir() else "?"
        table.add_row("Recording", f"{active.name} — {size} so far, growing")
    elif bags:
        total = sum(fsio.dir_size_bytes(b) for b in bags)
        table.add_row("Recordings waiting",
                      f"{len(bags)} ({human_size(total)}) — run: "
                      f"ros2 fairy mission_close")
    else:
        table.add_row("Recording", "none")

    lines = harvest_lines(state)
    if lines:
        table.add_row("Context captured", "\n".join(lines))
    console.print(Panel(table, title="ros-fairy status", border_style="cyan"))


def status_as_dict(state: dict | None, context: dict | None) -> dict:
    """Machine-readable status for --json (the one sanctioned JSON output)."""
    bags = sorted(str(p) for p in paths.bags_dir().glob("*") if p.is_dir()) \
        if paths.bags_dir().is_dir() else []
    return {
        "assistant": assistant_line(state),
        "watchdog_state": state,
        "mission_context": context,
        "spool_bags": bags,
    }
