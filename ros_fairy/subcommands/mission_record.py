"""ros2 fairy mission_record — safe wrapper around ros2 bag record.

It also takes ``ros2 bag record``'s own options (``--max-bag-size``,
compression, QoS overrides, topics, ...): they are added to this verb's
parser from the installed rosbag2, so ``-h`` lists them and argparse rejects
what this rosbag2 doesn't have. They are passed on as typed.
``-o``/``--output`` is refused (recordings go to the spool, where the watchdog
finds them), and a topic selection or ``--storage`` given here replaces the
one from robot_identity.yaml.
"""

import argparse
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
    refuse_without_access,
)
from ros_fairy.utils import clock, paths, ros_env

MIN_FREE_BYTES = 1 << 30  # 1 GiB
# Ctrl-C presses after the first before the recorder is forced to stop.
FORCE_AFTER_PRESSES = 2


# rosbag2's destinations (ros2bag.verb.record) that choose what is recorded:
# given to mission_record, they replace robot_identity.yaml's selection.
SELECTION_DESTS = {"topics_positional", "topics", "services", "topic_types",
                   "all", "all_topics", "all_services", "regex"}


class RecordOptionsError(Exception):
    """Options for ros2 bag record that can't be used, in plain words."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # instead of printing usage and exiting
        raise RecordOptionsError(message)


def _record_parser() -> argparse.ArgumentParser | None:
    """``ros2 bag record``'s own parser, or None if ros2bag isn't
    importable (then only -o/--output is checked)."""
    try:
        from ros2bag.verb.record import RecordVerb
    except ImportError:
        return None
    parser = _Parser(prog="ros2 bag record", add_help=False)
    RecordVerb().add_arguments(parser, "ros2 bag record")
    return parser


def storage_names() -> list[str] | None:
    """The storage plugins the installed rosbag2 can record with (mcap,
    sqlite3), or None when it can't be asked. Not from ros2 bag record's
    parser: building it reads this process's own command line for a
    --storage, and stops the program on one it doesn't know."""
    try:
        from rosbag2_py import get_registered_writers
        return sorted(get_registered_writers()) or None
    except Exception:  # no rosbag2 here, or a plugin that fails to load
        return None


def storage_problem() -> str | None:
    """Why robot_identity.yaml's recording storage can't be used, or None
    (a name rosbag2 doesn't have stops every recording)."""
    try:
        storage = robot_identity.harvest()["recording"].get("storage")
    except robot_identity.RobotIdentityError:
        return None
    names = storage_names()
    if not storage or names is None or storage in names:
        return None
    return (f"the robot's configuration records with storage '{storage}', "
            f"which rosbag2 here doesn't have ({', '.join(names)})")


def given_options(extra: list[str]) -> set[str]:
    """Which of ros2 bag record's settings ``extra`` sets (argparse
    destinations). Raises RecordOptionsError for options it rejects, and
    for -o/--output."""
    if not extra:
        return set()
    parser = _record_parser()
    if parser is None:
        given = {"output"} if any(a in ("-o", "--output") or
                                  a.startswith("--output=") for a in extra) \
            else set()
    else:
        defaults = vars(parser.parse_args([]))
        parsed = vars(parser.parse_args(extra))
        given = {k for k, v in parsed.items() if v != defaults.get(k)}
    if "output" in given:
        raise RecordOptionsError(
            "-o/--output can't be used: mission_record records into the "
            "spool, where the recording assistant finds it. Leave it out.")
    return given


# mission_record's own options, which are not passed on.
OWN_OPTIONS = {"--yes", "-y", "--debug"}


def add_record_options(parser: argparse.ArgumentParser) -> None:
    """Add ros2 bag record's options, from the installed rosbag2, to
    mission_record's parser, in their own group of the help."""
    try:
        from ros2bag.verb.record import RecordVerb
    except ImportError:
        return  # no rosbag2 here: mission_record takes no recorder options
    group = parser.add_argument_group(
        "options passed on to ros2 bag record",
        "Any of these is passed on as typed, except -o/--output (recordings "
        "go to the spool). Topics or a topic selection given here replace "
        "robot_identity.yaml's `recording:` topics, and --storage its "
        "storage.")
    RecordVerb().add_arguments(group, "ros2 bag record")


def record_tokens(argv: list[str]) -> list[str]:
    """What was typed after ``mission_record``, without its own options:
    the options for ros2 bag record, exactly as typed (a parsed value, such
    as an opened --qos-profile-overrides-path, can't be passed on)."""
    try:
        start = len(argv) - argv[::-1].index("mission_record")
    except ValueError:
        return []
    return [t for t in argv[start:] if t not in OWN_OPTIONS and t != "--"]


def build_record_command(output_dir: str,
                         extra: list[str] | None = None) -> list[str]:
    """The exact subprocess invocation. ``extra`` (options for ros2 bag
    record, checked with :func:`given_options`) goes last; a topic selection
    or storage in it replaces robot_identity.yaml's."""
    extra = list(extra or [])
    given = given_options(extra)
    topics: list[str] | None = None
    storage: str | None = None
    try:
        recording = robot_identity.harvest()["recording"]
        topics = recording.get("topics")
        storage = recording.get("storage")
    except robot_identity.RobotIdentityError:
        pass
    cmd = ["ros2", "bag", "record"]
    if not given & SELECTION_DESTS:
        cmd += topics if topics else ["--all"]
    if storage and "storage" not in given:
        cmd += ["--storage", storage]
    cmd += ["--output", output_dir]
    return cmd + extra


def _bag_prefix() -> str:
    from ros_fairy.manifest import builder
    context = builder.load_spool()[1]
    mission_id = (context or {}).get("identity", {}).get("mission_id")
    return mission_id or "unbriefed"


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()
    yes = getattr(args, "yes", False)
    refused = refuse_without_access(console)
    if refused is not None:
        return refused

    if shutil.which("ros2") is None:
        console.print("[red]I can't find ROS 2. Make sure the robot "
                      "software is started, then try again.[/red]")
        return 1

    extra = list(getattr(args, "record_args", None) or [])
    try:
        given = given_options(extra)
    except RecordOptionsError as exc:
        console.print(f"[red]Can't pass that to ros2 bag record: {exc}[/red]")
        return 1
    problem = None if "storage" in given else storage_problem()
    if problem:
        console.print(f"[red]Can't record: {problem}. Ask your engineer to "
                      "fix it with `ros2 fairy setup --storage NAME`, or "
                      "choose one for this recording with --storage.[/red]")
        return 1
    if given & SELECTION_DESTS:
        console.print("[dim]Recording the topics chosen here, instead of "
                      "robot_identity.yaml's selection.[/dim]")

    paths.bags_dir().mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(paths.spool_dir()).free
    if free < MIN_FREE_BYTES:
        proceed = confirm(
            "Disk space is very low — the recording may stop early. "
            "Record anyway?", default=False, console=console,
            assume_yes=yes)
        if not proceed:
            return 0

    from ros_fairy.manifest import builder
    if builder.load_spool()[1] is None:
        proceed = confirm(
            "No mission briefing yet — recording will still work, and "
            "you'll be asked the briefing questions when you close the "
            "mission. Continue?", default=True, console=console,
            assume_yes=yes)
        if not proceed:
            return 0

    # A second recorder is sometimes meant (other topics), more often a
    # forgotten one in another terminal; both recordings join the mission.
    from ros_fairy.subcommands.mission_close import _recording_in_progress
    if _recording_in_progress():
        from ros_fairy.watchdog import watchdog as wd
        active = Path((wd.read_state() or {}).get("active_bag_dir") or "")
        proceed = confirm(
            f"A recording is already running ({active.name or 'unknown'}). "
            "Start another one alongside it? Both will belong to this "
            "mission.", default=False, console=console, assume_yes=yes)
        if not proceed:
            console.print("Nothing was started.")
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
    command = build_record_command(output, extra)
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
    """The recorder, in a process group of its own, with no terminal on
    stdin.

    Its own group keeps the terminal's Ctrl-C away from it (we forward one).
    That also makes it a background job of the terminal, and from Jazzy on
    the recorder's keyboard controls switch the terminal to raw mode at
    start: a background job doing that is stopped by the kernel (SIGTTOU),
    so it would record nothing and never exit. With stdin not a terminal
    the keyboard controls stay off, on every distro.
    """
    if sys.version_info >= (3, 11):
        return subprocess.Popen(command, process_group=0,
                                stdin=subprocess.DEVNULL)
    return subprocess.Popen(command, preexec_fn=os.setpgrp,  # noqa: PLW1509
                            stdin=subprocess.DEVNULL)


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
                 "briefing, an unsynchronised clock or a recording already "
                 "running")
        add_record_options(parser)
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        args.record_args = record_tokens(sys.argv)
        return guarded_main(run, args)
