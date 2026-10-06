"""ros2 fairy mission_close — the single save/discard decision."""

import shutil
import sys
import time

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm  # noqa: F401 (tests patch Confirm.ask)

from ros_fairy.archive import assembler
from ros_fairy.manifest import builder, validator
from ros_fairy.subcommands import (
    VerbExtension,
    _configure_logging,
    confirm,
    guarded_main,
)
from ros_fairy.ui import briefing, review
from ros_fairy.utils import fsio, paths
from ros_fairy.watchdog import watchdog as wd


def _recording_in_progress() -> bool:
    state = wd.read_state()
    if state is None or state.get("state") != "RECORDING":
        return False
    from ros_fairy.ui.status import watchdog_alive
    return watchdog_alive(state)


def _wait_for_finalising(console: Console) -> bool:
    """Wait out an in-progress FINALISING instead of racing it.

    A recording shorter than the harvest pipeline (robot identity, ROS
    graph, docker info, ...) leaves the watchdog in FINALISING — not
    RECORDING — for up to :data:`wd.HARVEST_WAIT_S`, appending the bag to
    harvest.json only once it's done. ``_recording_in_progress`` only checks
    for the literal "RECORDING" state, so without this, a `mission_close`
    run in that window would find no bags yet and wrongly report nothing
    was recorded (reported 2026-09-10 against a bag whose harvest took 61s
    to finish after a ~4s recording).

    Returns False when the watchdog is still busy after the whole wait; a
    watchdog that died meanwhile ends the wait at once (the state file it
    left says FINALISING forever).
    """
    from ros_fairy.ui.status import watchdog_alive
    state = wd.read_state()
    if state is None or state.get("state") != "FINALISING":
        return True
    deadline = time.monotonic() + wd.HARVEST_WAIT_S + 5
    with Progress(SpinnerColumn(),
                  TextColumn("[progress.description]{task.description}"),
                  console=console, transient=True) as progress:
        progress.add_task(
            "Finishing up the last recording's context capture…", total=None)
        while True:
            if not watchdog_alive(state):
                console.print("[yellow]The recording assistant stopped while "
                              "finishing the last recording; going on with "
                              "what it had saved.[/yellow]")
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(1)
            state = wd.read_state()
            if state is None or state.get("state") != "FINALISING":
                return True
    console.print("[yellow]The recording assistant is still capturing the "
                  "last recording's context. Try again in a minute; if this "
                  "keeps happening, run [bold]ros2 fairy doctor[/bold]."
                  "[/yellow]")
    return False


def _salvage_bags(harvest: dict | None) -> dict | None:
    """If the watchdog never finalised, build bag records ourselves.

    Dashcam principle: closed bag directories sitting in the spool must be
    archivable even if the assistant was down the whole time.
    """
    known = {b["path"] for b in (harvest or {}).get("bags", [])}
    spool_bags = sorted(p for p in paths.bags_dir().glob("*") if p.is_dir()) \
        if paths.bags_dir().is_dir() else []
    missing = [b for b in spool_bags if str(b) not in known]
    if not missing:
        return harvest
    if harvest is None:
        harvest = builder.compose_harvest(
            None, None, None, None, None,
            {m: "failed" for m in builder.HARVEST_MODULES})
        with fsio.locked(paths.harvest_lock_path()):
            if not paths.harvest_json_path().exists():
                fsio.atomic_write_json(paths.harvest_json_path(), harvest)
    for bag_dir in missing:
        if (bag_dir / "metadata.yaml").is_file() or \
                any(f.suffix in (".db3", ".mcap") for f in bag_dir.iterdir()):
            wd.append_bag_record(bag_dir)
    return builder.load_spool()[0]


def _discard_spool() -> None:
    if paths.bags_dir().is_dir():
        for bag in paths.bags_dir().glob("*"):
            shutil.rmtree(bag, ignore_errors=True)
    for f in (paths.harvest_json_path(), paths.mission_context_path(),
              paths.session_env_path(), paths.watchdog_log_path()):
        f.unlink(missing_ok=True)


def _recover_pending(console: Console, yes: bool = False) -> int | None:
    """Deal with saves a crash, power cut or Ctrl-C left unfinished.

    Returns an exit code when this run should stop here, None to carry on
    with the current mission.
    """
    for pending in assembler.pending_saves():
        if pending.kind == assembler.DISCARD:
            # Only copies were made; the originals are untouched.
            assembler.discard_pending(pending)
            continue
        if pending.kind == assembler.STUCK:
            console.print(f"[yellow]An earlier save ({pending.name}) was cut "
                          f"off and can't be finished automatically: "
                          f"{pending.reason}. It is left as it is; ask your "
                          "robot engineer to look at it.[/yellow]")
            continue
        notices: list[str] = []
        if pending.kind == assembler.TIDY:
            final = assembler.finish_pending(pending, warn=notices.append)
            console.print(f"Finished tidying up after saving {final.name}.")
            _print_notices(console, notices)
            continue
        what = f'"{pending.goal}"' if pending.goal else pending.name
        console.print(f"A previous save ({what}) was interrupted. Your "
                      "recordings are safe.")
        if not confirm("Finish saving it now?", default=True,
                       console=console, assume_yes=yes):
            console.print("Nothing was changed. The interrupted save is "
                          "kept and will be offered again the next time you "
                          "run mission_close.")
            return 0
        try:
            final = assembler.finish_pending(pending, warn=notices.append)
        except assembler.AssemblyError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
        console.print(Panel(f"Mission saved: [bold]{final.name}[/bold]",
                            border_style="green"))
        _print_notices(console, notices)
        return 0
    return None


def _already_saved(console: Console) -> bool:
    """The spool still holds a mission that is already in the archive.

    A crash between saving and clearing the spool leaves it behind; saving it
    again would make a second crate with the same mission ID.
    """
    context = builder.load_spool()[1]
    mission_id = ((context or {}).get("identity") or {}).get("mission_id")
    crate = assembler.saved_crate_for(mission_id)
    if crate is None:
        return False
    assembler.clear_saved_spool(crate, mission_id)
    console.print(f"This mission was already saved as [bold]{crate.name}"
                  "[/bold]; I've cleared what it left behind in the spool. "
                  "Anything recorded after it is kept for the next save.")
    return True


def _print_notices(console: Console, notices: list[str]) -> None:
    for notice in notices:
        console.print(f"[yellow]{notice}[/yellow]")


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()
    try:
        with assembler.save_lock():
            return _run_locked(args, console)
    except assembler.SaveInProgressError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        return 1
    except assembler.AssemblyError as exc:  # e.g. no access to the archive
        console.print(f"[red]{exc}[/red]")
        return 1


def _run_locked(args, console: Console) -> int:
    yes = getattr(args, "yes", False)
    rc = _recover_pending(console, yes)
    if rc is not None:
        return rc
    if _already_saved(console):
        return 0

    if _recording_in_progress():
        console.print("[yellow]It looks like recording is still in "
                      "progress. Stop it first (Ctrl-C in the recording "
                      "window), then run this again.[/yellow]")
        return 1

    if not _wait_for_finalising(console):
        return 1

    # A bag finalised before its recorder had closed it is re-read now.
    wd.refresh_salvaged_records()
    harvest, context = builder.load_spool()
    harvest = _salvage_bags(harvest)
    if not (harvest or {}).get("bags"):
        console.print("There's nothing recorded yet. If you recorded outside "
                      "ros-fairy while the recording assistant wasn't "
                      "running, attach the recording first with: "
                      "[bold]ros2 fairy adopt <folder>[/bold]")
        return 1
    assert harvest is not None  # a None harvest has no bags, handled above

    # Now that the bags are known, correct sensor liveness against what was
    # actually recorded (the live-graph sample at mission start can be blind).
    builder.reconcile_sensor_detection(harvest)
    fsio.atomic_write_json(paths.harvest_json_path(), harvest)

    missing = validator.missing_user_fields(context)
    if missing:
        try:
            answers = briefing.ask_missing(missing, console=console)
        except EOFError:
            console.print("\n[red]This mission has no briefing yet ("
                          f"{', '.join(m.replace('_', ' ') for m in missing)}"
                          "), and there's no terminal to ask in. Run "
                          "[bold]ros2 fairy mission_start[/bold] first, or "
                          "close the mission in a terminal.[/red]")
            return 1
        if context is None:
            context = builder.new_mission_context(
                operator_name=answers.get("operator_name", ""),
                goal=answers.get("goal", ""),
                location_name=answers.get("location_name", ""))
        else:
            for field, value in answers.items():
                section = "identity" if field == "operator_name" else "intent"
                context.setdefault(section, {})[field] = value
        fsio.atomic_write_json(paths.mission_context_path(), context)

    # missing_user_fields(None) always reports all required fields, so a None
    # context above is always replaced before reaching here.
    assert context is not None
    existing_notes = (context.get("intent") or {}).get("notes")
    note_arg = getattr(args, "note", None)
    if note_arg is not None:
        new_notes = note_arg.strip() or None
    elif sys.stdin.isatty():
        new_notes = briefing.ask_notes(console, default=existing_notes)
    else:
        # Non-interactive (piped/scripted): keep whatever notes exist rather
        # than blocking on a prompt nobody can answer.
        new_notes = existing_notes
    if new_notes != existing_notes:
        context.setdefault("intent", {})["notes"] = new_notes
        fsio.atomic_write_json(paths.mission_context_path(), context)

    try:
        record = builder.build(harvest, context)
    except builder.ManifestError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    from ros_fairy.manifest import quality as quality_mod
    quality = quality_mod.assess(record, harvest)
    record.provenance.data_quality = quality.level

    from ros_fairy.archive import duplicates
    dup_msg = duplicates.describe(record, duplicates.find_similar(record))
    dup_msgs = [dup_msg] if dup_msg else []
    exact_row = duplicates.find_exact_duplicate(record)
    exact_msg = duplicates.describe_exact(exact_row) if exact_row else None

    review.show_summary(record, builder.harvest_level_warnings(harvest),
                        console=console, quality=quality, duplicates=dup_msgs,
                        exact_duplicate=exact_msg)
    decision = review.confirm_save(
        console=console, risky=quality.level == quality_mod.POOR,
        assume_yes=yes)

    if decision == "save":
        notices: list[str] = []
        try:
            with Progress(SpinnerColumn(),
                          TextColumn("[progress.description]{task.description}"),
                          console=console, transient=True) as progress:
                task = progress.add_task("Saving mission…", total=None)
                final = assembler.assemble(
                    record, harvest,
                    progress=lambda msg: progress.update(task,
                                                         description=msg),
                    warn=notices.append)
        except assembler.AssemblyError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
        console.print(Panel(f"Mission saved: [bold]{final.name}[/bold]",
                            border_style="green"))
        _print_notices(console, notices)
        return 0

    if decision == "discard":
        _discard_spool()
        console.print("Recording discarded.")
        return 0

    console.print("Nothing was changed — the recording is still in the "
                  "spool.")
    return 0


class MissionCloseVerb(VerbExtension):
    """Review the finished mission and decide: save it or discard it."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")
        parser.add_argument(
            "--note", metavar="TEXT",
            help="post-mission notes (skips the interactive prompt)")
        parser.add_argument(
            "--yes", "-y", action="store_true",
            help="save without asking (also finishes an interrupted save); "
                 "for scripts — the briefing must already exist")

    def main(self, *, args):
        return guarded_main(run, args)
