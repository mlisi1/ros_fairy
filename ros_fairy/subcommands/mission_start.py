"""ros2 fairy mission_start — the briefing wizard."""

from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm

from ros_fairy.manifest import builder
from ros_fairy.subcommands import VerbExtension, _configure_logging, guarded_main
from ros_fairy.ui import briefing
from ros_fairy.utils import clock, fsio, paths, ros_env
from ros_fairy.watchdog import watchdog as wd


def _watchdog_alive() -> bool:
    state = wd.read_state()
    if state is None:
        return False
    from ros_fairy.ui.status import _pid_alive
    return _pid_alive(state.get("pid"))


def _last_operator() -> str | None:
    try:
        from ros_fairy.archive import index
        rows, _ = index.query(limit=1)
        return rows[0]["operator"] if rows else None
    except Exception:
        return None


def _blocks_replacing(console: Console) -> bool:
    """Whether the unfinished mission can't be replaced without losing data.

    A live recording belongs to whichever mission is open, and recordings
    ros-fairy saved into the spool exist nowhere else; both must be resolved
    with mission_close first rather than silently mixed into the new mission.
    """
    state = wd.read_state()
    if state and state.get("state") in ("RECORDING", "FINALISING"):
        from ros_fairy.ui.status import _pid_alive
        if _pid_alive(state.get("pid")):
            console.print("[yellow]A recording is in progress. Stop it first, "
                          "then run this again.[/yellow]")
            return True
    bags = paths.bags_dir()
    spool_bags = [p for p in bags.iterdir() if p.is_dir()] \
        if bags.is_dir() else []
    if spool_bags:
        n = len(spool_bags)
        console.print(f"[yellow]The unfinished mission still has {n} "
                      f"recording{'s' if n != 1 else ''} saved by ros-fairy. "
                      "Save or discard it first with "
                      "[bold]ros2 fairy mission_close[/bold].[/yellow]")
        return True
    return False


def _drop_previous_harvest(console: Console) -> None:
    """Forget the replaced mission's harvest (graph snapshot + bag list).

    Without this the new mission inherits recordings and a ROS-graph capture
    from before it started. Recordings referenced in place (made outside
    ros-fairy) stay on disk and can be attached again with ``adopt``.
    """
    harvest = builder.load_spool()[0]
    foreign = [b["path"] for b in (harvest or {}).get("bags", [])]
    if foreign:
        console.print("These recordings are no longer attached to a mission "
                      "(they are still on disk; attach one with "
                      "[bold]ros2 fairy adopt <folder>[/bold]):")
        for path in foreign:
            console.print(f"  {path}")
    paths.harvest_json_path().unlink(missing_ok=True)
    paths.watchdog_log_path().unlink(missing_ok=True)


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()

    if not _watchdog_alive():
        console.print("[yellow]Background recording assistant isn't running "
                      "— your answers will still be saved.[/yellow]")

    if clock.is_synchronized() is False:
        console.print(f"[yellow]{clock.WARNING}[/yellow]")

    context_path = paths.mission_context_path()
    if context_path.is_file():
        existing = builder.load_spool()[1]
        if existing:
            if _blocks_replacing(console):
                return 1
            identity = existing.get("identity", {})
            when = identity.get("created_at", "")
            try:
                when = datetime.fromisoformat(when).astimezone().strftime(
                    "%d %B, %H:%M")
            except ValueError:
                pass
            replace = Confirm.ask(
                f"There's already an unfinished mission from {when} by "
                f"{identity.get('operator_name', 'someone')}. Start a new "
                f"one and replace it?", default=False, console=console)
            if not replace:
                return 0
            _drop_previous_harvest(console)

    answers = briefing.ask_briefing(console=console,
                                    default_operator=_last_operator())
    context = builder.new_mission_context(
        operator_name=answers["operator_name"],
        goal=answers["goal"],
        location_name=answers["location_name"],
        environment=answers["environment"],
        notes=answers["notes"])
    paths.spool_dir().mkdir(parents=True, exist_ok=True)
    # Hand the live recording shell's ROS environment to the watchdog so its
    # harvest sees the same DDS partition / overlay as this session, even if the
    # frozen watchdog.env snapshot has drifted (issue #29).
    ros_env.write_file(paths.session_env_path(), ros_env.capture())
    fsio.atomic_write_json(context_path, context)
    console.print(Panel("Mission briefing saved. Start recording with: "
                        "[bold]ros2 fairy mission_record[/bold]",
                        border_style="green"))
    return 0


class MissionStartVerb(VerbExtension):
    """Answer five quick questions to describe the mission you're starting."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        return guarded_main(run, args)
