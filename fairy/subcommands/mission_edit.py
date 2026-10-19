"""ros2 fairy mission_edit — change a saved mission's briefing.

For a typo in the place name, a goal worded better, notes added after the
fact. The record, README, RO-Crate and checksums are rewritten together and
the edit is recorded in the mission's provenance (see archive/edit.py). The
recordings, and the archive folder's name, are not touched.

--redact also removes the old values (for text that shouldn't be there). It
needs the person's name (--by) and runs the write as root through sudo, so
the system's auth log has the command next to the account, and the history
both.
"""

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from fairy.archive import edit, index, locate
from fairy.subcommands import (
    VerbExtension,
    _configure_logging,
    confirm,
    guarded_main,
    refuse_without_access,
)
from fairy.ui import briefing as questions

# Briefing field -> option.
OPTIONS = {"operator_name": "operator", "goal": "goal",
           "location_name": "location", "environment": "environment",
           "notes": "notes"}
LABELS = {"operator_name": "Operator", "goal": "Goal",
          "location_name": "Location", "environment": "Environment",
          "notes": "Notes"}
CLEAR = "-"


def _ask(console: Console, current: dict[str, str | None]
         ) -> dict[str, str | None]:
    """Every briefing question, the saved answer as the default: Enter keeps
    it, "-" clears an optional one."""
    console.print("Press Enter to keep an answer; type - to clear an "
                  "optional one.")
    answers: dict[str, str | None] = {}
    for field, prompt, required in questions.QUESTIONS:
        while True:
            value = questions.ask(prompt.split(" (")[0], console,
                                  default=current[field] or "",
                                  show_default=bool(current[field])).strip()
            if value == CLEAR and not required:
                answers[field] = None
                break
            if value or not required:
                answers[field] = value or None
                break
            console.print("[yellow]This one can't be empty.[/yellow]")
    return answers


def _show_changes(console: Console, current: dict[str, str | None],
                  wanted: dict[str, str | None]) -> bool:
    """Show what would change; False when nothing would."""
    table = Table(border_style="dim")
    table.add_column("")
    table.add_column("Now")
    table.add_column("Will be")
    changed = False
    for field, value in wanted.items():
        value = (value or "").strip() or None
        if value != current[field]:
            changed = True
            table.add_row(LABELS[field], current[field] or "[dim](empty)[/dim]",
                          value or "[dim](empty)[/dim]")
    if changed:
        console.print(table)
    return changed


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()
    refused = refuse_without_access(console)
    if refused is not None:
        return refused
    try:
        crate = locate.resolve_archive(getattr(args, "mission", None) or "1")
    except locate.LocateError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    done = edit.finish_interrupted(crate)
    if done == "finished":
        console.print("[yellow]An earlier edit of this mission was "
                      "interrupted; it has been completed.[/yellow]")
    try:
        record = edit.load(crate)
    except edit.EditError as exc:
        console.print(f"[red]Can't edit {crate.name}: {exc}[/red]")
        return 1
    differ = edit.changed_by_hand(crate)
    if differ:
        console.print(f"[red]Can't edit {crate.name}: the archive was changed "
                      f"after it was saved ({', '.join(differ)}). Run `ros2 "
                      "fairy verify` to see what; nothing was edited.[/red]")
        return 1

    current = edit.briefing(record)
    wanted = {field: getattr(args, option)
              for field, option in OPTIONS.items()
              if getattr(args, option, None) is not None}
    console.print(f"Mission [bold]{record.identity.mission_id}[/bold] "
                  f"({crate.name})")
    if not wanted:
        if not sys.stdin.isatty():
            console.print("[red]Say what to change (--operator, --goal, "
                          "--location, --environment, --notes): there's no "
                          "terminal to ask on.[/red]")
            return 2
        wanted = _ask(console, current)
    if not _show_changes(console, current, wanted):
        console.print("Nothing to change.")
        return 0
    redact = getattr(args, "redact", False)
    by = (getattr(args, "by", None) or "").strip() or None
    confirmed = getattr(args, "confirmed", False)
    if redact and not confirmed:
        if by is None:
            if not sys.stdin.isatty():
                console.print("[red]--redact needs your name: --by "
                              "\"First Last\".[/red]")
                return 2
            while not by:
                by = Prompt.ask("Your full name (recorded with the "
                                "redaction)", console=console).strip()
        console.print(
            "[yellow]Redacting removes the old values of these fields from "
            "the mission: its record, README, RO-Crate, the mission list, "
            "and earlier entries of its edit history. The redaction itself, "
            "your name and your account stay recorded. Copies made before "
            "(exported bundles, backups) still contain the old values: "
            "delete or replace them yourself.[/yellow]")
        if not confirm("Redact and save?", default=False, console=console,
                       assume_yes=getattr(args, "yes", False)):
            console.print("Nothing was changed.")
            return 0
        if os.geteuid() != 0:
            return _redact_via_sudo(console, crate, wanted, by or "")
    elif not confirmed and not confirm(
            "Save these changes to the mission?", default=True,
            console=console, assume_yes=getattr(args, "yes", False)):
        console.print("Nothing was changed.")
        return 0
    if redact and not by:
        console.print("[red]--redact needs your name: --by \"First Last\"."
                      "[/red]")
        return 2

    try:
        changes, final = edit.apply(crate, wanted, redact=redact, by=by)
    except edit.EditError as exc:
        console.print(f"[red]Can't edit {crate.name}: {exc}[/red]")
        return 1
    done = "Redacted" if redact else "Updated"
    console.print(f"[green]{done} {', '.join(LABELS[f].lower() for f in changes)}"
                  f" of {record.identity.mission_id}.[/green] The record, "
                  "README, RO-Crate and checksums were rewritten together; "
                  "the change is noted in the mission's provenance.")
    if final != crate:
        console.print(f"The folder was renamed to {final.name}, as its name "
                      "came from a redacted value.")
    elif {"operator_name", "location_name"} & set(changes):
        console.print(f"[dim]The folder keeps its name ({crate.name}), so "
                      "paths to it still work.[/dim]")
    exported = index.forget_export(record.identity.mission_id)
    if exported:
        when = exported["exported_at"]
        try:
            when = datetime.fromisoformat(when).astimezone().strftime(
                "%d %B %Y")
        except ValueError:
            pass
        bundle = Path(exported["bundle_path"])
        still = (" and still holds the redacted values" if redact else "")
        console.print(f"[yellow]The bundle exported on {when} ({bundle}) no "
                      f"longer matches{still}. Export it again, replacing it: "
                      f"ros2 fairy export {record.identity.mission_id} -o "
                      f"{bundle.parent}/ --force[/yellow]")
    return 0


def _redact_via_sudo(console: Console, crate, wanted: dict[str, str | None],
                     by: str) -> int:
    """Run the redacting write as root: sudo asks for the password, and its
    log keeps the command with the account behind it."""
    sudo = shutil.which("sudo")
    if sudo is None:
        console.print("[red]--redact runs through sudo, which isn't "
                      "available here. Run this command as root.[/red]")
        return 1
    console.print("[dim]A redaction needs your sudo password.[/dim]")
    command = [sudo, "--preserve-env=ROS_FAIRY_CONFIG_DIR,ROS_FAIRY_VAR_DIR,"
               "PYTHONPATH", sys.executable, "-m",
               "fairy.subcommands.mission_edit", str(crate), "--redact",
               f"--by={by}", "--confirmed"]
    # --opt=value, so a value starting with "-" isn't taken for an option.
    for field, value in wanted.items():
        command.append(f"--{OPTIONS[field]}={value or ''}")
    pkg_root = str(Path(__file__).resolve().parents[2])
    return subprocess.run(command,
                          env={**os.environ, "PYTHONPATH": pkg_root}).returncode


def _add_arguments(parser) -> None:
    parser.add_argument(
        "mission", nargs="?",
        help="mission to edit: number (1 = newest), archive path, or "
             "mission ID. Defaults to the most recent mission.")
    parser.add_argument("--operator", metavar="NAME",
                        help="the operator's name")
    parser.add_argument("--goal", metavar="TEXT",
                        help="what the mission was trying to do")
    parser.add_argument("--location", metavar="PLACE",
                        help="where it took place")
    parser.add_argument("--environment", metavar="TEXT",
                        help='the environment ("" clears it)')
    parser.add_argument("--notes", metavar="TEXT",
                        help='notes ("" clears them)')
    parser.add_argument("--yes", "-y", action="store_true",
                        help="don't ask before saving the changes")
    parser.add_argument(
        "--redact", action="store_true",
        help="also remove the old values from the mission (record, README, "
             "RO-Crate, list, edit history); needs --by and your sudo "
             "password")
    parser.add_argument("--by", metavar="NAME",
                        help="with --redact: your full name, recorded with it")
    parser.add_argument(
        "--debug", action="store_true",
        help="verbose logging to stderr (for engineers)")
    # internal: the root re-exec of a redaction, after the questions
    parser.add_argument("--confirmed", action="store_true",
                        help=argparse.SUPPRESS)


class MissionEditVerb(VerbExtension):
    """Change a saved mission's briefing (operator, goal, place, notes)."""

    def add_arguments(self, parser, cli_name):
        _add_arguments(parser)

    def main(self, *, args):
        return guarded_main(run, args)


def main(argv: list[str] | None = None) -> None:
    """The sudo re-exec target of a redaction."""
    parser = argparse.ArgumentParser(prog="ros2 fairy mission_edit",
                                     description=MissionEditVerb.__doc__)
    _add_arguments(parser)
    sys.exit(guarded_main(run, parser.parse_args(argv)))


if __name__ == "__main__":
    main()
