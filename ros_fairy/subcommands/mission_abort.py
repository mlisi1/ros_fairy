"""ros2 fairy mission_abort — throw away the open mission without saving it.

For a mission that could not be carried out, or a recording made by mistake.
Recordings ros-fairy saved into the spool are deleted; recordings made outside
it (referenced where they were recorded) are left on disk untouched, exactly as
mission_close's discard does.
"""

from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm

from ros_fairy.manifest import builder
from ros_fairy.subcommands import VerbExtension, _configure_logging, guarded_main
from ros_fairy.subcommands import mission_close
from ros_fairy.ui.review import human_size
from ros_fairy.utils import fsio, paths


def _recordings(harvest: dict | None) -> tuple[list[Path], list[Path]]:
    """(spool recordings, recordings made outside ros-fairy)."""
    bags = paths.bags_dir()
    spool = sorted(p for p in bags.iterdir() if p.is_dir()) \
        if bags.is_dir() else []
    foreign = [Path(b["path"]) for b in (harvest or {}).get("bags", [])
               if Path(b["path"]).parent != bags]
    return spool, foreign


def _describe(context: dict | None) -> str:
    identity = (context or {}).get("identity", {})
    goal = (context or {}).get("intent", {}).get("goal") or "untitled mission"
    when = identity.get("created_at", "")
    try:
        when = datetime.fromisoformat(when).astimezone().strftime(
            "%d %B, %H:%M")
    except ValueError:
        pass
    who = identity.get("operator_name")
    return f"'{goal}'" + (f" started {when}" if when else "") + \
        (f" by {who}" if who else "")


def _size(path: Path) -> str:
    try:
        return human_size(fsio.dir_size_bytes(path))
    except OSError:
        return "size unknown"


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()

    if mission_close._recording_in_progress():
        console.print("[yellow]A recording is still in progress. Stop it "
                      "first (Ctrl-C in the recording window), then run this "
                      "again.[/yellow]")
        return 1
    mission_close._wait_for_finalising(console)

    harvest, context = builder.load_spool()
    spool, foreign = _recordings(harvest)
    if context is None and not spool and not foreign:
        console.print("There's no mission in progress.")
        return 0

    if not Confirm.ask(f"Abort the mission {_describe(context)}? It will not "
                       "be saved.", default=False, console=console):
        console.print("Nothing was changed.")
        return 0

    if spool or foreign:
        n = len(spool) + len(foreign)
        console.print(f"This mission has {n} recording{'s' if n != 1 else ''}:")
        for bag in spool:
            console.print(f"  [red]{bag.name}[/red] ({_size(bag)}) — "
                          "will be deleted permanently")
        for bag in foreign:
            console.print(f"  {bag} ({_size(bag)}) — made outside ros-fairy, "
                          "stays on disk")
        question = (f"Delete {len(spool)} recording"
                    f"{'s' if len(spool) != 1 else ''} and abort?"
                    if spool else "Abort anyway?")
        if not Confirm.ask(question, default=False, console=console):
            console.print("Nothing was changed.")
            return 0

    mission_close._discard_spool()
    try:  # make the discard durable: a surviving spool leaks into the next
        fsio.fsync_dir(paths.spool_dir())  # mission (see assembler step 7)
    except OSError:
        pass
    console.print("Mission aborted.")
    return 0


class MissionAbortVerb(VerbExtension):
    """Abandon the open mission (and its recordings) without saving it."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        return guarded_main(run, args)
