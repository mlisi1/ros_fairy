"""ros2 fairy mission_delete — permanently remove saved missions.

One mission by ID, every mission (``--all``) or today's (``--today``). Every
form explains what is deleted, lists the missions, asks for confirmation and
then requires the operator to type :data:`PHRASE` — there is no undo.

The batch forms ask for sudo first and delete with it, so missions saved by
other operator accounts (whose crates they own) can be removed too. Deletion
only ever touches folders that are direct children of the archive directory.
"""

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from ros_fairy.archive import index
from ros_fairy.subcommands import VerbExtension, _configure_logging, guarded_main
from ros_fairy.ui.review import human_size
from ros_fairy.utils import paths

PHRASE = "I understand: flames and fire"


def _local_date(iso: str):
    try:
        return datetime.fromisoformat(iso).astimezone().date()
    except ValueError:
        return None


def _select(args, console: Console) -> list[dict] | None:
    """The index rows to delete, or None after reporting a usage error."""
    mission_id = getattr(args, "mission_id", None)
    all_mode = getattr(args, "all", False)
    today_mode = getattr(args, "today", False)
    if sum([bool(mission_id), all_mode, today_mode]) != 1:
        console.print("[red]Give one mission ID, or --all, or --today.[/red]")
        return None
    rows, _ = index.query(limit=None)
    if mission_id:
        # Exact IDs only: unlike export/verify, no "1 = newest" or paths here —
        # a typo must not delete the wrong mission.
        rows = [r for r in rows if r["mission_id"] == mission_id]
        if not rows:
            console.print(f"[red]There's no saved mission with ID "
                          f"'{mission_id}'. `ros2 fairy list` shows the IDs."
                          "[/red]")
            return None
    elif today_mode:
        today = datetime.now().astimezone().date()
        rows = [r for r in rows if _local_date(r["created_at"]) == today]
    return rows


def _explain(console: Console, rows: list[dict], batch: bool) -> None:
    table = Table(border_style="dim")
    for col in ("Date", "Mission ID", "Mission", "Size"):
        table.add_column(col, no_wrap=col == "Mission ID")
    for r in sorted(rows, key=lambda r: r["created_at"]):
        try:
            when = datetime.fromisoformat(r["created_at"]).astimezone() \
                .strftime("%Y-%m-%d %H:%M")
        except ValueError:
            when = r["created_at"]
        table.add_row(when, r["mission_id"], r["goal"],
                      human_size(r["size_bytes"]) if r["size_bytes"] else "")
    n = len(rows)
    total = sum(r["size_bytes"] or 0 for r in rows)
    lines = [
        f"This permanently deletes {n} saved mission{'s' if n != 1 else ''} "
        f"({human_size(total)}) from this robot: the archive folder — "
        "including the copies of the recordings inside it, the captured "
        "context and the manifests — and the entry in `ros2 fairy list`. "
        "[bold]There is no undo.[/bold]",
        "",
        "Not touched: recordings made outside ros-fairy (they stay where "
        "they were recorded), export bundles you already created, and the "
        "mission in progress, if any.",
    ]
    if batch:
        lines += ["", "Deleting several missions needs administrator (sudo) "
                      "rights; you'll be asked for your password."]
    console.print(Panel("\n".join(lines), title="Delete missions",
                        border_style="red"))
    console.print(table)


def _have_sudo(console: Console) -> bool:
    if os.geteuid() == 0:
        return True
    try:
        ok = subprocess.run(["sudo", "-v"]).returncode == 0
    except FileNotFoundError:
        ok = False
    if not ok:
        console.print("[red]Administrator rights weren't granted — nothing "
                      "was deleted.[/red]")
    return ok


def _safe_target(row: dict) -> Path | None:
    """The mission's folder, only if it is a direct child of the archive."""
    root = paths.archive_dir().resolve()
    target = Path(row["archive_path"])
    try:
        resolved = target.resolve()
    except OSError:
        return None
    if resolved.parent != root or resolved.name.startswith(".") \
            or target.is_symlink():
        return None
    return resolved


def _remove(target: Path, sudo: bool) -> None:
    if not target.exists():
        return  # already gone: just forget it in the index
    if sudo:
        subprocess.run(["sudo", "rm", "-rf", "--", str(target)], check=True)
    else:
        shutil.rmtree(target)


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()

    if not paths.index_db_path().is_file():
        console.print("No missions have been saved on this robot yet.")
        return 0
    try:
        rows = _select(args, console)
    except index.IndexUnavailableError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    if rows is None:
        return 1
    if not rows:
        console.print("There are no saved missions recorded today."
                      if getattr(args, "today", False)
                      else "There are no saved missions.")
        return 0
    if not sys.stdin.isatty():
        console.print("[red]mission_delete needs an interactive terminal: "
                      "you have to type a confirmation phrase.[/red]")
        return 1

    batch = not getattr(args, "mission_id", None)
    _explain(console, rows, batch)
    n = len(rows)
    if not Confirm.ask(f"Delete {'these' if n != 1 else 'this'} {n} "
                       f"mission{'s' if n != 1 else ''}?", default=False,
                       console=console):
        console.print("Nothing was deleted.")
        return 0
    typed = Prompt.ask(f'Type [bold]{PHRASE}[/bold] to confirm',
                       console=console, default="", show_default=False)
    if typed.strip() != PHRASE:
        console.print("That's not the phrase — nothing was deleted.")
        return 1
    if batch and not _have_sudo(console):
        return 1

    deleted, failed = 0, []
    stale: list[tuple[str, Exception]] = []
    for row in rows:
        target = _safe_target(row)
        if target is None:
            failed.append((row["mission_id"], "its folder is not inside the "
                           "archive, so it was left alone"))
            continue
        try:
            _remove(target, sudo=batch)
        except PermissionError:
            if batch or not Confirm.ask(
                    "This mission was saved by another account. Use "
                    "administrator (sudo) rights to delete it?",
                    default=False, console=console) \
                    or not _have_sudo(console):
                failed.append((row["mission_id"], "permission denied"))
                continue
            try:
                _remove(target, sudo=True)
            except (OSError, subprocess.CalledProcessError) as exc:
                failed.append((row["mission_id"], str(exc)))
                continue
        except (OSError, subprocess.CalledProcessError) as exc:
            failed.append((row["mission_id"], str(exc)))
            continue
        deleted += 1
        try:
            index.delete(row["mission_id"])
        except Exception as exc:
            # The folder is already gone: say so, and how to fix the list.
            stale.append((row["mission_id"], exc))

    if deleted:
        console.print(f"[green]Deleted {deleted} mission"
                      f"{'s' if deleted != 1 else ''}.[/green]")
    for mission_id, why in failed:
        console.print(f"[red]Not deleted: {mission_id} — {why}.[/red]")
    if stale:
        ids = ", ".join(m for m, _ in stale)
        console.print(f"[yellow]Deleted, but the mission list couldn't be "
                      f"updated for {ids} ({stale[0][1]}). Run "
                      "[bold]ros2 fairy reindex[/bold] to bring it up to "
                      "date.[/yellow]")
    return 1 if failed or stale else 0


class MissionDeleteVerb(VerbExtension):
    """Permanently delete saved missions (asks, then asks you to type a phrase)."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument(
            "mission_id", nargs="?", metavar="MISSION_ID",
            help="the mission to delete, e.g. m-20261002-082109-1a2b "
                 "(shown by `ros2 fairy list`)")
        parser.add_argument(
            "--all", action="store_true",
            help="delete every saved mission (needs sudo)")
        parser.add_argument(
            "--today", action="store_true",
            help="delete every mission recorded today (needs sudo)")
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        return guarded_main(run, args)
