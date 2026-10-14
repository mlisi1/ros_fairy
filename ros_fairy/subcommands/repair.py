"""ros2 fairy repair — make a saved mission's unplayable bags playable again.

A bag recorded with an unsynchronised clock won't play (``ros2 bag play``
honours the broken 1970↔now timeline and stalls). This writes a re-stamped,
immediately-playable copy of each affected recording — leaving the original
archive untouched, so its checksums and `verify` result still hold. The repaired
timing is synthetic (see ``utils/bag_repair``): good for inspection/playback,
not for time-critical processing.

Accepts a saved mission (number / archive path / ID, like ``verify``) or a path
to a single bag directory.
"""

from pathlib import Path

from rich.console import Console
from rich.table import Table

from ros_fairy.archive import locate
from ros_fairy.subcommands import (
    VerbExtension,
    _configure_logging,
    completion,
    guarded_main,
    json_error,
    print_json,
)
from ros_fairy.utils import bag_repair


def _bag_dirs(target: str) -> tuple[str, list[Path]]:
    """Resolve the target to (label, [bag directories]).

    A path to a single bag dir (``metadata.yaml`` but no ``mission_record.json``)
    -> just that one; otherwise a mission identifier (number / path / ID) -> all
    its bag dirs. The bag-dir case is checked first so it never needs the index.
    """
    p = Path(target)
    if p.is_dir() and (p / "metadata.yaml").is_file() \
            and not (p / "mission_record.json").is_file():
        return p.name, [p]
    crate = locate.resolve_archive(target)
    record = locate.load_record(crate)
    return crate.name, [crate / b.path for b in record.bags]


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()

    as_json = getattr(args, "json", False)
    target = getattr(args, "mission", None) or "1"
    try:
        label, bag_dirs = _bag_dirs(target)
    except locate.LocateError as exc:
        if as_json:
            return json_error(str(exc))
        console.print(f"[red]{exc}[/red]")
        return 1

    out_root = Path(getattr(args, "output", None) or f"{label}_repaired")
    if out_root.exists() and any(out_root.iterdir()) \
            and not getattr(args, "force", False):
        message = (f"{out_root} already exists and isn't empty. Choose "
                   "another --output, or pass --force.")
        if as_json:
            return json_error(message)
        console.print(f"[red]{message}[/red]")
        return 1

    do_all = getattr(args, "all", False)
    duration = getattr(args, "duration", None)
    results: list[dict] = []
    repaired = 0
    for bag_dir in bag_dirs:
        if not bag_dir.is_dir():
            results.append({"bag": bag_dir.name, "status": "missing"})
            continue
        if not (do_all or bag_repair.needs_repair(bag_dir)):
            results.append({"bag": bag_dir.name, "status": "ok"})
            continue
        dest = out_root / bag_dir.name
        try:
            summary = bag_repair.restamp_bag(bag_dir, dest, duration_s=duration)
            results.append({"bag": bag_dir.name, "status": "repaired",
                            "dest": summary["dest"],
                            "messages": summary["messages"],
                            "new_duration_s": summary["new_duration_s"]})
            repaired += 1
        except bag_repair.BagRepairError as exc:
            results.append({"bag": bag_dir.name, "status": "unsupported",
                            "detail": str(exc)})

    if as_json:
        print_json({"target": label, "output": str(out_root),
                    "repaired": repaired, "bags": results})
        return 0

    _render(console, label, out_root, results, repaired)
    return 0


_GLYPH = {"repaired": "[green]✓ repaired[/green]",
          "ok": "[dim]– already playable[/dim]",
          "unsupported": "[yellow]! skipped[/yellow]",
          "missing": "[red]✗ missing[/red]"}


def _render(console: Console, label: str, out_root: Path,
            results: list[dict], repaired: int) -> None:
    if repaired == 0 and all(r["status"] == "ok" for r in results):
        console.print("All recordings already have a usable clock — nothing to "
                      "repair. [dim](Use --all to re-stamp anyway.)[/dim]")
        return
    table = Table(border_style="dim")
    table.add_column("Recording")
    table.add_column("Result")
    table.add_column("Detail")
    for r in results:
        if r["status"] == "repaired":
            detail = (f"{r['messages']} msgs over {r['new_duration_s']:.0f}s "
                      f"→ {Path(r['dest']).name}")
        else:
            detail = r.get("detail", "")
        table.add_row(r["bag"], _GLYPH[r["status"]], detail)
    console.print(table)
    if repaired:
        console.print(
            f"\n[green]Repaired {repaired} recording(s)[/green] into {out_root}/ "
            "— play with [bold]ros2 bag play[/bold]. "
            "[dim]Timing is approximate; the originals are untouched.[/dim]")


class RepairVerb(VerbExtension):
    """Make a saved mission's unplayable (bad-clock) recordings playable."""

    def add_arguments(self, parser, cli_name):
        arg = parser.add_argument(
            "mission", nargs="?",
            help="mission to repair: number (1 = newest), archive path, mission "
                 "ID, or a path to a single bag directory. Defaults to newest.")
        arg.completer = completion.missions_or_folders  # type: ignore[attr-defined]
        parser.add_argument(
            "--output", "-o",
            help="directory to write repaired recordings into "
                 "(default: ./<name>_repaired)")
        parser.add_argument(
            "--all", action="store_true",
            help="re-stamp every recording, not only the bad-clock ones")
        parser.add_argument(
            "--duration", type=float,
            help="target playback length in seconds for each repaired recording")
        parser.add_argument(
            "--force", action="store_true",
            help="write into the output directory even if it isn't empty")
        parser.add_argument(
            "--json", action="store_true",
            help="machine-readable output for scripts")
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        return guarded_main(run, args)
