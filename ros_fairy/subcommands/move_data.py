"""ros2 fairy move_data — move the recordings and saved missions elsewhere.

For a robot whose missions should live on a bigger disk. Moves the whole
data directory (spool, archive, index, state, log), records the new place in
``/etc/ros-fairy/paths.yaml`` (read by every shell and the service, so no new
login is needed), rebuilds the mission list and restarts the watchdog.

Checks and the confirmation run as the operator; the move itself re-runs
this module as root through sudo, like ``setup``'s apply step.

Same disk: a rename, instant. Another disk: a copy into a hidden folder next
to the target (inside it, if it already exists), compared file by file
(sizes) with the original, then renamed into place; only then is the
original removed. If anything fails before that, the original is untouched
and the watchdog is restarted on it.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from rich.console import Console

from ros_fairy.subcommands import (
    VerbExtension,
    _configure_logging,
    confirm,
    guarded_main,
)
from ros_fairy.utils import paths

SERVICE_NAME = "ros-fairy-watchdog.service"


class MoveRefused(Exception):
    """Why the move can't happen now, in plain words."""


def _tree(root: Path) -> dict[str, int]:
    """Every file, folder and link below ``root``: relative path -> size
    (-1 for folders, -2 for links)."""
    found: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for name in dirnames:
            path = base / name
            found[str(path.relative_to(root))] = \
                -2 if path.is_symlink() else -1
        for name in filenames:
            path = base / name
            found[str(path.relative_to(root))] = \
                -2 if path.is_symlink() else path.lstat().st_size
    return found


def _size(tree: dict[str, int]) -> int:
    return sum(size for size in tree.values() if size > 0)


def _same_disk(a: Path, b: Path) -> bool:
    return os.stat(a).st_dev == os.stat(b).st_dev


def _watchdog_busy() -> bool:
    from ros_fairy.subcommands import mission_close
    from ros_fairy.ui.status import watchdog_alive
    from ros_fairy.watchdog import watchdog as wd
    state = wd.read_state()
    # A watchdog that died mid-finalise left FINALISING behind: not busy.
    finalising = (state or {}).get("state") == "FINALISING" \
        and watchdog_alive(state)
    return mission_close._recording_in_progress() or finalising


def check(target_arg: str) -> tuple[Path, Path, dict[str, int], bool]:
    """(current, target, what's there, same disk), or MoveRefused."""
    if os.environ.get(paths.VAR_DIR_ENV):
        raise MoveRefused(
            f"{paths.VAR_DIR_ENV} is set in this shell ({paths.var_dir()}), "
            "so this shell doesn't use the robot's configured place. Unset "
            "it and try again.")
    if not os.path.isabs(target_arg):
        raise MoveRefused(f"{target_arg} isn't an absolute path (start it "
                          "with /).")
    current = paths.var_dir()
    target = Path(os.path.normpath(target_arg))
    if target == current:
        raise MoveRefused(f"The missions are already kept in {current}.")
    if current in target.parents or target in current.parents:
        raise MoveRefused(f"{target} and {current} are inside one another; "
                          "choose a separate folder.")
    if target == paths.config_dir() or paths.config_dir() in target.parents:
        raise MoveRefused(f"{target} is inside the configuration folder.")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise MoveRefused(f"{target} already exists and isn't an empty "
                          "folder. Choose a new or empty one.")
    if not current.is_dir():
        raise MoveRefused(f"There's nothing to move: {current} doesn't "
                          "exist. To choose where missions go on a robot "
                          "that has none yet, use `ros2 fairy setup "
                          "--var-dir`.")
    spool = paths.spool_dir()
    if paths.mission_context_path().exists() or \
            paths.harvest_json_path().exists() or \
            (paths.bags_dir().is_dir() and any(paths.bags_dir().iterdir())):
        raise MoveRefused(
            "A mission is open (briefing or recordings in "
            f"{spool}). Save it with `ros2 fairy mission_close` or abandon "
            "it with `ros2 fairy mission_abort` first.")
    from ros_fairy.archive import assembler
    if assembler.pending_saves():
        raise MoveRefused("A save is unfinished. Run `ros2 fairy "
                          "mission_close` to finish it first.")
    if _watchdog_busy():
        raise MoveRefused("The recording assistant is busy with a "
                          "recording. Wait until it's done.")
    tree = _tree(current)
    # An existing (empty) target may be the mount point of another disk: its
    # own filesystem is the one that counts, not its parent's.
    parent = target if target.exists() else target.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    same_disk = _same_disk(parent, current)
    if not same_disk:
        free = shutil.disk_usage(parent).free
        need = _size(tree)
        if free < need * 1.05 + (1 << 30):
            from ros_fairy.ui.review import human_size
            raise MoveRefused(
                f"There isn't enough room: the missions take "
                f"{human_size(need)} and {parent} has {human_size(free)} free "
                "(with 1 GiB to spare).")
    return current, target, tree, same_disk


def _systemctl(*args: str) -> bool:
    try:
        return subprocess.run(["systemctl", *args], capture_output=True,
                              check=False).returncode == 0
    except OSError:
        return False


def _start_watchdog(console: Console) -> None:
    if not Path("/etc/systemd/system", SERVICE_NAME).exists():
        return
    _systemctl("start", SERVICE_NAME)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _systemctl("is-active", "--quiet", SERVICE_NAME):
            return
        time.sleep(0.5)
    console.print("[yellow]The recording assistant didn't come back within "
                  "10 s. Check: journalctl -u ros-fairy-watchdog[/yellow]")


def _copy(console: Console, current: Path, target: Path) -> None:
    """Copy ``current`` to ``target`` through a hidden folder, checking it
    against the original as it is now (the watchdog is stopped).

    The hidden folder sits next to a new target, or inside an existing
    (empty) one, so it is always on the target's disk — an empty target
    may be the mount point of that disk."""
    existing = target.exists()
    work = (target if existing else target.parent) / f".{target.name}.moving"
    if work.exists():
        shutil.rmtree(work)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        before = _tree(current)
        # cp -a keeps owners, the ros-fairy group, modes and times.
        result = subprocess.run(
            ["cp", "-a", "--no-target-directory", str(current), str(work)],
            capture_output=True, text=True)
        if result.returncode != 0:
            raise MoveRefused(f"Copying failed: {result.stderr.strip()}")
        after = _tree(current)
        if after != before:
            raise MoveRefused("Something changed the missions while they "
                              "were being copied; nothing was changed. Try "
                              "again when nobody is using ros2 fairy.")
        copied = _tree(work)
        if copied != after:
            different = sorted(set(copied.items()) ^ set(after.items()))
            raise MoveRefused(
                "The copy doesn't match the original (first difference: "
                f"{different[0][0]}); nothing was changed.")
        if existing:
            # Checked empty beforehand; keep the folder (it may be a mount
            # point), give it the original's owner and mode, and move the
            # copy's contents into it.
            info = current.stat()
            os.chown(target, info.st_uid, info.st_gid)
            shutil.copystat(current, target)
            for entry in work.iterdir():
                entry.rename(target / entry.name)
            work.rmdir()
        else:
            work.rename(target)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        if existing:  # it was empty: undo a half-done move into it
            for entry in list(target.iterdir()):
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
        raise


def _settle_index() -> None:
    """Fold the index's write-ahead log into index.db, so the copy is
    compared against files nothing else will rewrite on its own (SQLite
    does that when the last connection closes)."""
    import sqlite3
    db = paths.index_db_path()
    if not db.is_file():
        return
    try:
        con = sqlite3.connect(db, timeout=5.0)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            con.close()
    except sqlite3.Error:
        pass  # a copy check that then differs says so


def move(console: Console, current: Path, target: Path,
         same_disk: bool) -> int:
    """The root part: stop, move, record, reindex, restart."""
    from ros_fairy.archive import index
    from ros_fairy.subcommands import setup

    _systemctl("stop", SERVICE_NAME)
    _settle_index()
    try:
        if same_disk:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.rmdir()
            current.rename(target)
            try:
                setup.write_paths_file(target)
            except BaseException:
                target.rename(current)  # paths.yaml still names current
                raise
        else:
            console.print(f"Copying to {target}…")
            _copy(console, current, target)
            setup.write_paths_file(target)
    except BaseException:
        _start_watchdog(console)
        raise
    try:
        if not same_disk:
            try:
                shutil.rmtree(current)
            except OSError as exc:
                console.print(f"[yellow]The missions moved, but the old "
                              f"folder {current} couldn't be removed "
                              f"({exc}). Remove it by hand.[/yellow]")
        try:
            count = index.reindex()
        except Exception as exc:  # the data has moved; the list can be rebuilt
            console.print(f"[yellow]The missions moved, but rebuilding the "
                          f"mission list failed ({exc}). Run `ros2 fairy "
                          "reindex`.[/yellow]")
            count = None
    finally:
        _start_watchdog(console)
    listed = f" ({count} saved mission{'s' if count != 1 else ''})" \
        if count is not None else ""
    console.print(f"[green]Done: missions are now kept in {target}{listed}."
                  "[/green] Every shell, and the recording assistant, uses "
                  "the new place from now on.")
    return 0


def _as_root_via_sudo(console: Console, target: Path) -> int:
    sudo = shutil.which("sudo")
    if sudo is None:
        console.print("[red]Moving the missions needs root, and `sudo` isn't "
                      "available. Run this command as root.[/red]")
        return 1
    console.print("[dim]Moving needs root: you may be asked for your "
                  "password.[/dim]")
    pkg_root = str(Path(__file__).resolve().parents[2])
    result = subprocess.run(
        [sudo, "--preserve-env=ROS_FAIRY_CONFIG_DIR,PYTHONPATH",
         sys.executable, "-m", "ros_fairy.subcommands.move_data",
         str(target), "--confirmed"],
        env={**os.environ, "PYTHONPATH": pkg_root})
    return result.returncode


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()
    try:
        current, target, tree, same_disk = check(args.target)
    except MoveRefused as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    if not getattr(args, "confirmed", False):
        from ros_fairy.ui.review import human_size
        crates = [p for p in paths.archive_dir().iterdir()
                  if p.is_dir() and not p.name.startswith(".")] \
            if paths.archive_dir().is_dir() else []
        how = ("same disk: a rename, instant" if same_disk else
               "another disk: a copy, checked, then the original is removed")
        console.print(f"Move {len(crates)} saved mission(s), "
                      f"{human_size(_size(tree))} in all,\n"
                      f"  from {current}\n  to   {target}\n({how}). The "
                      "recording assistant stops while this runs.")
        if not confirm("Move them?", default=True, console=console,
                       assume_yes=getattr(args, "yes", False)):
            console.print("Nothing was changed.")
            return 0
    if os.geteuid() != 0:
        return _as_root_via_sudo(console, target)
    from ros_fairy.archive import assembler
    try:
        # No mission_close may start while the folder moves under it.
        with assembler.save_lock():
            return move(console, current, target, same_disk)
    except assembler.SaveInProgressError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    except MoveRefused as exc:
        console.print(f"[red]{exc}[/red] The missions are still in "
                      f"{current}.")
        return 1


def _add_arguments(parser) -> None:
    parser.add_argument("target", metavar="DIR",
                        help="the new folder for recordings and saved "
                             "missions (new or empty), e.g. /data/ros-fairy")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="don't ask before moving")
    parser.add_argument("--debug", action="store_true",
                        help="verbose logging to stderr (for engineers)")
    # internal: the root re-exec, after the operator confirmed
    parser.add_argument("--confirmed", action="store_true",
                        help=argparse.SUPPRESS)


class MoveDataVerb(VerbExtension):
    """Move the recordings and saved missions to another folder or disk."""

    def add_arguments(self, parser, cli_name):
        _add_arguments(parser)

    def main(self, *, args):
        return guarded_main(run, args)


def main(argv: list[str] | None = None) -> None:
    """The sudo re-exec target (``python3 -m ros_fairy.subcommands.move_data``)."""
    parser = argparse.ArgumentParser(prog="ros2 fairy move_data",
                                     description=MoveDataVerb.__doc__)
    _add_arguments(parser)
    sys.exit(guarded_main(run, parser.parse_args(argv)))


if __name__ == "__main__":
    main()
