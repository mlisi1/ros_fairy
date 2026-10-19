"""ros2 fairy reindex — rebuild the mission index from the archives on disk.

The SQLite index is a cache over the archive directory; if it is lost, stale,
or was written before a permissions fix, this verb regenerates it by scanning
every saved crate's ``mission_record.json``. Read-only with respect to the
archives themselves.
"""

from rich.console import Console

from fairy.archive import index
from fairy.subcommands import (
    VerbExtension,
    _configure_logging,
    guarded_main,
    json_error,
    print_json,
)
from fairy.utils import paths


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()
    report: dict = {}
    try:
        count = index.reindex(report=report)
    except index.IndexUnavailableError as exc:
        if getattr(args, "json", False):
            return json_error(str(exc))
        console.print(f"[red]{exc}[/red]")
        return 1
    skipped = report.get("skipped", [])
    duplicates = report.get("duplicates", {})
    if getattr(args, "json", False):
        print_json({"missions": count,
                    "archive_dir": str(paths.archive_dir()),
                    "skipped": [{"folder": f, "reason": r}
                                for f, r in skipped],
                    "duplicates": duplicates})
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
