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
    from ros_fairy.ui.status import watchdog_alive
    return watchdog_alive(wd.read_state())


def _last_operator() -> str | None:
    try:
        from ros_fairy.archive import index
        rows, _ = index.query(limit=1)
        return rows[0]["operator"] if rows else None
    except Exception:
        return None


def _blocks_replacing(console: Console, has_mission: bool = True) -> bool:
    """Whether what the spool holds can't be put aside without losing data.

    A live recording belongs to whichever mission is open, and recordings
    ros-fairy saved into the spool exist nowhere else; both must be resolved
    with mission_close first rather than silently mixed into the new mission.
    """
    state = wd.read_state()
    if state and state.get("state") in ("RECORDING", "FINALISING"):
        from ros_fairy.ui.status import watchdog_alive
        if watchdog_alive(state):
            console.print("[yellow]A recording is in progress. Stop it first, "
                          "then run this again.[/yellow]")
            return True
    bags = paths.bags_dir()
    spool_bags = [p for p in bags.iterdir() if p.is_dir()] \
        if bags.is_dir() else []
    if spool_bags:
        n = len(spool_bags)
        whose = "The unfinished mission still has" if has_mission \
            else "There are"
        extra = "" if has_mission else " made while no mission was open"
        console.print(f"[yellow]{whose} {n} recording{'s' if n != 1 else ''}"
                      f" saved by ros-fairy{extra}. Save or discard "
                      f"{'it' if has_mission else 'them'} first with "
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
    harvest, existing = builder.load_spool()
    # Whatever the spool holds is only put aside once the new briefing has
    # been answered: a Ctrl-C during the questions must change nothing.
    drop_previous = False
    if context_path.is_file() and existing:
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
        drop_previous = True
    elif harvest is not None:
        # No mission open, but the watchdog captured something: recordings
        # made with no mission (an engineer's `record_all test`). They must
        # not slip silently into this mission.
        if _blocks_replacing(console, has_mission=False):
            return 1
        earlier = [b["path"] for b in harvest.get("bags", [])]
        if earlier:
            n = len(earlier)
            console.print(f"{n} recording{'s were' if n != 1 else ' was'} "
                          "made while no mission was open:")
            for path in earlier:
                console.print(f"  {path}")
            drop_previous = not Confirm.ask(
                f"Include {'them' if n != 1 else 'it'} in this new mission?",
                default=False, console=console)
        else:
            drop_previous = True  # context of no recording: stale

    answers = briefing.ask_briefing(console=console,
                                    default_operator=_last_operator())
    if drop_previous:
        _drop_previous_harvest(console)
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
    ros_env.write_file(paths.session_env_path(), ros_env.capture(),
                       mode=0o664)
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
