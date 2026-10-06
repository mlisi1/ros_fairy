"""ros2 fairy mission_record — safe wrapper around ros2 bag record."""

import os
import shutil
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console

from ros_fairy.harvest import robot_identity
from ros_fairy.subcommands import (
    VerbExtension,
    _configure_logging,
    confirm,
    guarded_main,
)
from ros_fairy.utils import clock, paths, ros_env

MIN_FREE_BYTES = 1 << 30  # 1 GiB
# Ctrl-C presses after the first before the recorder is forced to stop.
FORCE_AFTER_PRESSES = 2


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
    yes = getattr(args, "yes", False)

    if shutil.which("ros2") is None:
        console.print("[red]I can't find ROS 2. Make sure the robot "
                      "software is started, then try again.[/red]")
        return 1

    paths.bags_dir().mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(paths.spool_dir()).free
    if free < MIN_FREE_BYTES:
        proceed = confirm(
            "Disk space is very low — the recording may stop early. "
            "Record anyway?", default=False, console=console,
            assume_yes=yes)
        if not proceed:
            return 1

    from ros_fairy.manifest import builder
    if builder.load_spool()[1] is None:
        proceed = confirm(
            "No mission briefing yet — recording will still work, and "
            "you'll be asked the briefing questions when you close the "
            "mission. Continue?", default=True, console=console,
            assume_yes=yes)
        if not proceed:
            return 0

    from ros_fairy.subcommands.mission_start import _watchdog_alive
    if not _watchdog_alive():
        console.print("[yellow]The background recording assistant isn't "
                      "running, so some context about this recording may "
                      "not be captured.[/yellow]")

    if clock.is_synchronized() is False:
        proceed = confirm(
            f"{clock.WARNING}\nRecord anyway?", default=False, console=console,
            assume_yes=yes)
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

    child = _start(command)
    try:
        returncode = _wait(child)
    except KeyboardInterrupt:
        # The recorder runs in its own process group, so the terminal's
        # Ctrl-C reached only us: forward exactly one SIGINT (a second one
        # can cut rosbag2's metadata.yaml short).
        child.send_signal(signal.SIGINT)
        _wait_for_close(child, console)
        console.print("\nRecording stopped." + _closed_note(output)
                      + " When the mission is over, run: "
                      "[bold]ros2 fairy mission_close[/bold]")
        return 0
    if returncode != 0:
        console.print("[red]Recording stopped with a problem. The data "
                      "captured so far is kept.[/red]" + _closed_note(output))
        return 1
    console.print("Recording finished. When the mission is over, run: "
                  "[bold]ros2 fairy mission_close[/bold]")
    return 0


def _start(command: list[str]) -> subprocess.Popen:
    """The recorder, in a process group of its own."""
    if sys.version_info >= (3, 11):
        return subprocess.Popen(command, process_group=0)
    return subprocess.Popen(command, preexec_fn=os.setpgrp)  # noqa: PLW1509


def _wait(child: subprocess.Popen) -> int:
    """Wait for the recorder; a closed terminal (SIGHUP) or a SIGTERM is
    handled like Ctrl-C, so the recorder is stopped cleanly rather than left
    running on its own."""
    def stop(signum, frame):
        raise KeyboardInterrupt
    old = {sig: signal.signal(sig, stop)
           for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        return child.wait()
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


def _wait_for_close(child: subprocess.Popen, console: Console) -> None:
    """Let rosbag2 finish closing the bag. More Ctrl-C presses explain the
    wait; only the last one of FORCE_AFTER_PRESSES forces a stop."""
    presses = 0
    while True:
        try:
            child.wait()
            return
        except KeyboardInterrupt:
            presses += 1
            if presses < FORCE_AFTER_PRESSES:
                console.print("\n[yellow]Still saving the recording — please "
                              f"wait. Press Ctrl-C {FORCE_AFTER_PRESSES - presses}"
                              " more time(s) to force it to stop (the end of "
                              "the recording may then be incomplete).[/yellow]")
            else:
                console.print("\n[yellow]Forcing the recorder to stop."
                              "[/yellow]")
                child.terminate()


def _closed_note(output: str) -> str:
    if (Path(output) / "metadata.yaml").is_file() or not Path(output).is_dir():
        return ""
    return (" It was cut off before it could be closed properly; what was "
            "recorded is kept and will be read from the recording itself when "
            "you save.")


class MissionRecordVerb(VerbExtension):
    """Record mission data (wraps ros2 bag record with safety checks)."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument(
            "--yes", "-y", action="store_true",
            help="record without asking about low disk space, a missing "
                 "briefing or an unsynchronised clock")
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        return guarded_main(run, args)
