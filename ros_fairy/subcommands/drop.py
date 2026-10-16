"""ros2 fairy drop — take one unwanted recording out of the open mission.

The opposite of ``adopt``. Recordings are named by the number
``mission_status`` shows (#1, #2, ...), which the watchdog gives each one as
it joins the mission and never reuses, so it still names the same recording
after another one is dropped. A recording ros-fairy saved into the spool is
deleted (after confirmation); one made elsewhere and only referenced
(``detected`` or ``adopted``) is detached and stays on disk, where ``adopt``
can attach it again.
"""

import shutil
import sys
from pathlib import Path

from rich.console import Console
from rich.prompt import Prompt

from ros_fairy.archive import assembler
from ros_fairy.subcommands import (
    VerbExtension,
    _configure_logging,
    confirm,
    guarded_main,
    mission_close,
    refuse_without_access,
)
from ros_fairy.ui.status import (
    recording_line,
    recording_list,
    watchdog_alive,
)
from ros_fairy.watchdog import watchdog as wd


def _show(console: Console, recs: list[dict]) -> None:
    console.print("Recordings in the open mission:")
    for rec in recs:
        console.print(recording_line(rec))


def _find(recs: list[dict], which: str) -> dict | None:
    """A recording by its number (``3`` or ``#3``) or its path."""
    number = which.lstrip("#")
    if number.isdigit():
        return next((r for r in recs if r["id"] == int(number)), None)
    wanted = Path(which).expanduser().resolve()
    return next((r for r in recs if r["path"].resolve() == wanted), None)


def _busy() -> str | None:
    state = wd.read_state() or {}
    if mission_close._recording_in_progress():
        return "A recording is in progress. Stop it first, then run this " \
            "again."
    # A dead watchdog's state file says FINALISING forever.
    if state.get("state") == "FINALISING" and watchdog_alive(state):
        return "The recording assistant is still processing the last " \
            "recording. Wait a moment, then run this again."
    return None


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()
    refused = refuse_without_access(console)
    if refused is not None:
        return refused
    busy = _busy()
    if busy:
        console.print(f"[yellow]{busy}[/yellow]")
        return 1
    try:
        with assembler.save_lock():  # no mission_close meanwhile
            return _run_locked(args, console)
    except assembler.SaveInProgressError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        return 1


def _run_locked(args, console: Console) -> int:
    unfinished = mission_close._unfinished_save()
    if unfinished:
        console.print(f"[yellow]{unfinished}[/yellow]")
        return 1
    recs = recording_list()
    if not recs:
        console.print("The open mission has no recordings.")
        return 0
    which = getattr(args, "recording", None)
    if which is None:
        _show(console, recs)
        if not sys.stdin.isatty():
            console.print("Say which one: ros2 fairy drop <#>.")
            return 2
        which = Prompt.ask("Which recording should be taken out? (#, Enter "
                           "for none)", default="", console=console).strip()
        if not which:
            console.print("Nothing was changed.")
            return 0
    rec = _find(recs, which)
    if rec is None:
        _show(console, recs)
        console.print(f"[red]There's no recording {which} in the open "
                      "mission.[/red]")
        return 1

    path = rec["path"]
    yes = getattr(args, "yes", False)
    if rec["in_spool"]:
        question = (f"Delete recording #{rec['id']} ({path.name}) "
                    "permanently? The rest of the mission is kept."
                    if rec["id"] else f"Delete the recording {path.name} "
                    "permanently? The rest of the mission is kept.")
    else:
        question = (f"Take {path} out of this mission? It stays on disk "
                    "where it is.")
    if not confirm(question, default=False, console=console, assume_yes=yes):
        console.print("Nothing was changed.")
        return 0

    wd.drop_bag_record(path)
    if rec["in_spool"]:
        try:
            shutil.rmtree(path)
        except OSError as exc:
            console.print(f"[red]The recording was taken out of the mission, "
                          f"but deleting {path} failed ({exc}). Delete it by "
                          "hand before closing the mission, or it is "
                          "attached again.[/red]")
            return 1
        console.print(f"Deleted {path.name}.")
    else:
        console.print(f"Took {path.name} out of the mission. It's still at "
                      f"{path}; attach it again with [bold]ros2 fairy adopt "
                      f"{path}[/bold].")
    left = len(recs) - 1
    console.print(f"The mission now has {left} recording"
                  f"{'s' if left != 1 else ''}.")
    return 0


class DropVerb(VerbExtension):
    """Take one unwanted recording out of the open mission (undoes adopt)."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument(
            "recording", nargs="?",
            help="the recording's number as mission_status shows it (3 or "
                 "#3), or its path. Without it, the recordings are listed to "
                 "choose from")
        parser.add_argument(
            "--yes", "-y", action="store_true",
            help="don't ask before deleting or detaching it")
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        return guarded_main(run, args)
