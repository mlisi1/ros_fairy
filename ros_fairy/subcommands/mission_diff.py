"""ros2 fairy diff — compare two saved missions."""

from rich.console import Console

from ros_fairy.archive import locate
from ros_fairy.subcommands import (
    VerbExtension,
    _configure_logging,
    guarded_main,
    json_error,
    print_json,
)
from ros_fairy.ui import diff as diff_ui
from ros_fairy.utils import paths


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()

    as_json = getattr(args, "json", False)

    def fail(message: str) -> int:
        if as_json:
            return json_error(message)
        console.print(f"[red]{message}[/red]")
        return 1

    if not paths.index_db_path().is_file():
        return fail("No missions have been saved on this robot yet.")

    a_id: str | None = getattr(args, "mission_a", None)
    b_id: str | None = getattr(args, "mission_b", None)

    if a_id is None and b_id is None:
        a_id, b_id = "2", "1"
    elif b_id is None:
        return fail("Provide either no arguments (compares the two most "
                    "recent missions) or two identifiers.")
    assert a_id is not None and b_id is not None  # both set or returned above

    try:
        crate_a = locate.resolve_archive(a_id)
        crate_b = locate.resolve_archive(b_id)
        record_a = locate.load_record(crate_a)
        record_b = locate.load_record(crate_b)
    except locate.LocateError as exc:
        return fail(str(exc))

    if as_json:
        print_json(diff_ui.diff_as_dict(record_a, record_b, crate_a, crate_b))
        return 0

    diff_ui.show_diff(record_a, record_b, console=console,
                      crate_a=crate_a, crate_b=crate_b)
    return 0


class DiffVerb(VerbExtension):
    """Compare two missions and show what changed between them."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument(
            "mission_a", nargs="?",
            help="older mission: number (2 = second newest), archive path, or "
                 "mission ID. Defaults to the second most recent mission.")
        parser.add_argument(
            "mission_b", nargs="?",
            help="newer mission: number (1 = newest), archive path, or mission "
                 "ID. Defaults to the most recent mission.")
        parser.add_argument(
            "--json", action="store_true",
            help="machine-readable output for scripts")
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        return guarded_main(run, args)
