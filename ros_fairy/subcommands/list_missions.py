"""ros2 fairy list — table of saved missions from the SQLite index."""

import json
from datetime import datetime

from rich.console import Console
from rich.table import Table

from ros_fairy.archive import assembler, index
from ros_fairy.subcommands import VerbExtension, _configure_logging, guarded_main
from ros_fairy.ui.review import human_size
from ros_fairy.utils import paths
from ros_fairy.utils.topic_health import humanize_duration


def _fmt_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime(
            "%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def _local_date(iso: str):
    """The calendar date (in local time) a mission's created_at falls on, or
    None if it can't be parsed — used only to group same-day rows."""
    try:
        return datetime.fromisoformat(iso).astimezone().date()
    except ValueError:
        return None


_QUALITY_CELL = {
    "degraded": "[yellow]partial[/yellow]",
    "poor": "[red]poor[/red]",
}


def _quality_cell(value: str | None) -> str:
    return _QUALITY_CELL.get(value or "", "")


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()
    as_json = getattr(args, "json", False)

    if not paths.index_db_path().is_file():
        if as_json:
            print(json.dumps({"missions": [], "total": 0, "shown": 0},
                             indent=2))
        else:
            console.print("No missions have been saved on this robot yet.")
        return 0

    try:
        rows, total = index.query(
            operator=getattr(args, "operator", None),
            location=getattr(args, "location", None),
            since=getattr(args, "since", None),
            until=getattr(args, "until", None),
            quality=getattr(args, "quality", None),
            limit=getattr(args, "limit", 20) or 20)
    except index.IndexUnavailableError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    if as_json:
        # Rows are already plain dicts of index columns (all JSON-native).
        print(json.dumps(
            {"missions": rows, "total": total, "shown": len(rows)}, indent=2))
        return 0

    if not rows:
        console.print("No missions found.")
        return 0

    show_path = getattr(args, "path", False)
    table = Table(border_style="dim")
    table.add_column("Date")
    # Fixed-width ("m-YYYYMMDD-HHMMSS-xxxx"), no_wrap so it's always copyable
    # as one contiguous string — this is the identifier `ros2 fairy export`,
    # `verify` and `diff` accept, so an operator can paste it straight from
    # here instead of hunting through archive paths.
    table.add_column("Mission ID", no_wrap=True, style="dim")
    table.add_column("Mission")
    table.add_column("Location")
    table.add_column("Operator")
    table.add_column("Duration", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("⚠", justify="right")
    table.add_column("Data")
    if show_path:
        table.add_column("Path")
    for i, row in enumerate(rows):
        goal = row["goal"]
        if len(goal) > 40:
            goal = goal[:39] + "…"
        cells = [
            _fmt_date(row["created_at"]),
            row["mission_id"],
            goal,
            row["location"],
            row["operator"],
            humanize_duration(row["duration_s"]) if row["duration_s"] else "",
            human_size(row["size_bytes"]) if row["size_bytes"] else "",
            str(row["warning_count"]) if row["warning_count"] else "",
            _quality_cell(row.get("data_quality")),
        ]
        if show_path:
            cells.append(row["archive_path"])
        # A rule between two rows landing on different calendar days makes a
        # busy multi-day list scannable at a glance.
        next_day = _local_date(rows[i + 1]["created_at"]) if i + 1 < len(rows) \
            else None
        end_section = next_day is not None and next_day != _local_date(
            row["created_at"])
        table.add_row(*cells, end_section=end_section)
    console.print(table)
    if total > len(rows):
        console.print(f"Showing {len(rows)} of {total} missions")
    broken = assembler.find_incomplete_crates()
    if broken:
        n = len(broken)
        console.print(f"[yellow]{n} incomplete mission save"
                      f"{'s' if n != 1 else ''} not shown — run "
                      "[bold]ros2 fairy doctor[/bold] for details.[/yellow]")
    return 0


class ListVerb(VerbExtension):
    """List the missions saved on this robot."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument("--debug", action="store_true",
                            help="verbose logging to stderr (for engineers)")
        parser.add_argument("--operator", help="filter by operator name")
        parser.add_argument("--location", help="filter by location")
        parser.add_argument("--since", metavar="YYYY-MM-DD",
                            help="only missions on or after this date")
        parser.add_argument("--until", metavar="YYYY-MM-DD",
                            help="only missions up to this date")
        parser.add_argument("--quality", choices=["ok", "degraded", "poor"],
                            help="only missions with this data-quality verdict")
        parser.add_argument("--limit", type=int, default=20,
                            help="maximum rows to show (default 20)")
        parser.add_argument("--path", action="store_true",
                            help="also show archive directory paths")
        parser.add_argument("--json", action="store_true",
                            help="machine-readable output for scripts")

    def main(self, *, args):
        return guarded_main(run, args)
