"""ros2 fairy doctor — preflight self-check.

Answers "is this robot ready to capture a FAIR mission right now?" before the
operator commits to a run, catching the failure modes that otherwise only show
up as an empty or unusable archive afterwards: the watchdog not running, ROS
unreachable from the *service* context, an unsynchronised clock, missing mcap,
no robot identity, or no disk. Read-only; never changes anything.

Each check yields {status, title, detail, hint}. Exit code is 1 if any check
FAILs (WARN/SKIP do not fail), so it is usable in scripts and `--json`.
"""

import json
import os
import shutil
from datetime import datetime, timezone

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ros_fairy.subcommands import VerbExtension, _configure_logging, guarded_main
from ros_fairy.utils import clock, paths, ros_env

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
_GLYPH = {OK: "[green]✓[/green]", WARN: "[yellow]![/yellow]",
          FAIL: "[red]✗[/red]", SKIP: "[dim]–[/dim]"}

MIN_FREE_BYTES = 1 << 30  # 1 GiB, matching mission_record's preflight


def _check_identity() -> dict:
    from ros_fairy.harvest import robot_identity
    try:
        data = robot_identity.harvest()
    except robot_identity.RobotIdentityError as exc:
        return {"status": FAIL, "title": "Robot is not set up",
                "detail": str(exc),
                "hint": "run `ros2 fairy setup` (as an engineer) to create "
                        "/etc/ros-fairy/robot_identity.yaml"}
    robot = data.get("robot") or {}
    n = len(data.get("sensors", []))
    return {"status": OK, "title": "Robot identity configured",
            "detail": f"{robot.get('name', '?')} — {n} sensor(s) declared",
            "hint": ""}


def _check_watchdog() -> dict:
    from ros_fairy.ui.status import STALE_HEARTBEAT_S, _pid_alive
    from ros_fairy.watchdog import watchdog as wd
    state = wd.read_state()
    if state is None or not _pid_alive(state.get("pid")):
        return {"status": FAIL, "title": "Recording assistant is not running",
                "detail": "the watchdog service is down",
                "hint": "start it: `sudo systemctl enable --now "
                        "ros-fairy-watchdog`"}
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(state.get("heartbeat_at", ""))
               ).total_seconds()
    except ValueError:
        age = None
    # The watchdog heartbeats only while RECORDING; while IDLE the state
    # file is rewritten only on transitions, so age says nothing there.
    if state.get("state") == "RECORDING" and \
            age is not None and age > STALE_HEARTBEAT_S:
        return {"status": WARN, "title": "Recording assistant is not responding",
                "detail": f"last heartbeat {int(age)}s ago",
                "hint": "check `systemctl status ros-fairy-watchdog`"}
    return {"status": OK, "title": "Recording assistant is running",
            "detail": f"state: {state.get('state', '?').lower()}", "hint": ""}


def _check_ros_reachable() -> dict:
    from ros_fairy.harvest import ros_graph
    try:
        nodes = ros_graph.list_nodes()
    except ros_graph.RosGraphError as exc:
        msg = str(exc)
        if "no ROS nodes visible" in msg:
            nodes = []
        elif "not found" in msg or "rclpy is not available" in msg:
            return {"status": FAIL, "title": "ROS 2 is not on PATH",
                    "detail": msg,
                    "hint": "source your ROS 2 environment, e.g. "
                            "`source /opt/ros/<distro>/setup.bash`"}
        else:
            return {"status": FAIL, "title": "ROS 2 is not reachable",
                    "detail": msg, "hint": "is the robot software started?"}
    if not nodes:
        return {"status": WARN, "title": "ROS 2 works but no nodes are running",
                "detail": "no nodes are visible on the ROS graph",
                "hint": "start the robot software before recording, or check "
                        "ROS_DOMAIN_ID / RMW_IMPLEMENTATION match it"}
    return {"status": OK, "title": "ROS 2 graph is reachable",
            "detail": f"{len(nodes)} node(s) visible", "hint": ""}


def _check_ros_environment() -> dict:
    distro = os.environ.get("ROS_DISTRO")
    if not distro:
        return {"status": WARN, "title": "ROS environment not fully sourced",
                "detail": "ROS_DISTRO is unset in this shell",
                "hint": "source ROS so `setup` can capture it for the service"}
    bits = [f"distro: {distro}"]
    for key, label in (("RMW_IMPLEMENTATION", "rmw"),
                       ("ROS_DOMAIN_ID", "domain")):
        if os.environ.get(key):
            bits.append(f"{label}: {os.environ[key]}")
    return {"status": OK, "title": "ROS environment sourced",
            "detail": ", ".join(bits), "hint": ""}


def _check_service_env() -> dict:
    """The *service's* effective ROS env — not just this shell's.

    The watchdog starts from a frozen snapshot (``watchdog.env``) captured at
    setup. That goes blind when the file is missing/stale or when the operator
    later records under a different ROS_DOMAIN_ID / RMW than setup saw — a robot
    that passes every shell-context check while the service is on the wrong DDS
    partition and harvests an empty graph (issue #29).
    """
    env = ros_env.read_file(paths.watchdog_env_path())
    setup_hint = "re-run setup: `ros2 fairy setup`"
    if "ROS_DISTRO" not in env:
        missing = "missing" if not env else "has no ROS_DISTRO"
        return {"status": FAIL,
                "title": "Background service has no ROS environment",
                "detail": f"{paths.watchdog_env_path()} {missing}",
                "hint": setup_hint}
    drift = []
    for key in ros_env.DISCOVERY_KEYS:
        service_val = env.get(key, "")
        shell_val = os.environ.get(key, "")
        if key == "ROS_DOMAIN_ID":  # an unset domain means 0
            service_val, shell_val = service_val or "0", shell_val or "0"
        if shell_val and service_val != shell_val:
            drift.append(f"{key} {service_val or 'unset'} ≠ {shell_val} here")
    if drift:
        return {"status": WARN,
                "title": "Background service is on a different ROS partition",
                "detail": "; ".join(drift),
                "hint": "a mission started with `ros2 fairy mission_start` "
                        "reconciles this automatically; otherwise " + setup_hint}
    return {"status": OK, "title": "Background service ROS environment matches",
            "detail": f"distro: {env['ROS_DISTRO']}", "hint": ""}


def _check_service_harvest() -> dict:
    """What the watchdog's own (service-context) harvest last achieved.

    This is the check that distinguishes "ROS works in my shell" from "the
    background service can actually see ROS" — the exact failure that produced
    empty archives on the real robot.
    """
    from ros_fairy.watchdog import watchdog as wd
    state = wd.read_state()
    status = (state or {}).get("harvest_status", {})
    if not status:
        return {"status": SKIP, "title": "Service has not harvested yet",
                "detail": "no harvest recorded since the watchdog started",
                "hint": "start a short recording to trigger a harvest"}
    # This is history — what the service saw during the last recording —
    # not the graph right now (that's _check_ros_reachable). Say when, so
    # "no nodes running now" next to it doesn't read as a contradiction.
    when = _when(state.get("harvest_captured_at"))
    count = state.get("harvest_node_count")
    graph = status.get("ros_graph")
    if graph in ("ok", "partial"):
        detail = f"last capture{when} saw {count} node(s)" if count \
            else f"last capture{when} succeeded"
        if graph == "partial":
            detail += "; a few nodes' parameters were not captured"
        return {"status": OK, "title": "Background service can reach ROS",
                "detail": detail, "hint": ""}
    return {"status": FAIL,
            "title": "Background service could not reach ROS",
            "detail": f"last capture{when}: {graph}",
            "hint": "if the robot software was running at that time, the "
                    "service may lack a ROS environment — re-run "
                    "`ros2 fairy setup`"}


def _when(iso: str | None) -> str:
    """" at 10:22" (today) / " on 01 Oct, 17:20"; "" if unknown."""
    try:
        t = datetime.fromisoformat(iso).astimezone()
    except (TypeError, ValueError):
        return ""
    if t.date() == datetime.now().astimezone().date():
        return f" at {t:%H:%M}"
    return f" on {t:%d %b, %H:%M}"


def _check_clock() -> dict:
    synced = clock.is_synchronized()
    if synced is True:
        return {"status": OK, "title": "System clock is synchronised",
                "detail": "", "hint": ""}
    if synced is False:
        return {"status": FAIL, "title": "System clock is not synchronised",
                "detail": "recordings will be stamped with the wrong time",
                "hint": "wait for NTP/chrony to sync before recording; see "
                        "docs/recovering-bad-clock-bags.md"}
    return {"status": SKIP, "title": "Clock sync status unknown",
            "detail": "`timedatectl` not available", "hint": ""}


def _check_mcap() -> dict:
    from ros_fairy.utils import bag_storage
    if bag_storage.supports_timestamps("mcap"):
        return {"status": OK, "title": "MCAP support available",
                "detail": "bag timing and health analysis enabled", "hint": ""}
    return {"status": WARN, "title": "MCAP support missing",
            "detail": "the 'mcap' package is not installed",
            "hint": "install it: `pip install mcap` (bag duration and health "
                    "checks need it on Jazzy)"}


def _check_disk() -> dict:
    # The spool may not exist yet (pre-setup); measure the nearest existing
    # ancestor so the check still reports the right filesystem.
    target = paths.spool_dir()
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        free = shutil.disk_usage(target).free
    except OSError as exc:
        return {"status": SKIP, "title": "Disk space unknown",
                "detail": str(exc), "hint": ""}
    from ros_fairy.ui.review import human_size
    if free < MIN_FREE_BYTES:
        return {"status": FAIL, "title": "Very low disk space",
                "detail": f"{human_size(free)} free in the spool",
                "hint": "free up space before recording"}
    return {"status": OK, "title": "Disk space is adequate",
            "detail": f"{human_size(free)} free", "hint": ""}


def _check_docker() -> dict:
    from ros_fairy.harvest import docker_info
    try:
        available = docker_info.harvest().get("available", False)
    except Exception:
        available = False
    if available:
        return {"status": OK, "title": "Docker is reachable",
                "detail": "container software will be recorded", "hint": ""}
    return {"status": SKIP, "title": "Docker not in use",
            "detail": "no Docker daemon (fine if this robot doesn't use it)",
            "hint": ""}


def _check_archive() -> dict:
    from ros_fairy.archive import assembler
    broken = assembler.find_incomplete_crates()
    if not broken:
        return {"status": OK, "title": "Saved missions are complete",
                "detail": "", "hint": ""}
    n = len(broken)
    return {"status": WARN,
            "title": f"{n} incomplete mission save{'s' if n != 1 else ''}",
            "detail": ", ".join(p.name for p in broken),
            "hint": "a save was cut off (e.g. power loss) — these are missing "
                    "from `ros2 fairy list`. Recordings made outside ros-fairy "
                    "are still where they were recorded and can be attached "
                    "to a mission with `ros2 fairy adopt`. If you don't need "
                    "them, delete the folder"
                    f"{'s' if n != 1 else ''} to clear this: "
                    + " ".join(f"`rm -r {p}`" for p in broken)}


_CHECKS = (_check_identity, _check_watchdog, _check_ros_reachable,
           _check_ros_environment, _check_service_env, _check_service_harvest,
           _check_clock, _check_mcap, _check_disk, _check_docker, _check_archive)


def diagnose() -> list[dict]:
    """Run every check; a check that raises becomes a FAIL rather than a crash."""
    results = []
    for check in _CHECKS:
        try:
            results.append(check())
        except Exception as exc:  # a broken check must not break doctor
            results.append({"status": FAIL, "title": f"Check failed: {check.__name__}",
                            "detail": str(exc), "hint": ""})
    return results


def _overall(checks: list[dict]) -> str:
    if any(c["status"] == FAIL for c in checks):
        return FAIL
    if any(c["status"] == WARN for c in checks):
        return WARN
    return OK


def _render(console: Console, checks: list[dict]) -> None:
    table = Table.grid(padding=(0, 1))
    table.add_column(width=1)
    table.add_column()
    for check in checks:
        detail = f" [dim]({check['detail']})[/dim]" if check["detail"] else ""
        table.add_row(_GLYPH[check["status"]], check["title"] + detail)
        if check["status"] in (FAIL, WARN) and check.get("hint"):
            table.add_row("", f"  [dim]→ {check['hint']}[/dim]")
    overall = _overall(checks)
    title = {OK: "[green]READY[/green]",
             WARN: "[yellow]READY (with warnings)[/yellow]",
             FAIL: "[red]NOT READY[/red]"}[overall]
    border = {OK: "green", WARN: "yellow", FAIL: "red"}[overall]
    console.print(Panel(table, title=f"ros-fairy doctor — {title}",
                        border_style=border))


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()

    checks = diagnose()
    overall = _overall(checks)

    if getattr(args, "json", False):
        print(json.dumps({"result": overall, "checks": checks}, indent=2))
    else:
        _render(console, checks)

    return 1 if overall == FAIL else 0


class DoctorVerb(VerbExtension):
    """Check that this robot is ready to capture a mission."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument(
            "--json", action="store_true",
            help="machine-readable output for scripts")
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        return guarded_main(run, args)
