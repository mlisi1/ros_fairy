"""ros2 fairy mission_record — safe wrapper around ros2 bag record."""

import shutil
import signal
import subprocess
from datetime import datetime

from rich.console import Console
from rich.prompt import Confirm

from ros_fairy.harvest import robot_identity
from ros_fairy.subcommands import VerbExtension, _configure_logging, guarded_main
from ros_fairy.utils import clock, paths, ros_env

MIN_FREE_BYTES = 1 << 30  # 1 GiB


def build_record_command(output_dir: str) -> list[str]:
    """The exact subprocess invocation."""
    topics: list[str] | None = None
    storage: str | None = None
    try:
        recording = robot_identity.harvest()["recording"]
        topics = recording.get("topics")
        storage = recording.get("storage")
    except robot_identity.RobotIdentityError:
        pass
    cmd = ["ros2", "bag", "record"]
    cmd += topics if topics else ["--all"]
    if storage:
        cmd += ["--storage", storage]
    cmd += ["--output", output_dir]
    return cmd


def _bag_prefix() -> str:
    from ros_fairy.manifest import builder
    context = builder.load_spool()[1]
    mission_id = (context or {}).get("identity", {}).get("mission_id")
    return mission_id or "unbriefed"


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()

    if shutil.which("ros2") is None:
        console.print("[red]I can't find ROS 2. Make sure the robot "
                      "software is started, then try again.[/red]")
        return 1

    paths.bags_dir().mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(paths.spool_dir()).free
    if free < MIN_FREE_BYTES:
        proceed = Confirm.ask(
            "Disk space is very low — the recording may stop early. "
            "Record anyway?", default=False, console=console)
        if not proceed:
            return 1

    from ros_fairy.manifest import builder
    if builder.load_spool()[1] is None:
        proceed = Confirm.ask(
            "No mission briefing yet — recording will still work, and "
            "you'll be asked the briefing questions when you close the "
            "mission. Continue?", default=True, console=console)
        if not proceed:
            return 0

    from ros_fairy.subcommands.mission_start import _watchdog_alive
    if not _watchdog_alive():
        console.print("[yellow]The background recording assistant isn't "
                      "running, so some context about this recording may "
                      "not be captured.[/yellow]")

    if clock.is_synchronized() is False:
        proceed = Confirm.ask(
            f"{clock.WARNING}\nRecord anyway?", default=False, console=console)
        if not proceed:
            return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = str(paths.bags_dir() / f"{_bag_prefix()}_{stamp}")
    command = build_record_command(output)
    console.print(f"[dim]Recording to {output} — press Ctrl-C to stop.[/dim]")

    # Hand this shell's ROS environment to the watchdog now that recording is
    # actually starting (every preflight passed) — the recorder has the correct
    # env, the watchdog only a frozen copy it must match (#29). Written here, not
    # earlier, so an aborted preflight never leaves a stale handoff behind.
    ros_env.write_file(paths.session_env_path(), ros_env.capture(),
                       mode=0o664)

    child = subprocess.Popen(command)
    try:
        returncode = child.wait()
    except KeyboardInterrupt:
        # rosbag2 needs a clean SIGINT to write metadata.yaml
        child.send_signal(signal.SIGINT)
        returncode = child.wait()
        console.print("\nRecording stopped. When the mission is over, run: "
                      "[bold]ros2 fairy mission_close[/bold]")
        return 0
    if returncode != 0:
        console.print("[red]Recording stopped with a problem. The data "
                      "captured so far is kept.[/red]")
        return 1
    console.print("Recording finished. When the mission is over, run: "
                  "[bold]ros2 fairy mission_close[/bold]")
    return 0


class MissionRecordVerb(VerbExtension):
    """Record mission data (wraps ros2 bag record with safety checks)."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        return guarded_main(run, args)
