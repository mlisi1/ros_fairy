"""ros2 fairy adopt — pull a recording the watchdog never saw into the mission.

The dashcam captures recordings automatically: those made via
``ros2 fairy mission_record`` (into the spool) and those started in another
terminal (found by the watchdog's ``/proc`` recorder-process poller). ``adopt``
is the manual escape hatch for what slips through — a bag recorded while the
watchdog was down, copied from another machine, or otherwise out of reach.

It runs the same processing the watchdog's FINALISING step would (parse
``metadata.yaml``, topic-health analysis) and appends a ``source="adopted"`` bag
entry to ``harvest.json``, referenced in place and copied into the crate at
``mission_close``.
"""

from pathlib import Path

from rich.console import Console

from ros_fairy.manifest import builder
from ros_fairy.subcommands import (
    VerbExtension,
    _configure_logging,
    guarded_main,
    json_error,
    print_json,
)
from ros_fairy.utils import fsio, paths
from ros_fairy.utils import topic_health as th
from ros_fairy.watchdog import watchdog


def _is_bag_dir(bag_dir: Path) -> bool:
    if not (bag_dir / "metadata.yaml").is_file():
        return False
    try:
        return any(f.name.endswith(watchdog.STORAGE_SUFFIXES)
                   for f in bag_dir.iterdir())
    except OSError:
        return False


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()
    want_json = getattr(args, "json", False)

    bag_dir = Path(args.bagdir).expanduser()
    try:
        bag_dir = bag_dir.resolve()
    except OSError:
        pass

    if not bag_dir.is_dir():
        return _fail(console, want_json,
                     f"There's no folder at {args.bagdir}.")
    if not _is_bag_dir(bag_dir):
        return _fail(console, want_json,
                     f"{bag_dir.name} doesn't look like a recording "
                     "(no metadata.yaml and storage file).")

    # Busy guard — one bag, one mission: don't race the watchdog mid-recording.
    state = watchdog.read_state()
    if state and state.get("state") == "RECORDING":
        return _fail(console, want_json,
                     "The assistant is busy with another recording. Try again "
                     "once it has finished.")

    harvest_doc, _ = builder.load_spool()
    if str(bag_dir) in {b.get("path")
                        for b in (harvest_doc or {}).get("bags", [])}:
        msg = "That recording is already part of the current mission."
        if want_json:
            print_json({"bag": bag_dir.name, "source": "adopted",
                        "path": str(bag_dir), "status": "already_adopted"})
        else:
            console.print(f"[yellow]{msg}[/yellow]")
        return 0

    # No context captured yet (watchdog never ran, or ROS was down): best-effort
    # harvest now so the adopted bag still gets whatever is capturable.
    if harvest_doc is None:
        if not want_json:
            console.print("Capturing what I can about the robot and "
                          "software…")
        fsio.atomic_write_json(paths.harvest_json_path(), watchdog.run_pipeline())

    watchdog.append_bag_record(bag_dir, source="adopted")

    harvest_doc, _ = builder.load_spool()
    bag: dict = next((b for b in (harvest_doc or {}).get("bags", [])
                      if b.get("path") == str(bag_dir)), {})
    warnings = bag.get("health_warnings", [])
    if want_json:
        print_json({"bag": bag_dir.name, "source": "adopted",
                    "path": str(bag_dir), "status": "adopted",
                    "health_warnings": len(warnings)})
        return 0

    dur = th.humanize_duration(bag["duration_s"]) \
        if bag.get("duration_s") else "length unknown"
    size = f"{bag.get('size_bytes', 0) / 1e9:.1f} GB"
    console.print(f"[green]Adopted[/green] {bag_dir.name} ({dur}, {size}).")
    if warnings:
        console.print(f"[yellow]Note:[/yellow] {warnings[0]['plain_text']}")
    console.print("Run [bold]ros2 fairy mission_close[/bold] to review and save.")
    return 0


def _fail(console: Console, want_json: bool, msg: str) -> int:
    if want_json:
        return json_error(msg)
    else:
        console.print(f"[red]{msg}[/red]")
    return 1


class AdoptVerb(VerbExtension):
    """Ingest a bag recorded outside mission_record into the current mission."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument(
            "bagdir",
            help="path to a rosbag2 recording directory (metadata.yaml + "
                 "storage file) to adopt into the current mission")
        parser.add_argument(
            "--json", action="store_true",
            help="machine-readable output for scripts")
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        return guarded_main(run, args)
