"""ros2 fairy reindex — rebuild the mission index from the archives on disk.

The SQLite index is a cache over the archive directory; if it is lost, stale,
or was written before a permissions fix, this verb regenerates it by scanning
every saved crate's ``mission_record.json``. Read-only with respect to the
archives themselves.
"""

import json

from rich.console import Console

from ros_fairy.archive import index
from ros_fairy.subcommands import VerbExtension, _configure_logging, guarded_main
from ros_fairy.utils import paths


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()
    report: dict = {}
    try:
        count = index.reindex(report=report)
    except index.IndexUnavailableError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            console.print(f"[red]{exc}[/red]")
        return 1
    skipped = report.get("skipped", [])
    duplicates = report.get("duplicates", {})
    if getattr(args, "json", False):
        print(json.dumps({"ok": True, "missions": count,
                          "archive_dir": str(paths.archive_dir()),
                          "skipped": [{"folder": f, "reason": r}
                                      for f, r in skipped],
                          "duplicates": duplicates}, indent=2))
        return 0
    if count == 0:
        console.print("No saved missions found in "
                      f"{paths.archive_dir()} — the index is now empty.")
    else:
        plural = "mission" if count == 1 else "missions"
        console.print(f"Rebuilt the mission list: {count} saved {plural} "
                      "found. `ros2 fairy list` is up to date.")
    for folder, reason in skipped:
        console.print(f"[yellow]Couldn't read {folder}: {reason}[/yellow]")
    for mission_id, folders in duplicates.items():
        console.print(f"[yellow]Mission {mission_id} was saved more than "
                      f"once ({', '.join(folders)}); the most recent save is "
                      "listed. Check which one to keep with "
                      "[bold]ros2 fairy verify[/bold] before deleting the "
                      "other.[/yellow]")
    return 0


class ReindexVerb(VerbExtension):
    """Rebuild the mission list from the saved archives on disk."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument(
            "--json", action="store_true",
            help="machine-readable output for scripts")
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        return guarded_main(run, args)
