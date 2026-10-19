"""ros2 fairy doctor — preflight self-check.

Answers "is this robot ready to capture a FAIR mission right now?" before the
operator commits to a run, catching the failure modes that otherwise only show
up as an empty or unusable archive afterwards: the watchdog not running, ROS
unreachable from the *service* context, an unsynchronised clock, missing mcap,
no robot identity, or no disk. Read-only; never changes anything.

Each check yields {status, title, detail, hint}. Exit code is 1 if any check
FAILs (WARN/SKIP do not fail), so it is usable in scripts and `--json`.
"""

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ros_fairy.subcommands import (
    VerbExtension,
    _configure_logging,
    guarded_main,
    print_json,
)
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
        path = paths.robot_identity_path()
        if path.is_file():
            return {"status": FAIL,
                    "title": "The robot configuration has a mistake",
                    "detail": str(exc),
                    "hint": "fix it with the `ros2 fairy setup` flags (e.g. "
                            "--set-sensor; see `ros2 fairy setup -h`), or "
                            f"by hand in {path} (with sudo)"}
        return {"status": FAIL, "title": "Robot is not set up",
                "detail": str(exc),
                "hint": "run `ros2 fairy setup` (as an engineer) to create "
                        f"{path}"}
    robot = data.get("robot") or {}
    n = len(data.get("sensors", []))
    return {"status": OK, "title": "Robot identity configured",
            "detail": f"{robot.get('name', '?')} — {n} sensor(s) declared",
            "hint": ""}


def _check_watchdog() -> dict:
    from ros_fairy.ui.status import STALE_HEARTBEAT_S, watchdog_alive
    from ros_fairy.watchdog import watchdog as wd
    state = wd.read_state()
    if not watchdog_alive(state):
        return {"status": FAIL, "title": "Recording assistant is not running",
                "detail": "the watchdog service is down",
                "hint": "start it: `sudo systemctl enable --now "
                        "ros-fairy-watchdog`"}
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(state.get("heartbeat_at", ""))
               ).total_seconds()
    except (TypeError, ValueError):
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


REFRESH_HINT = ("run `ros2 fairy setup --refresh-env` in a shell with the "
                "robot's ROS environment")


def _check_service_env() -> dict:
    """The *service's* ROS environment, compared with this shell's.

    The watchdog starts from a frozen snapshot (``watchdog.env``) captured at
    setup. It goes blind when the file is missing, or when the robot's ROS
    environment changes afterwards (another distro or overlay, ROS_DOMAIN_ID,
    RMW, a DDS profile): the service then harvests the wrong graph, or none
    (issue #29). ``setup --refresh-env`` re-captures it.
    """
    env = ros_env.read_file(paths.watchdog_env_path())
    if "ROS_DISTRO" not in env:
        missing = "missing" if not env else "has no ROS_DISTRO"
        return {"status": FAIL,
                "title": "Background service has no ROS environment",
                "detail": f"{paths.watchdog_env_path()} {missing}",
                "hint": REFRESH_HINT}
    if not os.environ.get("ROS_DISTRO"):
        return {"status": OK, "title": "Background service has a ROS "
                "environment", "detail": f"distro: {env['ROS_DISTRO']} (source "
                "ROS here to compare it with this shell's)", "hint": ""}
    differences = ros_env.service_differences(env, os.environ)
    shell_only = _shell_off_the_robot(env, differences)
    if shell_only:
        return shell_only
    if differences:
        return {"status": WARN,
                "title": "Background service's ROS environment is out of date",
                "detail": "service → this shell: " + "; ".join(differences),
                "hint": REFRESH_HINT}
    return {"status": OK, "title": "Background service ROS environment matches",
            "detail": f"distro: {env['ROS_DISTRO']}", "hint": ""}


def _shell_off_the_robot(service: dict[str, str],
                         differences: list[str]) -> dict | None:
    """With the robot's stack in containers, a difference may be this
    shell's, not the service's: when only discovery settings differ and a
    running ROS container matches the service, refreshing from this shell
    would break it. Say so instead (None otherwise)."""
    if not differences or differences != ros_env.discovery_differences(
            service, os.environ):
        return None
    from ros_fairy.harvest import docker_info
    try:
        containers = docker_info.ros_containers()
    except docker_info.DockerUnavailable:
        return None
    matching = [c["name"] for c in containers
                if not ros_env.discovery_differences(service, c["env"])]
    if not matching:
        return None
    fixes = [f"export {key}={service[key]}" if service.get(key)
             else f"unset {key}"
             for key in ros_env.differing_keys(service, os.environ)]
    return {"status": WARN,
            "title": "This shell is on a different ROS partition than the "
                     "robot",
            "detail": "recording assistant → this shell: "
                      + "; ".join(differences) + "; the recording assistant "
                      f"matches the robot's container(s) {', '.join(matching)}",
            "hint": f"to work with the robot from this shell: "
                    f"`{'; '.join(fixes)}` (nothing to refresh)"}


def _discovery_summary(settings: dict[str, str]) -> str:
    keys = [k for k in ros_env.SESSION_ADOPT_KEYS if k in settings]
    return ", ".join(f"{k}={settings[k]}" for k in keys) or "all defaults"


def _check_container_env() -> dict:
    """The robot's ROS containers, compared with the service.

    A stack that runs in Docker changes its DDS settings inside the
    container, where _check_service_env, which only sees this shell, can't.
    Read with ``docker inspect``, with the settings the container's ROS
    processes really run with when the watchdog has seen them (start-up
    scripts included).
    """
    from ros_fairy.harvest import docker_info
    named = True
    try:
        containers = docker_info.ros_containers()
    except docker_info.DockerUnavailable as exc:
        # The watchdog's findings still say what the containers run with,
        # only not their names (that needs Docker).
        from ros_fairy.watchdog import ros_processes
        containers = [
            {"name": f"the container whose first process is {pid}",
             "env": settings[0], "network": "host", "pid": pid,
             "source": "processes",
             **({"disagreeing": settings} if len(settings) > 1 else {})}
            for pid, settings in sorted(ros_processes.read().items())]
        if not containers:
            return {"status": SKIP, "title": "ROS containers not checked",
                    "detail": str(exc), "hint": ""}
        named = False
    if not containers:
        return {"status": SKIP, "title": "No ROS containers running",
                "detail": "no running container has ROS_DISTRO in its "
                          "configuration, or ROS processes the recording "
                          "assistant has seen", "hint": ""}
    service = ros_env.read_file(paths.watchdog_env_path())
    problems, fix = [], None
    for container in containers:
        name = container["name"]
        if container.get("disagreeing"):
            problems.append(
                f"{name}'s ROS processes don't agree: " + " / ".join(
                    _discovery_summary(s) for s in container["disagreeing"]))
            continue
        differences = ros_env.discovery_differences(service, container["env"])
        if differences:
            seen = " (as its ROS processes run)" \
                if container.get("source") == "processes" else ""
            problems.append(f"{name}{seen}: " + "; ".join(differences))
            fix = fix or name
        if container["network"] != "host":
            problems.append(f"{name} is on its own Docker network "
                            f"({container['network'] or 'default'}): the "
                            "host can't discover its nodes by multicast")
    names = ", ".join(c["name"] for c in containers)
    if not problems:
        return {"status": OK,
                "title": "ROS containers match the background service",
                "detail": names, "hint": ""}
    disagree = any(c.get("disagreeing") for c in containers)
    if fix and not named:
        hint = ("if the container is right, an account that may use Docker "
                "can run `ros2 fairy setup --refresh-env --from-container "
                "<name>` (`docker ps` names it)")
        title = "A ROS container is on a different ROS partition"
    elif fix:
        hint = (f"if the container is right, run `ros2 fairy setup "
                f"--refresh-env --from-container {fix}`")
        title = "A ROS container is on a different ROS partition"
    elif disagree:
        hint = ("give all of the container's nodes the same DDS settings "
                "(an `export` in one start-up script but not another?)")
        title = "A ROS container's nodes are on different ROS partitions"
    else:
        hint = ("run the container with `--network host` (compose: "
                "`network_mode: host`), or configure DDS to reach it")
        title = "A ROS container can't be discovered from the host"
    return {"status": WARN, "title": title,
            "detail": "service → container: " + "; ".join(problems),
            "hint": hint}


def _check_service_python() -> dict:
    """Can the installed unit's interpreter load ros_fairy at all?

    The service runs as root with only the root-owned part of watchdog.env,
    so a copy installed in a home directory (colcon workspace, venv) works in
    the operator's shell but not in the service.
    """
    from ros_fairy.subcommands import setup
    unit = setup.UNIT_DIR / setup.SERVICE_NAME
    try:
        text = unit.read_text()
    except OSError:
        return {"status": SKIP, "title": "Watchdog service not installed",
                "detail": str(unit), "hint": ""}
    python = next((line.split("=", 1)[1].split()[0]
                   for line in text.splitlines()
                   if line.startswith("ExecStart=")), setup.SYSTEM_PYTHON)
    env = ros_env.read_file(paths.watchdog_env_path())
    found = setup.service_import_location(python, env)
    if found is None:
        return {"status": FAIL,
                "title": "Background service can't load ros_fairy",
                "detail": f"{python} can't import it as the service would",
                "hint": "ros_fairy is probably only in a home-directory "
                        "workspace or venv; install it system-wide with "
                        "`./install.sh`, then re-run `ros2 fairy setup`"}
    return {"status": OK, "title": "Background service can load ros_fairy",
            "detail": f"{python}, from {found}", "hint": ""}


def _check_service_harvest() -> dict:
    """What the watchdog's own (service-context) harvest last achieved.

    This is the check that distinguishes "ROS works in my shell" from "the
    background service can actually see ROS" — the exact failure that produced
    empty archives on the real robot.
    """
    from ros_fairy.watchdog import watchdog as wd
    state = wd.read_state() or {}
    status = state.get("harvest_status", {})
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
                    "service's ROS environment may be out of date: "
                    + REFRESH_HINT}


def _when(iso: str | None) -> str:
    """" at 10:22" (today) / " on 01 Oct, 17:20"; "" if unknown."""
    if not isinstance(iso, str):
        return ""
    try:
        t = datetime.fromisoformat(iso).astimezone()
    except ValueError:
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
                "hint": "wait for NTP/chrony to sync before recording; a "
                        "recording already made with a wrong clock can be "
                        "fixed with `ros2 fairy repair`"}
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


def _check_data_dir() -> dict:
    """Where missions are kept, and whether this shell agrees with the
    service (which never sees a shell's environment variables)."""
    try:
        where = paths.var_dir()
        source = paths.var_dir_source()
    except paths.PathsConfigError as exc:
        return {"status": FAIL, "title": "Data folder setting is broken",
                "detail": str(exc), "hint": ""}
    if source == "environment":
        try:
            configured = paths.configured_var_dir() or \
                Path(paths.DEFAULT_VAR_DIR)
        except paths.PathsConfigError as exc:
            return {"status": FAIL, "title": "Data folder setting is broken",
                    "detail": str(exc), "hint": ""}
        if configured != where:
            return {"status": WARN,
                    "title": "This shell uses a different data folder",
                    "detail": f"{paths.VAR_DIR_ENV}={where} here; the "
                              f"recording assistant uses {configured}",
                    "hint": f"unset {paths.VAR_DIR_ENV} (to move the "
                            "missions, use `ros2 fairy move_data`)"}
    default = Path(paths.DEFAULT_VAR_DIR)
    if where != default and (default / "archive").is_dir() and any(
            not p.name.startswith(".")
            for p in (default / "archive").iterdir()):
        return {"status": WARN,
                "title": f"Missions left behind in {default}",
                "detail": f"missions are kept in {where}, but "
                          f"{default / 'archive'} still holds some; they "
                          "aren't listed",
                "hint": f"move them into {where / 'archive'} and run "
                        "`ros2 fairy reindex`, or delete them"}
    why = {"paths.yaml": f"set in {paths.paths_file()}",
           "environment": f"from {paths.VAR_DIR_ENV}",
           "default": "the default"}[source]
    return {"status": OK, "title": f"Missions are kept in {where}",
            "detail": why, "hint": ""}


def _check_harvest_tools() -> dict:
    from ros_fairy.utils import harvest_tools
    tools = harvest_tools.missing_tools()
    modules = harvest_tools.missing_modules()
    if not tools and not modules:
        return {"status": OK, "title": "Harvest tools installed",
                "detail": "", "hint": ""}
    names = [t.command for t in tools] + [m.module for m in modules]
    gives = [t.gives for t in tools] + [m.gives for m in modules]
    return {"status": WARN,
            "title": f"Harvest tools missing: {', '.join(names)}",
            "detail": "left out of every mission: " + "; ".join(gives),
            "hint": "install them: `" + harvest_tools.install_hint(
                tools, modules) + "` (or re-run `./install.sh --with-deps`)"}


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
    holding = []
    for crate in broken:
        bags = crate / "bags"
        count = sum(1 for p in bags.iterdir() if p.is_dir()) \
            if bags.is_dir() else 0
        holding.append(f"{crate.name} ({count} recording"
                       f"{'s' if count != 1 else ''})" if count
                       else crate.name)
    # Never suggest deleting them: recordings made with mission_record were
    # *moved* into the folder, which may hold the only copy.
    return {"status": WARN,
            "title": f"{n} incomplete mission save{'s' if n != 1 else ''}",
            "detail": ", ".join(holding),
            "hint": "a save was cut off (e.g. power loss), so "
                    f"{'these are' if n != 1 else 'this is'} missing from "
                    "`ros2 fairy list`. Recordings inside may be the only "
                    "copy — don't delete them. Run `ros2 fairy mission_close` "
                    "to finish an interrupted save; otherwise ask your robot "
                    "engineer to look at the folder before removing anything."}


def _check_watchdog_code() -> dict:
    """Is the watchdog running the code that is installed now?"""
    from ros_fairy.build_info import build_info, short
    from ros_fairy.watchdog import watchdog as wd
    state = wd.read_state() or {}
    running = state.get("code_id")
    mine = build_info()
    installed = mine["code_id"]
    running_label = state.get("build") or f"code {running}"
    # short() already names the code when there is no commit to name.
    mine_label = short(mine) if installed in short(mine) \
        else f"{short(mine)}, code {installed}"
    if running is None:
        return {"status": SKIP, "title": "Watchdog code version unknown",
                "detail": "the watchdog predates this check", "hint":
                "restart it once so it reports its version: `sudo systemctl "
                "restart ros-fairy-watchdog`"}
    if running == installed:
        return {"status": OK, "title": "Watchdog runs the installed code",
                "detail": mine_label, "hint": ""}
    return {"status": WARN, "title": "Watchdog runs different code than this "
                                     "command",
            "detail": f"watchdog {running_label}; this command {mine_label}",
            "hint": "after installing a new version, restart the watchdog: "
                    "`sudo systemctl restart ros-fairy-watchdog`. If it is "
                    "still different, this command runs from another "
                    "install (a checkout or ~/.local) than the service"}


_CHECKS = (_check_identity, _check_data_dir, _check_watchdog,
           _check_service_python,
           _check_watchdog_code,
           _check_ros_reachable,
           _check_ros_environment, _check_service_env, _check_container_env,
           _check_service_harvest,
           _check_clock, _check_mcap, _check_harvest_tools, _check_disk,
           _check_docker, _check_archive)


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
        print_json({"result": overall, "checks": checks})
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
