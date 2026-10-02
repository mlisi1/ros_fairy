import importlib.util
import io
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from rich.console import Console

from ros_fairy.manifest import builder
from ros_fairy.subcommands import (
    doctor,
    export,
    list_missions,
    mission_abort,
    mission_close,
    mission_delete,
    mission_diff,
    mission_record,
    mission_start,
    mission_status,
    repair,
)
from ros_fairy.subcommands import setup as setup_cmd
from ros_fairy.utils import clock, fsio, paths
from tests.unit.test_archive import _spool


def _console():
    return Console(file=io.StringIO(), width=120, force_terminal=False)


ARGS = SimpleNamespace()


# --- mission_start -----------------------------------------------------------

def test_mission_start_writes_context(fairy_dirs):
    answers = {"operator_name": "Jane", "goal": "Map the creek",
               "location_name": "Marsh Creek", "environment": None,
               "notes": None}
    console = _console()
    with mock.patch.object(mission_start.briefing, "ask_briefing",
                           return_value=answers):
        assert mission_start.run(ARGS, console=console) == 0
    context = json.loads(paths.mission_context_path().read_text())
    assert context["identity"]["operator_name"] == "Jane"
    assert context["identity"]["mission_id"].startswith("m-")
    assert "mission_record" in console.file.getvalue()


def test_mission_start_snapshots_session_ros_env(fairy_dirs):
    """The recording shell's ROS env is handed to the watchdog (issue #29)."""
    from ros_fairy.utils import ros_env
    answers = {"operator_name": "Jane", "goal": "g", "location_name": "L",
               "environment": None, "notes": None}
    with mock.patch.object(mission_start.briefing, "ask_briefing",
                           return_value=answers), \
            mock.patch.dict(os.environ,
                            {"ROS_DISTRO": "jazzy", "ROS_DOMAIN_ID": "9"}):
        assert mission_start.run(ARGS, console=_console()) == 0
    env = ros_env.read_file(paths.session_env_path())
    assert env.get("ROS_DISTRO") == "jazzy" and env.get("ROS_DOMAIN_ID") == "9"


def test_mission_record_snapshots_session_ros_env(fairy_dirs):
    _spool(fairy_dirs)
    from ros_fairy.utils import ros_env
    with mock.patch.object(mission_record.shutil, "which",
                           return_value="/usr/bin/ros2"), \
            mock.patch.object(mission_record.clock, "is_synchronized",
                              return_value=True), \
            mock.patch.dict(os.environ, {"ROS_DISTRO": "jazzy",
                                         "ROS_DOMAIN_ID": "9"}), \
            mock.patch.object(mission_record.subprocess, "Popen") as popen:
        popen.return_value.wait.return_value = 0
        assert mission_record.run(ARGS, console=_console()) == 0
    env = ros_env.read_file(paths.session_env_path())
    assert env.get("ROS_DOMAIN_ID") == "9"


def test_mission_start_keeps_existing_when_declined(fairy_dirs):
    existing = builder.new_mission_context("Sam", "Old goal", "Old place")
    fsio.atomic_write_json(paths.mission_context_path(), existing)
    with mock.patch.object(mission_start.Confirm, "ask",
                           return_value=False):
        assert mission_start.run(ARGS, console=_console()) == 0
    context = json.loads(paths.mission_context_path().read_text())
    assert context["identity"]["operator_name"] == "Sam"


def _old_mission_with_foreign_bag():
    """An unfinished mission whose only recording was referenced in place."""
    fsio.atomic_write_json(paths.mission_context_path(),
                           builder.new_mission_context("Sam", "Old", "Lab"))
    harvest = builder.compose_harvest(
        None, None, None, None, None,
        {m: "ok" for m in builder.HARVEST_MODULES})
    harvest["bags"] = [{"path": "/home/op/bags/old_run", "source": "detected"}]
    fsio.atomic_write_json(paths.harvest_json_path(), harvest)


def test_mission_start_replace_drops_previous_harvest(fairy_dirs):
    """Replacing must not hand the old graph snapshot and bag list to the new
    mission (seen 2026-09-30: a mission archived a bag from the day before)."""
    _old_mission_with_foreign_bag()
    answers = {"operator_name": "Jane", "goal": "New", "location_name": "L",
               "environment": None, "notes": None}
    console = _console()
    with mock.patch.object(mission_start.Confirm, "ask", return_value=True), \
            mock.patch.object(mission_start.briefing, "ask_briefing",
                              return_value=answers):
        assert mission_start.run(ARGS, console=console) == 0
    assert not paths.harvest_json_path().exists()
    assert "/home/op/bags/old_run" in console.file.getvalue()
    context = json.loads(paths.mission_context_path().read_text())
    assert context["intent"]["goal"] == "New"


def test_mission_start_declined_keeps_previous_harvest(fairy_dirs):
    _old_mission_with_foreign_bag()
    with mock.patch.object(mission_start.Confirm, "ask", return_value=False):
        assert mission_start.run(ARGS, console=_console()) == 0
    assert paths.harvest_json_path().exists()


def test_mission_start_refuses_replace_with_spool_recordings(fairy_dirs):
    """Spool bags exist nowhere else: close the old mission first."""
    _spool(fairy_dirs)
    fsio.atomic_write_json(paths.mission_context_path(),
                           builder.new_mission_context("Sam", "Old", "Lab"))
    console = _console()
    with mock.patch.object(mission_start.Confirm, "ask") as ask:
        assert mission_start.run(ARGS, console=console) == 1
    ask.assert_not_called()
    assert "mission_close" in console.file.getvalue()
    assert paths.harvest_json_path().exists()


def test_mission_start_refuses_replace_while_recording(fairy_dirs):
    _old_mission_with_foreign_bag()
    fsio.atomic_write_json(paths.watchdog_state_path(),
                           {"state": "RECORDING", "pid": os.getpid()})
    console = _console()
    with mock.patch.object(mission_start.Confirm, "ask") as ask:
        assert mission_start.run(ARGS, console=console) == 1
    ask.assert_not_called()
    assert "recording is in progress" in console.file.getvalue()


# --- mission_abort -----------------------------------------------------------

def _abort(answers):
    """Run mission_abort with scripted confirmations; returns (rc, output,
    times asked). Questions are folded into the output (the prompt itself is
    mocked away) and whitespace is normalised against console wrapping."""
    console = _console()
    with mock.patch.object(mission_abort.Confirm, "ask",
                           side_effect=answers) as ask:
        rc = mission_abort.run(ARGS, console=console)
    text = console.file.getvalue() + " ".join(c.args[0]
                                              for c in ask.call_args_list)
    return rc, " ".join(text.split()), ask.call_count


def _open_mission():
    fsio.atomic_write_json(paths.mission_context_path(),
                           builder.new_mission_context("Sam", "Survey", "Lab"))


def test_mission_abort_nothing_open(fairy_dirs):
    rc, out, asked = _abort([])
    assert rc == 0 and asked == 0
    assert "no mission in progress" in out


def test_mission_abort_briefing_only_asks_once(fairy_dirs):
    _open_mission()
    rc, out, asked = _abort([True])
    assert rc == 0 and asked == 1
    assert "'Survey'" in out and "Mission aborted" in out
    assert not paths.mission_context_path().exists()


def test_mission_abort_declined_changes_nothing(fairy_dirs):
    _open_mission()
    rc, out, asked = _abort([False])
    assert rc == 0 and asked == 1
    assert paths.mission_context_path().exists()


def test_mission_abort_with_recording_asks_again(fairy_dirs):
    _spool(fairy_dirs)
    bag = paths.bags_dir() / "rosbag2_0"
    assert bag.is_dir()

    rc, out, asked = _abort([True, False])  # second thoughts at the bag list
    assert rc == 0 and asked == 2
    assert "rosbag2_0" in out and "deleted permanently" in out
    assert bag.is_dir() and paths.harvest_json_path().exists()

    rc, out, asked = _abort([True, True])
    assert rc == 0 and asked == 2
    assert not bag.exists()
    assert not paths.harvest_json_path().exists()
    assert not paths.mission_context_path().exists()


def test_mission_abort_leaves_foreign_recordings_on_disk(fairy_dirs, tmp_path):
    _old_mission_with_foreign_bag()  # references /home/op/bags/old_run
    foreign = tmp_path / "ops" / "mistake_run"
    foreign.mkdir(parents=True)
    harvest = json.loads(paths.harvest_json_path().read_text())
    harvest["bags"] = [{"path": str(foreign), "source": "detected"}]
    fsio.atomic_write_json(paths.harvest_json_path(), harvest)

    rc, out, asked = _abort([True, True])
    assert rc == 0 and asked == 2
    assert "stays on disk" in out
    assert foreign.is_dir()
    assert not paths.harvest_json_path().exists()


def test_mission_abort_refuses_while_recording(fairy_dirs):
    _open_mission()
    fsio.atomic_write_json(paths.watchdog_state_path(),
                           {"state": "RECORDING", "pid": os.getpid()})
    rc, out, asked = _abort([])
    assert rc == 1 and asked == 0
    assert "still in progress" in out
    assert paths.mission_context_path().exists()


# --- mission_record ----------------------------------------------------------

def test_mission_record_requires_ros2(fairy_dirs):
    console = _console()
    with mock.patch.object(mission_record.shutil, "which",
                           return_value=None):
        assert mission_record.run(ARGS, console=console) == 1
    assert "can't find ROS 2" in console.file.getvalue()


def test_clock_is_synchronized_parsing():
    def result(val):
        return SimpleNamespace(returncode=0, stdout=val + "\n")
    with mock.patch.object(clock.subprocess, "run", return_value=result("yes")):
        assert clock.is_synchronized() is True
    with mock.patch.object(clock.subprocess, "run", return_value=result("no")):
        assert clock.is_synchronized() is False
    with mock.patch.object(clock.subprocess, "run",
                           side_effect=FileNotFoundError):
        assert clock.is_synchronized() is None


def test_clock_warning_points_to_repair():
    # the bad-clock warning routes the operator to the recovery path (#27)
    assert "ros2 fairy repair" in clock.WARNING


def test_mission_record_aborts_on_unsynced_clock(fairy_dirs):
    _spool(fairy_dirs)  # a mission context, so the briefing prompt is skipped
    console = _console()
    with mock.patch.object(mission_record.shutil, "which",
                           return_value="/usr/bin/ros2"), \
         mock.patch.object(mission_record.clock, "is_synchronized",
                           return_value=False), \
         mock.patch.object(mission_record.Confirm, "ask",
                           return_value=False) as ask, \
         mock.patch.object(mission_record.subprocess, "Popen") as popen:
        assert mission_record.run(ARGS, console=console) == 0
    ask.assert_called_once()          # the clock prompt
    popen.assert_not_called()         # recording never started
    # An aborted preflight must not leave a stale env handoff behind (#29 #4).
    assert not paths.session_env_path().exists()


def test_build_record_command_default(fairy_dirs):
    cmd = mission_record.build_record_command("/out")
    assert cmd == ["ros2", "bag", "record", "--all", "--output", "/out"]


def test_build_record_command_from_identity(fairy_dirs, identity_yaml):
    text = identity_yaml.read_text() + \
        "recording:\n  topics: [/fix, /depth]\n  storage: mcap\n"
    identity_yaml.write_text(text)
    cmd = mission_record.build_record_command("/out")
    assert cmd == ["ros2", "bag", "record", "/fix", "/depth",
                   "--storage", "mcap", "--output", "/out"]


# --- mission_close -----------------------------------------------------------

def test_mission_close_nothing_recorded(fairy_dirs):
    console = _console()
    assert mission_close.run(ARGS, console=console) == 1
    assert "nothing recorded" in console.file.getvalue()


def test_mission_close_blocks_while_recording(fairy_dirs):
    _spool(fairy_dirs)
    fsio.atomic_write_json(paths.watchdog_state_path(), {
        "pid": os.getpid(), "state": "RECORDING"})
    console = _console()
    assert mission_close.run(ARGS, console=console) == 1
    assert "still in progress" in console.file.getvalue()


def test_wait_for_finalising_returns_immediately_when_idle(fairy_dirs):
    with mock.patch.object(mission_close.wd, "read_state",
                           return_value={"state": "IDLE"}), \
            mock.patch.object(mission_close.time, "sleep") as sleep_mock:
        mission_close._wait_for_finalising(_console())
    sleep_mock.assert_not_called()


def test_wait_for_finalising_polls_until_state_changes(fairy_dirs):
    states = iter([{"state": "FINALISING"}, {"state": "FINALISING"},
                   {"state": "IDLE"}])
    with mock.patch.object(mission_close.wd, "read_state",
                           side_effect=lambda: next(states)), \
            mock.patch.object(mission_close.time, "sleep") as sleep_mock:
        mission_close._wait_for_finalising(_console())
    assert sleep_mock.call_count == 2


def test_mission_close_waits_for_finalising_instead_of_racing_it(fairy_dirs):
    """Regression (2026-09-10): a watchdog still FINALISING (a short
    recording whose harvest pipeline outlasted it) is not "RECORDING", so
    the old code raced ahead and wrongly reported nothing was recorded."""
    _spool(fairy_dirs)
    fsio.atomic_write_json(paths.watchdog_state_path(), {
        "pid": os.getpid(), "state": "FINALISING"})
    console = _console()
    with mock.patch.object(mission_close, "_wait_for_finalising") as wait_mock, \
            mock.patch.object(mission_close.review, "confirm_save",
                              return_value="discard"):
        assert mission_close.run(ARGS, console=console) == 0
    wait_mock.assert_called_once()
    out = console.file.getvalue()
    assert "still in progress" not in out
    assert "nothing recorded" not in out


def test_mission_close_save_flow(fairy_dirs):
    _spool(fairy_dirs)
    console = _console()
    with mock.patch.object(mission_close.review, "confirm_save",
                           return_value="save"):
        assert mission_close.run(ARGS, console=console) == 0
    out = console.file.getvalue()
    assert "Mission saved" in out
    archives = [p for p in paths.archive_dir().iterdir()
                if p.is_dir() and p.name != ".staging"]
    assert len(archives) == 1
    assert (archives[0] / "ro-crate-metadata.json").is_file()
    assert not paths.mission_context_path().exists()


def test_mission_close_discard_flow(fairy_dirs):
    _spool(fairy_dirs)
    console = _console()
    with mock.patch.object(mission_close.review, "confirm_save",
                           return_value="discard"):
        assert mission_close.run(ARGS, console=console) == 0
    assert "discarded" in console.file.getvalue()
    assert not any(paths.bags_dir().iterdir())
    assert not paths.harvest_json_path().exists()


def test_mission_close_discard_clears_session_env(fairy_dirs):
    _spool(fairy_dirs)
    from ros_fairy.utils import ros_env
    ros_env.write_file(paths.session_env_path(), {"ROS_DOMAIN_ID": "7"})
    with mock.patch.object(mission_close.review, "confirm_save",
                           return_value="discard"):
        assert mission_close.run(ARGS, console=_console()) == 0
    # A stale session.env would otherwise be adopted by the next, unrelated
    # harvest, re-introducing the drift this guards against (#29).
    assert not paths.session_env_path().exists()


def test_mission_close_keep_flow(fairy_dirs):
    _spool(fairy_dirs)
    console = _console()
    with mock.patch.object(mission_close.review, "confirm_save",
                           return_value="keep"):
        assert mission_close.run(ARGS, console=console) == 0
    assert "still in the spool" in console.file.getvalue()
    assert any(paths.bags_dir().iterdir())


def test_mission_close_gap_fill_briefing(fairy_dirs):
    _spool(fairy_dirs)
    paths.mission_context_path().unlink()
    answers = {"operator_name": "Sam", "goal": "Salvage run",
               "location_name": "Pier 4"}
    console = _console()
    with mock.patch.object(mission_close.briefing, "ask_missing",
                           return_value=answers), \
         mock.patch.object(mission_close.review, "confirm_save",
                           return_value="save"):
        assert mission_close.run(ARGS, console=console) == 0
    archives = [p for p in paths.archive_dir().iterdir()
                if p.is_dir() and p.name != ".staging"]
    record = json.loads(
        (archives[0] / "mission_record.json").read_text())
    assert record["identity"]["operator_name"] == "Sam"
    assert record["intent"]["location_name"] == "Pier 4"


def test_mission_close_salvages_unfinalised_bag(fairy_dirs):
    # bags exist but the watchdog never wrote harvest.json
    _spool(fairy_dirs)
    paths.harvest_json_path().unlink()
    console = _console()
    with mock.patch.object(mission_close.review, "confirm_save",
                           return_value="save"):
        assert mission_close.run(ARGS, console=console) == 0
    archives = [p for p in paths.archive_dir().iterdir()
                if p.is_dir() and p.name != ".staging"]
    record = json.loads((archives[0] / "mission_record.json").read_text())
    assert len(record["bags"]) == 1
    assert record["bags"][0]["message_count"] > 0
    # honest about the missing context
    assert "hasn't been set up" in console.file.getvalue()


# --- mission_status / list ----------------------------------------------------

def test_mission_status_json(fairy_dirs, capsys):
    args = SimpleNamespace(json=True)
    assert mission_status.run(args, console=_console()) == 0
    data = json.loads(capsys.readouterr().out)
    assert "assistant" in data
    assert data["watchdog_state"] is None


def test_list_no_index(fairy_dirs):
    console = _console()
    assert list_missions.run(SimpleNamespace(), console=console) == 0
    assert "No missions have been saved" in console.file.getvalue()


def test_list_shows_missions(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    from ros_fairy.archive import assembler
    assembler.assemble(record, harvest)

    # Wide enough that the added Mission ID column (fixed-width, no_wrap)
    # doesn't force the free-text columns to wrap across lines — a real
    # terminal narrow enough to do that would wrap them regardless of this
    # column, same as it always could with a long goal/location/operator.
    console = Console(file=io.StringIO(), width=160, force_terminal=False)
    args = SimpleNamespace(operator=None, location=None, since=None,
                           until=None, limit=20, path=False)
    assert list_missions.run(args, console=console) == 0
    out = console.file.getvalue()
    assert "Jane Doe" in out
    assert "Survey eelgrass beds" in out
    assert "10 minutes" in out
    assert record.identity.mission_id in out

    console = _console()
    args.operator = "nobody"
    assert list_missions.run(args, console=console) == 0
    assert "No missions found" in console.file.getvalue()


def test_list_flags_incomplete_saves(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    from ros_fairy.archive import assembler
    assembler.assemble(builder.build(harvest, context), harvest)
    (paths.archive_dir() / "2026-09-29_17-04-31_cut-off").mkdir()
    console = Console(file=io.StringIO(), width=160, force_terminal=False)
    args = SimpleNamespace(operator=None, location=None, since=None,
                           until=None, limit=20, path=False)
    assert list_missions.run(args, console=console) == 0
    assert "1 incomplete mission save not shown" in console.file.getvalue()


def test_list_divides_different_days_with_a_rule(fairy_dirs):
    import time
    # Day grouping is inherently local-time-dependent (that's the point —
    # "same day" means the operator's day); pin TZ so the test is
    # deterministic regardless of the host's own timezone.
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        _make_archive(fairy_dirs, created_at="2026-06-10T09:00:00+00:00")
        _make_archive(fairy_dirs, created_at="2026-06-10T14:00:00+00:00")
        _make_archive(fairy_dirs, created_at="2026-06-11T09:00:00+00:00")

        console = Console(file=io.StringIO(), width=160, force_terminal=False)
        args = SimpleNamespace(operator=None, location=None, since=None,
                               until=None, limit=20, path=False)
        assert list_missions.run(args, console=console) == 0
        out = console.file.getvalue()
        # oldest first: the two 06-10 rows with no rule between them (same
        # day), a rule (different day), then the 06-11 row
        assert out.count("├") == 1
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


def test_list_prints_latest_last_and_all_lifts_limit(fairy_dirs):
    from ros_fairy.archive import index
    for day in range(3):
        _make_archive(fairy_dirs, created_at=f"2026-06-1{day}T09:00:00+00:00")
    oldest, middle, newest = [r["mission_id"] for r in index.query()[0]][::-1]

    def render(**overrides):
        console = Console(file=io.StringIO(), width=160, force_terminal=False)
        args = SimpleNamespace(operator=None, location=None, since=None,
                               until=None, limit=20, path=False)
        vars(args).update(overrides)
        assert list_missions.run(args, console=console) == 0
        return console.file.getvalue()

    out = render()
    assert out.index(oldest) < out.index(middle) < out.index(newest)

    out = render(limit=2)  # keeps the latest two, still oldest first
    assert oldest not in out
    assert out.index(middle) < out.index(newest)
    assert "latest 2 of 3" in out and "--all" in out

    out = render(limit=2, all=True)
    assert all(m in out for m in (oldest, middle, newest))
    assert "Showing" not in out


def test_list_json(fairy_dirs, capsys):
    from ros_fairy.archive import assembler
    harvest, context = _spool(fairy_dirs)
    assembler.assemble(builder.build(harvest, context), harvest)

    args = SimpleNamespace(operator=None, location=None, since=None,
                           until=None, limit=20, path=False, json=True)
    assert list_missions.run(args, console=_console()) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["total"] == 1
    assert data["shown"] == 1
    assert data["missions"][0]["operator"] == "Jane Doe"
    assert data["missions"][0]["goal"] == "Survey eelgrass beds"


def test_list_json_no_index(fairy_dirs, capsys):
    args = SimpleNamespace(json=True)
    assert list_missions.run(args, console=_console()) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {"missions": [], "total": 0, "shown": 0}


def test_diff_json(fairy_dirs, capsys):
    from ros_fairy.archive import assembler
    h1, c1 = _spool(fairy_dirs)
    assembler.assemble(builder.build(h1, c1), h1)
    h2, c2 = _spool(fairy_dirs)
    c2["intent"]["goal"] = "A different goal entirely"
    assembler.assemble(builder.build(h2, c2), h2)

    args = SimpleNamespace(mission_a="2", mission_b="1", json=True)
    assert mission_diff.run(args, console=_console()) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mission_a"]["goal"] == "Survey eelgrass beds"   # older
    assert data["mission_b"]["goal"] == "A different goal entirely"  # newer
    goal_changes = data["changes"].get("mission_context", [])
    assert any(c["label"] == "Goal" for c in goal_changes)


# --- setup ---------------------------------------------------------------------

def test_run_as_normal_user_needs_no_root_until_apply(fairy_dirs):
    """The default flow: the wizard runs unprivileged; only the commit step
    (`_apply`, run via sudo) touches anything that needs root."""
    console = _console()
    config = {"robot": {"name": "X"}}
    with mock.patch.object(setup_cmd.os, "geteuid", return_value=1000), \
            mock.patch.object(setup_cmd, "_check_ros_visible",
                              return_value=True), \
            mock.patch.object(setup_cmd.shutil, "which",
                              return_value="/usr/bin/docker"), \
            mock.patch.object(setup_cmd, "_collect", return_value=config), \
            mock.patch.object(setup_cmd.ros_env, "capture",
                              return_value={"ROS_DISTRO": "jazzy"}), \
            mock.patch.object(setup_cmd, "_apply_via_sudo",
                              return_value=0) as apply_sudo, \
            mock.patch.object(setup_cmd, "_apply") as apply_direct:
        assert setup_cmd.run(ARGS, console=console) == 0
    apply_direct.assert_not_called()
    apply_sudo.assert_called_once()
    payload = apply_sudo.call_args[0][1]
    assert payload == {"config": config, "env": {"ROS_DISTRO": "jazzy"}}
    assert "password" in console.file.getvalue().lower()


def test_run_as_root_applies_directly_no_sudo_reexec(fairy_dirs):
    console = _console()
    config = {"robot": {"name": "X"}}
    with mock.patch.object(setup_cmd.os, "geteuid", return_value=0), \
            mock.patch.object(setup_cmd, "_ensure_ros_environment",
                              return_value=True), \
            mock.patch.object(setup_cmd, "_check_ros_visible",
                              return_value=True), \
            mock.patch.object(setup_cmd.shutil, "which",
                              return_value="/usr/bin/docker"), \
            mock.patch.object(setup_cmd, "_collect", return_value=config), \
            mock.patch.object(setup_cmd.ros_env, "capture",
                              return_value={"ROS_DISTRO": "jazzy"}), \
            mock.patch.object(setup_cmd, "_apply",
                              return_value=True) as apply_direct, \
            mock.patch.object(setup_cmd, "_apply_via_sudo") as apply_sudo:
        assert setup_cmd.run(ARGS, console=console) == 0
    apply_direct.assert_called_once()
    apply_sudo.assert_not_called()


def test_run_declined_review_writes_nothing(fairy_dirs):
    console = _console()
    with mock.patch.object(setup_cmd.os, "geteuid", return_value=1000), \
            mock.patch.object(setup_cmd, "_check_ros_visible",
                              return_value=True), \
            mock.patch.object(setup_cmd.shutil, "which",
                              return_value="/usr/bin/docker"), \
            mock.patch.object(setup_cmd, "_collect", return_value=None), \
            mock.patch.object(setup_cmd, "_apply_via_sudo") as apply_sudo:
        assert setup_cmd.run(ARGS, console=console) == 0
    apply_sudo.assert_not_called()


def test_run_apply_from_stdin_requires_root(fairy_dirs):
    console = _console()
    args = SimpleNamespace(apply_from_stdin=True, debug=False)
    with mock.patch.object(setup_cmd.os, "geteuid", return_value=1000):
        assert setup_cmd.run(args, console=console) == 1
    assert "internal" in console.file.getvalue().lower()


def test_run_apply_from_stdin_applies_the_staged_payload(fairy_dirs, monkeypatch):
    console = _console()
    args = SimpleNamespace(apply_from_stdin=True, debug=False)
    payload = {"config": {"robot": {"name": "X"}},
               "env": {"ROS_DISTRO": "jazzy"}}
    monkeypatch.setattr(setup_cmd.sys, "stdin", io.StringIO(json.dumps(payload)))
    with mock.patch.object(setup_cmd.os, "geteuid", return_value=0), \
            mock.patch.object(setup_cmd, "_apply",
                              return_value=True) as apply_mock:
        assert setup_cmd.run(args, console=console) == 0
    apply_mock.assert_called_once_with(console, payload)


def test_apply_via_sudo_pipes_payload_over_stdin(fairy_dirs):
    console = _console()
    payload = {"config": {"a": 1}, "env": {"b": 2}}
    with mock.patch.object(setup_cmd.shutil, "which",
                           return_value="/usr/bin/sudo"), \
            mock.patch.object(setup_cmd.subprocess, "run") as run_mock:
        run_mock.return_value = SimpleNamespace(returncode=0)
        assert setup_cmd._apply_via_sudo(console, payload) == 0
    cmd, kwargs = run_mock.call_args[0][0], run_mock.call_args[1]
    assert cmd[0] == "/usr/bin/sudo"
    assert "--apply-from-stdin" in cmd
    assert json.loads(kwargs["input"]) == payload


def test_apply_via_sudo_preserves_dir_overrides_and_pythonpath(fairy_dirs):
    """sudo's env_reset must not silently drop a relocated install's
    ROS_FAIRY_CONFIG_DIR/VAR_DIR, nor break `import ros_fairy` for a
    colcon-workspace install that relies on PYTHONPATH."""
    console = _console()
    with mock.patch.object(setup_cmd.shutil, "which",
                           return_value="/usr/bin/sudo"), \
            mock.patch.object(setup_cmd.subprocess, "run") as run_mock:
        run_mock.return_value = SimpleNamespace(returncode=0)
        setup_cmd._apply_via_sudo(console, {"config": {}, "env": {}})
    cmd, kwargs = run_mock.call_args[0][0], run_mock.call_args[1]
    preserve = next(a for a in cmd if a.startswith("--preserve-env="))
    assert "ROS_FAIRY_CONFIG_DIR" in preserve
    assert "ROS_FAIRY_VAR_DIR" in preserve
    assert "PYTHONPATH" in preserve
    # The PYTHONPATH handed to sudo's own process (for --preserve-env to
    # forward) must point at the directory containing the ros_fairy package,
    # not whatever the unprivileged shell's PYTHONPATH happened to be.
    assert kwargs["env"]["PYTHONPATH"] == \
        str(Path(setup_cmd.__file__).resolve().parents[2])


def test_apply_via_sudo_missing_sudo_binary(fairy_dirs):
    console = _console()
    with mock.patch.object(setup_cmd.shutil, "which", return_value=None):
        assert setup_cmd._apply_via_sudo(console, {}) == 1
    assert "sudo" in console.file.getvalue().lower()


def test_apply_writes_identity_dirs_and_service_with_given_env(fairy_dirs):
    console = _console()
    payload = {"config": {"robot": {"name": "X"}},
               "env": {"ROS_DISTRO": "jazzy"}}
    with mock.patch.object(setup_cmd, "write_identity") as wi, \
            mock.patch.object(setup_cmd, "create_dirs") as cd, \
            mock.patch.object(setup_cmd, "install_service",
                              return_value=True) as inst, \
            mock.patch.dict(setup_cmd.os.environ, {}, clear=True):
        assert setup_cmd._apply(console, payload) is True
    wi.assert_called_once_with(payload["config"])
    cd.assert_called_once()
    inst.assert_called_once_with(console, payload["env"])


def test_apply_reports_group_add_failure_instead_of_false_success(fairy_dirs):
    """A failed `usermod` must not be reported to the operator as success —
    they'd otherwise find out the hard way when mission_start denies them."""
    console = _console()
    payload = {"config": {"robot": {"name": "X"}}, "env": {}}
    with mock.patch.object(setup_cmd, "write_identity"), \
            mock.patch.object(setup_cmd, "create_dirs", return_value=False), \
            mock.patch.object(setup_cmd, "install_service", return_value=True), \
            mock.patch.dict(setup_cmd.os.environ, {"SUDO_USER": "jane"}):
        assert setup_cmd._apply(console, payload) is True
    out = console.file.getvalue()
    assert "Couldn't add jane" in out
    assert "Added jane" not in out


def test_add_operator_to_group_returns_none_with_no_sudo_user(fairy_dirs):
    with mock.patch.dict(setup_cmd.os.environ, {}, clear=True):
        assert setup_cmd._add_operator_to_group() is None


def test_add_operator_to_group_returns_true_when_already_member(fairy_dirs):
    fake_group = SimpleNamespace(gr_mem=["jane"])
    with mock.patch.dict(setup_cmd.os.environ, {"SUDO_USER": "jane"}), \
            mock.patch.object(setup_cmd.grp, "getgrnam",
                              return_value=fake_group):
        assert setup_cmd._add_operator_to_group() is True


def test_add_operator_to_group_returns_usermod_outcome(fairy_dirs):
    fake_group = SimpleNamespace(gr_mem=[])
    with mock.patch.dict(setup_cmd.os.environ, {"SUDO_USER": "jane"}), \
            mock.patch.object(setup_cmd.grp, "getgrnam",
                              return_value=fake_group), \
            mock.patch.object(setup_cmd.subprocess, "run",
                              return_value=SimpleNamespace(returncode=1)):
        assert setup_cmd._add_operator_to_group() is False


def test_install_service_stops_polling_on_failed_unit(fairy_dirs):
    """A unit that crashes on start must be reported immediately, not after
    burning the full 10s timeout polling a service that's already dead."""
    console = _console()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["systemctl", "is-active"]:
            return SimpleNamespace(returncode=3, stdout="failed\n")
        return SimpleNamespace(returncode=0, stdout="")

    with mock.patch.object(setup_cmd, "write_watchdog_env"), \
            mock.patch.object(setup_cmd.shutil, "copy"), \
            mock.patch.object(setup_cmd.subprocess, "run",
                              side_effect=fake_run):
        assert setup_cmd.install_service(console, {}) is False
    is_active_calls = [c for c in calls if c[:2] == ["systemctl", "is-active"]]
    assert len(is_active_calls) == 1


def test_collect_returns_none_when_review_declined(fairy_dirs):
    console = _console()
    config = {"robot": {"name": "X", "platform": "P", "serial_number": "S"},
              "owner": {"organization": "O", "contact_email": "a@b.c"}}
    with mock.patch.object(setup_cmd, "ask_robot", return_value=config), \
            mock.patch.object(setup_cmd, "ask_sensors",
                              return_value=([], [])), \
            mock.patch.object(setup_cmd, "review", return_value=False):
        assert setup_cmd._collect(console) is None
    assert "Nothing was written" in console.file.getvalue()


def test_collect_returns_config_when_review_confirmed(fairy_dirs):
    console = _console()
    config = {"robot": {"name": "X", "platform": "P", "serial_number": "S"},
              "owner": {"organization": "O", "contact_email": "a@b.c"}}
    with mock.patch.object(setup_cmd, "ask_robot", return_value=config), \
            mock.patch.object(setup_cmd, "ask_sensors",
                              return_value=([], [])), \
            mock.patch.object(setup_cmd, "review", return_value=True):
        assert setup_cmd._collect(console) == config


# --- setup: self-sourcing ROS (`ros-fairy-setup`, no pre-sourced shell) --------

def test_ensure_ros_environment_already_sourced_is_a_noop(fairy_dirs):
    with mock.patch.object(setup_cmd.shutil, "which", return_value="/bin/ros2"), \
            mock.patch.dict(setup_cmd.os.environ, {"ROS_DISTRO": "jazzy"}), \
            mock.patch.object(setup_cmd.ros_env, "source_setup_bash") as src:
        assert setup_cmd._ensure_ros_environment(_console(), None) is True
    src.assert_not_called()


def test_ensure_ros_environment_autodetects_single_install(fairy_dirs, tmp_path):
    setup_bash = tmp_path / "jazzy" / "setup.bash"
    setup_bash.parent.mkdir()
    setup_bash.write_text("")
    with mock.patch.object(setup_cmd.shutil, "which",
                           side_effect=[None, "/opt/ros/jazzy/bin/ros2"]), \
            mock.patch.dict(setup_cmd.os.environ, {}, clear=True), \
            mock.patch.object(setup_cmd.ros_env, "find_setup_bash",
                              return_value=[setup_bash]), \
            mock.patch.object(setup_cmd.ros_env, "source_setup_bash",
                              return_value={"ROS_DISTRO": "jazzy"}) as src:
        assert setup_cmd._ensure_ros_environment(_console(), None) is True
    src.assert_called_once_with(setup_bash)
    assert setup_cmd.os.environ.get("ROS_DISTRO") == "jazzy"


def test_ensure_ros_environment_explicit_path_overrides_autodetect(
        fairy_dirs, tmp_path):
    explicit = tmp_path / "custom" / "setup.bash"
    explicit.parent.mkdir()
    explicit.write_text("")
    console = _console()
    with mock.patch.object(setup_cmd.shutil, "which",
                           side_effect=[None, "/opt/ros/jazzy/bin/ros2"]), \
            mock.patch.dict(setup_cmd.os.environ, {}, clear=True), \
            mock.patch.object(setup_cmd.ros_env, "find_setup_bash") as find, \
            mock.patch.object(setup_cmd.ros_env, "source_setup_bash",
                              return_value={}) as src:
        assert setup_cmd._ensure_ros_environment(console, str(explicit)) is True
    find.assert_not_called()
    src.assert_called_once_with(explicit)


def test_ensure_ros_environment_explicit_path_missing(fairy_dirs, tmp_path):
    console = _console()
    with mock.patch.object(setup_cmd.shutil, "which", return_value=None):
        ok = setup_cmd._ensure_ros_environment(
            console, str(tmp_path / "nope.bash"))
    assert ok is False
    assert "doesn't exist" in console.file.getvalue()


def test_ensure_ros_environment_ambiguous_installs(fairy_dirs, tmp_path):
    a, b = tmp_path / "a" / "setup.bash", tmp_path / "b" / "setup.bash"
    console = _console()
    with mock.patch.object(setup_cmd.shutil, "which", return_value=None), \
            mock.patch.dict(setup_cmd.os.environ, {}, clear=True), \
            mock.patch.object(setup_cmd.ros_env, "find_setup_bash",
                              return_value=[a, b]):
        ok = setup_cmd._ensure_ros_environment(console, None)
    assert ok is False
    assert "Multiple ROS 2 installs" in console.file.getvalue()
    assert "--ros-setup" in console.file.getvalue()


def test_ensure_ros_environment_no_install_found(fairy_dirs):
    console = _console()
    with mock.patch.object(setup_cmd.shutil, "which", return_value=None), \
            mock.patch.dict(setup_cmd.os.environ, {}, clear=True), \
            mock.patch.object(setup_cmd.ros_env, "find_setup_bash",
                              return_value=[]):
        ok = setup_cmd._ensure_ros_environment(console, None)
    assert ok is False
    assert "--ros-setup" in console.file.getvalue()


def test_ensure_ros_environment_source_failure_is_reported(fairy_dirs, tmp_path):
    setup_bash = tmp_path / "jazzy" / "setup.bash"
    setup_bash.parent.mkdir()
    setup_bash.write_text("")
    console = _console()
    with mock.patch.object(setup_cmd.shutil, "which", return_value=None), \
            mock.patch.dict(setup_cmd.os.environ, {}, clear=True), \
            mock.patch.object(setup_cmd.ros_env, "find_setup_bash",
                              return_value=[setup_bash]), \
            mock.patch.object(setup_cmd.ros_env, "source_setup_bash",
                              side_effect=RuntimeError("boom")):
        ok = setup_cmd._ensure_ros_environment(console, None)
    assert ok is False
    assert "boom" in console.file.getvalue()


def test_ensure_ros_environment_sourced_but_still_no_ros2(fairy_dirs, tmp_path):
    """Sourcing succeeded but ros2 still isn't found — a non-ROS script."""
    setup_bash = tmp_path / "jazzy" / "setup.bash"
    setup_bash.parent.mkdir()
    setup_bash.write_text("")
    console = _console()
    with mock.patch.object(setup_cmd.shutil, "which", return_value=None), \
            mock.patch.dict(setup_cmd.os.environ, {}, clear=True), \
            mock.patch.object(setup_cmd.ros_env, "find_setup_bash",
                              return_value=[setup_bash]), \
            mock.patch.object(setup_cmd.ros_env, "source_setup_bash",
                              return_value={"FOO": "bar"}):
        ok = setup_cmd._ensure_ros_environment(console, None)
    assert ok is False
    assert "still can't find ros2" in console.file.getvalue()


def test_setup_ask_robot_validates_email(fairy_dirs):
    answers = iter(["Heron-02", "Clearpath Heron", "H02", "Lab",
                    "not-an-email", "fleet@example.org"])
    with mock.patch.object(setup_cmd.Prompt, "ask",
                           side_effect=lambda *a, **k: next(answers)):
        config = setup_cmd.ask_robot(_console(), {})
    assert config["owner"]["contact_email"] == "fleet@example.org"


def test_setup_ask_sensors_keeps_existing_on_rerun(fairy_dirs):
    # Re-running setup and declining the add-loop must not wipe the sensors
    # already configured (idempotency: current values are the defaults).
    current = {
        "sensors": [{"sensor_id": "gps0", "type": "gps",
                     "make_model": "u-blox ZED-F9P", "topic": "/fix",
                     "calibration": "gps0_cal"}],
        "calibrations": [{"name": "gps0_cal", "source_path": "/tmp/c.yaml"}],
    }
    # Confirm.ask: "Keep the 1 sensor(s)...?" → True, "Add a sensor?" → False
    confirms = iter([True, False])
    with mock.patch.object(setup_cmd.Confirm, "ask",
                           side_effect=lambda *a, **k: next(confirms)), \
         mock.patch.object(setup_cmd, "_live_topics", return_value=[]):
        sensors, calibrations = setup_cmd.ask_sensors(_console(), current)
    assert sensors == current["sensors"]
    assert calibrations == current["calibrations"]


def test_setup_ask_sensors_can_drop_existing(fairy_dirs):
    current = {"sensors": [{"sensor_id": "gps0", "type": "gps",
                            "make_model": "x", "topic": "/fix"}]}
    # Keep? → False, Add? → False
    confirms = iter([False, False])
    with mock.patch.object(setup_cmd.Confirm, "ask",
                           side_effect=lambda *a, **k: next(confirms)), \
         mock.patch.object(setup_cmd, "_live_topics", return_value=[]):
        sensors, calibrations = setup_cmd.ask_sensors(_console(), current)
    assert sensors == [] and calibrations == []


# --- repair ------------------------------------------------------------------

_MCAP = importlib.util.find_spec("mcap") is not None


@pytest.mark.skipif(not _MCAP, reason="mcap package not installed")
def test_repair_command_on_single_bad_bag(tmp_path):
    from tests.conftest import make_mcap_bag
    bad = make_mcap_bag(tmp_path / "rosbag2_bad",
                        {"/data": [float(i) for i in range(1, 31)]
                         + [1_750_000_000.0 + i * 0.5 for i in range(11)]})
    out = tmp_path / "out"
    args = SimpleNamespace(mission=str(bad), output=str(out), all=False,
                           duration=10.0, force=False, json=False)
    assert repair.run(args, console=_console()) == 0
    fixed = out / bad.name
    assert (fixed / "metadata.yaml").is_file() and list(fixed.glob("*.mcap"))


@pytest.mark.skipif(not _MCAP, reason="mcap package not installed")
def test_repair_command_skips_healthy_bag(tmp_path):
    from tests.conftest import make_mcap_bag
    good = make_mcap_bag(tmp_path / "rosbag2_ok",
                         {"/data": [1_750_000_000.0 + i * 0.1 for i in range(50)]})
    out = tmp_path / "out"
    console = _console()
    args = SimpleNamespace(mission=str(good), output=str(out), all=False,
                           duration=None, force=False, json=False)
    assert repair.run(args, console=console) == 0
    assert "nothing to repair" in console.file.getvalue()
    assert not out.exists()


def test_repair_command_unknown_target(fairy_dirs):
    args = SimpleNamespace(mission="nope", output=None, all=False,
                           duration=None, force=False, json=False)
    assert repair.run(args, console=_console()) == 1


# --- data quality / degradation gate -----------------------------------------

def test_quality_ok_for_healthy_mission(fairy_dirs):
    from ros_fairy.manifest import quality
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    assert quality.assess(record, harvest).level == quality.OK


def test_sensor_silent_in_one_recording_only_is_not_silent(fairy_dirs):
    """2026-10-02: an unrelated recording without sensor topics made every
    sensor of the mission count as 'produced no data at all'."""
    from ros_fairy.manifest import quality
    from ros_fairy.manifest.schema import HealthWarning
    harvest, context = _spool(fairy_dirs, n_bags=2)
    record = builder.build(harvest, context)
    silent = HealthWarning(topic="/fix", sensor_id="gps0",
                           kind="never_published", plain_text="no data")
    record.bags[0].health_warnings = [silent]
    record.bags[1].health_warnings = []
    reasons = quality.assess(record, harvest).reasons
    assert not any("produced no data" in r for r in reasons)
    record.bags[1].health_warnings = [silent]
    reasons = quality.assess(record, harvest).reasons
    assert any("1 sensor(s) produced no data" in r for r in reasons)


def test_quality_poor_without_ros_context(fairy_dirs):
    from ros_fairy.manifest import quality
    harvest, context = _spool(fairy_dirs)
    harvest["ros_graph"]["nodes"] = []
    harvest["provenance"]["harvest_status"]["ros_graph"] = "failed"
    record = builder.build(harvest, context)
    q = quality.assess(record, harvest)
    assert q.level == quality.POOR and any("software" in r for r in q.reasons)


def test_quality_poor_when_all_bags_unusable(fairy_dirs):
    from ros_fairy.manifest import quality
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    for b in record.bags:
        b.duration_s = None
    assert quality.assess(record, harvest).level == quality.POOR


def test_quality_degraded_when_sensor_not_detected(fairy_dirs):
    from ros_fairy.manifest import quality
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    for s in record.sensors:
        s.detected_at_start = False
    assert quality.assess(record, harvest).level == quality.DEGRADED


def test_mission_close_gates_poor_mission(fairy_dirs):
    # Spool harvest looks like no ROS context was captured -> poor.
    harvest, _ = _spool(fairy_dirs)
    harvest["ros_graph"]["nodes"] = []
    harvest["provenance"]["harvest_status"]["ros_graph"] = "failed"
    fsio.atomic_write_json(paths.harvest_json_path(), harvest)

    captured = {}

    def fake_confirm(console=None, *, risky=False):
        captured["risky"] = risky
        return "keep"

    with mock.patch.object(mission_close.review, "confirm_save", fake_confirm):
        assert mission_close.run(ARGS, console=_console()) == 0
    assert captured["risky"] is True


def test_mission_close_warns_on_likely_duplicate(fairy_dirs):
    from ros_fairy.archive import assembler

    # 1) Save a "Crosslab" mission.
    harvest, context = _spool(fairy_dirs)
    context["intent"]["location_name"] = "Crosslab"
    assembler.assemble(builder.build(harvest, context), harvest)

    # 2) Spool a new mission with the place mistyped "Crossloab".
    harvest2, context2 = _spool(fairy_dirs)
    context2["intent"]["location_name"] = "Crossloab"
    fsio.atomic_write_json(paths.mission_context_path(), context2)

    console = _console()
    with mock.patch.object(mission_close.review, "confirm_save",
                           return_value="keep"):
        assert mission_close.run(ARGS, console=console) == 0
    assert "Possible duplicate" in console.file.getvalue()


def test_mission_close_does_not_gate_healthy_mission(fairy_dirs):
    _spool(fairy_dirs)
    captured = {}

    def fake_confirm(console=None, *, risky=False):
        captured["risky"] = risky
        return "keep"

    with mock.patch.object(mission_close.review, "confirm_save", fake_confirm):
        assert mission_close.run(ARGS, console=_console()) == 0
    assert captured["risky"] is False


# --- export ------------------------------------------------------------------

def _make_archive(fairy_dirs, created_at: str | None = None):
    from ros_fairy.archive import assembler
    harvest, context = _spool(fairy_dirs)
    if created_at:
        context["identity"]["created_at"] = created_at
    record = builder.build(harvest, context)
    return assembler.assemble(record, harvest)


def test_export_creates_zip_and_checksum(fairy_dirs, tmp_path):
    import zipfile
    crate = _make_archive(fairy_dirs)
    out = tmp_path / "share"
    out.mkdir()  # an existing directory is treated as the output folder
    args = SimpleNamespace(mission=str(crate), output=str(out), format="zip",
                           force=False, json=False)
    assert export.run(args, console=_console()) == 0

    bundle = out / f"{crate.name}.zip"
    sidecar = out / f"{crate.name}.zip.sha256"
    assert bundle.is_file() and sidecar.is_file()
    # checksum sidecar is correct and sha256sum-compatible
    digest, name = sidecar.read_text().split()
    assert digest == fsio.sha256_file(bundle) and name == bundle.name
    # bundle has a top-level crate folder
    with zipfile.ZipFile(bundle) as zf:
        names = zf.namelist()
    assert f"{crate.name}/mission_record.json" in names


def test_export_refuses_existing_without_force(fairy_dirs, tmp_path):
    crate = _make_archive(fairy_dirs)
    dest = tmp_path / "m.zip"
    dest.write_text("old")
    base = dict(mission=str(crate), output=str(dest), format="zip", json=False)
    assert export.run(SimpleNamespace(**base, force=False),
                      console=_console()) == 1
    assert export.run(SimpleNamespace(**base, force=True),
                      console=_console()) == 0
    assert dest.read_bytes()[:2] == b"PK"  # overwritten with a real zip


def test_export_json(fairy_dirs, tmp_path, capsys):
    crate = _make_archive(fairy_dirs)
    args = SimpleNamespace(mission=str(crate), output=str(tmp_path),
                           format="zip", force=False, json=True)
    assert export.run(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["sha256"] == fsio.sha256_file(Path(data["bundle"]))
    assert data["mission_id"] and data["verify_result"] in ("ok", "warn", "fail")


def test_export_unknown_mission(fairy_dirs):
    args = SimpleNamespace(mission="does-not-exist", output=None, format="zip",
                           force=False, json=False)
    assert export.run(args, console=_console()) == 1


def _batch_args(out_dir, **overrides):
    base = dict(mission=None, all=False, today=False, exclude=None,
               output=str(out_dir), format="zip", force=False, json=True)
    base.update(overrides)
    return SimpleNamespace(**base)


def test_export_all_skips_already_exported(fairy_dirs, tmp_path, capsys):
    crate1 = _make_archive(fairy_dirs, created_at="2026-06-10T09:00:00+00:00")
    crate2 = _make_archive(fairy_dirs, created_at="2026-06-11T09:00:00+00:00")
    mission2 = json.loads((crate2 / "mission_record.json").read_text())
    share = tmp_path / "share"
    share.mkdir()  # an existing directory is treated as the output folder

    # Export mission 1 individually — it now counts as exported.
    args1 = SimpleNamespace(mission=str(crate1), output=str(share),
                            format="zip", force=False, json=False)
    assert export.run(args1, console=_console()) == 0

    with mock.patch.object(export.Confirm, "ask", return_value=True):
        assert export.run(_batch_args(share, all=True),
                          console=_console()) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["exported"] == 1
    assert data["results"][0]["mission_id"] == mission2["identity"]["mission_id"]

    # Running --all again exports nothing further.
    with mock.patch.object(export.Confirm, "ask", return_value=True):
        assert export.run(_batch_args(share, all=True),
                          console=_console()) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["exported"] == 0


def test_export_today_filters_by_date(fairy_dirs, tmp_path, capsys):
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    import time
    time.tzset()
    try:
        old_crate = _make_archive(fairy_dirs,
                                  created_at="2020-01-01T09:00:00+00:00")
        today_iso = datetime.now(timezone.utc).isoformat()
        new_crate = _make_archive(fairy_dirs, created_at=today_iso)
        new_id = json.loads(
            (new_crate / "mission_record.json").read_text())["identity"][
                "mission_id"]

        with mock.patch.object(export.Confirm, "ask", return_value=True):
            assert export.run(_batch_args(tmp_path / "share", today=True),
                              console=_console()) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["exported"] == 1
        assert data["results"][0]["mission_id"] == new_id
        assert old_crate.is_dir()  # sanity: fixture used, untouched
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


def test_export_exclude_skips_given_ids(fairy_dirs, tmp_path, capsys):
    crate1 = _make_archive(fairy_dirs, created_at="2026-06-10T09:00:00+00:00")
    crate2 = _make_archive(fairy_dirs, created_at="2026-06-11T09:00:00+00:00")
    id1 = json.loads(
        (crate1 / "mission_record.json").read_text())["identity"]["mission_id"]
    id2 = json.loads(
        (crate2 / "mission_record.json").read_text())["identity"]["mission_id"]

    with mock.patch.object(export.Confirm, "ask", return_value=True):
        assert export.run(
            _batch_args(tmp_path / "share", all=True, exclude=[id1]),
            console=_console()) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["exported"] == 1
    assert data["results"][0]["mission_id"] == id2


def test_export_batch_declines_without_confirmation(fairy_dirs, tmp_path):
    _make_archive(fairy_dirs)
    share = tmp_path / "share"
    with mock.patch.object(export.Confirm, "ask", return_value=False):
        assert export.run(_batch_args(share, all=True, json=False),
                          console=_console()) == 0
    assert not list(share.glob("*.zip"))  # nothing was written


def test_export_batch_warns_on_low_disk_space(fairy_dirs, tmp_path):
    _make_archive(fairy_dirs)
    share = tmp_path / "share"
    calls = []

    def fake_ask(prompt, *a, **kw):
        calls.append(prompt)
        return False  # decline at the (first) low-space prompt

    with mock.patch.object(export.shutil, "disk_usage",
                           return_value=SimpleNamespace(total=0, used=0, free=1)), \
         mock.patch.object(export.Confirm, "ask", side_effect=fake_ask):
        assert export.run(_batch_args(share, all=True, json=False),
                          console=_console()) == 1
    assert any("free" in c for c in calls)
    assert not list(share.glob("*.zip"))  # nothing was written


# --- doctor ------------------------------------------------------------------

def test_doctor_check_clock(monkeypatch):
    monkeypatch.setattr(doctor.clock, "is_synchronized", lambda: False)
    assert doctor._check_clock()["status"] == doctor.FAIL
    monkeypatch.setattr(doctor.clock, "is_synchronized", lambda: True)
    assert doctor._check_clock()["status"] == doctor.OK
    monkeypatch.setattr(doctor.clock, "is_synchronized", lambda: None)
    assert doctor._check_clock()["status"] == doctor.SKIP


def test_doctor_watchdog_stale_heartbeat_only_matters_while_recording():
    # The watchdog heartbeats only while RECORDING; an IDLE service whose
    # state file is hours old is healthy, not "not responding".
    import os
    from datetime import datetime, timedelta, timezone

    from ros_fairy.watchdog import watchdog as wd
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    idle = {"pid": os.getpid(), "state": "IDLE", "heartbeat_at": old}
    with mock.patch.object(wd, "read_state", return_value=idle):
        assert doctor._check_watchdog()["status"] == doctor.OK
    recording = dict(idle, state="RECORDING")
    with mock.patch.object(wd, "read_state", return_value=recording):
        c = doctor._check_watchdog()
        assert c["status"] == doctor.WARN and "not responding" in c["title"]


def test_doctor_service_harvest_distinguishes_service_context():
    from ros_fairy.watchdog import watchdog as wd
    with mock.patch.object(wd, "read_state",
                           return_value={"harvest_status": {"ros_graph": "ok"}}):
        assert doctor._check_service_harvest()["status"] == doctor.OK
    with mock.patch.object(
            wd, "read_state",
            return_value={"harvest_status": {"ros_graph": "failed"}}):
        c = doctor._check_service_harvest()
        assert c["status"] == doctor.FAIL and "ros2 fairy setup" in c["hint"]
    with mock.patch.object(wd, "read_state", return_value=None):
        assert doctor._check_service_harvest()["status"] == doctor.SKIP


def test_doctor_service_harvest_partial_is_history_not_a_warning():
    """2026-10-02: "parameter capture timed out" warned next to "no nodes are
    running" — it described the last recording, not now. A partial capture
    still proves the service reaches ROS."""
    from ros_fairy.watchdog import watchdog as wd
    captured = datetime.now(timezone.utc).isoformat()
    state = {"harvest_status": {"ros_graph": "partial"},
             "harvest_captured_at": captured, "harvest_node_count": 38}
    with mock.patch.object(wd, "read_state", return_value=state):
        c = doctor._check_service_harvest()
    assert c["status"] == doctor.OK
    assert c["detail"].startswith("last capture at ")
    assert "38 node(s)" in c["detail"] and "parameters" in c["detail"]


def test_doctor_service_env_missing_file_fails(fairy_dirs):
    # watchdog.env doesn't exist → the service started blind.
    c = doctor._check_service_env()
    assert c["status"] == doctor.FAIL and "ros2 fairy setup" in c["hint"]


def test_doctor_service_env_no_distro_fails(fairy_dirs):
    from ros_fairy.utils import ros_env
    ros_env.write_file(paths.watchdog_env_path(), {"PATH": "/usr/bin"})
    assert doctor._check_service_env()["status"] == doctor.FAIL


def test_doctor_service_env_domain_mismatch_warns(fairy_dirs):
    from ros_fairy.utils import ros_env
    ros_env.write_file(paths.watchdog_env_path(),
                       {"ROS_DISTRO": "jazzy", "ROS_DOMAIN_ID": "7"})
    with mock.patch.dict(doctor.os.environ, {"ROS_DOMAIN_ID": "42"}):
        c = doctor._check_service_env()
    assert c["status"] == doctor.WARN and "ROS_DOMAIN_ID" in c["detail"]


def test_doctor_service_env_matches_is_ok(fairy_dirs):
    from ros_fairy.utils import ros_env
    ros_env.write_file(paths.watchdog_env_path(),
                       {"ROS_DISTRO": "jazzy", "ROS_DOMAIN_ID": "7",
                        "RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp"})
    env = {"ROS_DOMAIN_ID": "7", "RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp"}
    with mock.patch.dict(doctor.os.environ, env):
        assert doctor._check_service_env()["status"] == doctor.OK


def test_doctor_check_that_raises_becomes_fail():
    def boom():
        raise RuntimeError("nope")
    with mock.patch.object(doctor, "_CHECKS", (boom,)):
        results = doctor.diagnose()
    assert results[0]["status"] == doctor.FAIL
    assert "nope" in results[0]["detail"]


@pytest.mark.parametrize("error, status, title", [
    ("no ROS nodes visible: ROS is not running", "warn", "no nodes"),
    ("rclpy is not available: No module named 'rclpy'", "fail", "not on PATH"),
    ("could not join the ROS graph: boom", "fail", "not reachable"),
])
def test_doctor_ros_reachable_classifies_snapshot_errors(error, status, title):
    from ros_fairy.harvest import ros_graph
    with mock.patch.object(ros_graph, "list_nodes",
                           side_effect=ros_graph.RosGraphError(error)):
        result = doctor._check_ros_reachable()
    assert result["status"] == status
    assert title in result["title"]


def test_doctor_archive_check(fairy_dirs):
    paths.archive_dir().mkdir(parents=True, exist_ok=True)
    (paths.archive_dir() / ".staging").mkdir()
    assert doctor._check_archive()["status"] == doctor.OK
    good = paths.archive_dir() / "good"
    good.mkdir()
    (good / "mission_record.json").write_text("{}")
    assert doctor._check_archive()["status"] == doctor.OK
    (paths.archive_dir() / "2026-09-29_17-04-31_cut-off").mkdir()
    result = doctor._check_archive()
    assert result["status"] == doctor.WARN
    assert "2026-09-29_17-04-31_cut-off" in result["detail"]
    assert "adopt" in result["hint"]


def test_doctor_run_not_ready_exit_and_json(capsys):
    fake = [{"status": doctor.OK, "title": "a", "detail": "", "hint": ""},
            {"status": doctor.FAIL, "title": "b", "detail": "d", "hint": "h"}]
    with mock.patch.object(doctor, "diagnose", return_value=fake):
        rc = doctor.run(SimpleNamespace(json=True))
    data = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert data["result"] == "fail" and len(data["checks"]) == 2


def test_doctor_run_ready():
    fake = [{"status": doctor.OK, "title": "a", "detail": "", "hint": ""}]
    console = _console()
    with mock.patch.object(doctor, "diagnose", return_value=fake):
        rc = doctor.run(ARGS, console=console)
    assert rc == 0
    assert "READY" in console.file.getvalue()


def test_setup_captures_ros_environment(fairy_dirs):
    """The watchdog runs as a service with no sourced ROS env, so setup must
    snapshot the operator's ROS environment into the unit's EnvironmentFile."""
    keep = {"ROS_FAIRY_CONFIG_DIR": os.environ["ROS_FAIRY_CONFIG_DIR"]}
    env = {**keep,
           "ROS_DISTRO": "jazzy",
           "AMENT_PREFIX_PATH": "/opt/ros/jazzy",
           "RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp",
           "ROS_DOMAIN_ID": "7",
           "PATH": "/opt/ros/jazzy/bin:/usr/bin",
           "HOME": "/root", "EDITOR": "vim"}
    with mock.patch.dict(setup_cmd.os.environ, env, clear=True):
        setup_cmd.write_watchdog_env(setup_cmd.ros_env.capture())
        text = paths.watchdog_env_path().read_text()
    assert "ROS_DISTRO=jazzy" in text
    assert "RMW_IMPLEMENTATION=rmw_cyclonedds_cpp" in text
    assert "ROS_DOMAIN_ID=7" in text
    assert "AMENT_PREFIX_PATH=/opt/ros/jazzy" in text
    assert "PATH=/opt/ros/jazzy/bin:/usr/bin" in text
    # Non-ROS variables are not snapshotted.
    assert "EDITOR" not in text
    assert "HOME=" not in text


def test_setup_fails_when_ros_environment_missing(fairy_dirs):
    """No captured ROS env must abort setup (not just warn): the service would
    otherwise start blind and harvest an empty graph forever (issue #29)."""
    keep = {"ROS_FAIRY_CONFIG_DIR": os.environ["ROS_FAIRY_CONFIG_DIR"]}
    console = _console()
    with mock.patch.dict(setup_cmd.os.environ, {**keep, "PATH": "/usr/bin"},
                         clear=True):
        assert setup_cmd._check_ros_visible(console) is False
    out = console.file.getvalue()
    assert "ROS_DISTRO is unset" in out and "ros-fairy-setup" in out


def test_setup_fails_when_graph_not_visible(fairy_dirs):
    """ROS sourced but no nodes visible (wrong domain/RMW, software down) must
    abort: the watchdog would harvest an empty graph."""
    keep = {"ROS_FAIRY_CONFIG_DIR": os.environ["ROS_FAIRY_CONFIG_DIR"]}
    console = _console()
    with mock.patch.dict(setup_cmd.os.environ,
                         {**keep, "ROS_DISTRO": "jazzy", "PATH": "/usr/bin"},
                         clear=True), \
            mock.patch.object(setup_cmd, "_ros2_list", return_value=[]):
        assert setup_cmd._check_ros_visible(console) is False
    assert "no nodes are visible" in console.file.getvalue()


def test_setup_ros_visible_passes(fairy_dirs):
    keep = {"ROS_FAIRY_CONFIG_DIR": os.environ["ROS_FAIRY_CONFIG_DIR"]}
    with mock.patch.dict(setup_cmd.os.environ,
                         {**keep, "ROS_DISTRO": "jazzy", "PATH": "/usr/bin"},
                         clear=True), \
            mock.patch.object(setup_cmd, "_ros2_list",
                              return_value=["/robot_node"]):
        assert setup_cmd._check_ros_visible(_console()) is True


def test_setup_written_identity_is_harvestable(fairy_dirs):
    config = {
        "robot": {"name": "Heron-02", "platform": "Heron",
                  "serial_number": "H02"},
        "owner": {"organization": "Lab", "contact_email": "a@b.c"},
        "sensors": [{"sensor_id": "gps0", "type": "gps",
                     "make_model": "F9P", "topic": "/fix"}],
    }
    setup_cmd.write_identity(config)
    from ros_fairy.harvest import robot_identity
    data = robot_identity.harvest()
    assert data["robot"]["name"] == "Heron-02"
    assert data["sensors"][0]["sensor_id"] == "gps0"


# --- verb boundary guard (no tracebacks, Ctrl-C -> 130) ----------------------

def test_guarded_main_turns_ctrl_c_into_130(capsys):
    from ros_fairy.subcommands import guarded_main

    def run(args):
        raise KeyboardInterrupt

    assert guarded_main(run, SimpleNamespace(debug=False)) == 130
    err = capsys.readouterr().err
    assert "Cancelled" in err
    assert "Traceback" not in err


def test_guarded_main_hides_unexpected_errors(capsys):
    from ros_fairy.subcommands import guarded_main

    def run(args):
        raise RuntimeError("internal detail the operator must not see")

    assert guarded_main(run, SimpleNamespace(debug=False)) == 1
    err = capsys.readouterr().err
    assert "--debug" in err
    assert "Traceback" not in err
    assert "internal detail" not in err


def test_guarded_main_reraises_for_engineers():
    from ros_fairy.subcommands import guarded_main

    def run(args):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        guarded_main(run, SimpleNamespace(debug=True))


def test_guarded_main_passes_through_exit_codes():
    from ros_fairy.subcommands import guarded_main
    assert guarded_main(lambda args: 0, SimpleNamespace()) == 0
    assert guarded_main(lambda args: 3, SimpleNamespace()) == 3


def test_all_verb_wrappers_are_guarded():
    """Every VerbExtension.main must route through guarded_main."""
    import inspect

    from ros_fairy import subcommands as pkg
    from ros_fairy.subcommands import adopt, reindex, verify
    modules = [adopt, doctor, export, list_missions, mission_abort,
               mission_close, mission_delete,
               mission_diff, mission_record, mission_start, mission_status,
               reindex, repair, setup_cmd, verify]
    for module in modules:
        wrappers = [obj for _, obj in inspect.getmembers(module, inspect.isclass)
                    if issubclass(obj, pkg.VerbExtension)
                    and obj is not pkg.VerbExtension
                    and obj.__module__ == module.__name__]
        assert wrappers, f"{module.__name__} has no VerbExtension"
        for wrapper in wrappers:
            source = inspect.getsource(wrapper.main)
            assert "guarded_main" in source, \
                f"{module.__name__}.{wrapper.__name__}.main is unguarded"


# --- reindex ------------------------------------------------------------------

def test_reindex_verb_rebuilds_index(fairy_dirs, capsys):
    from ros_fairy.archive import assembler, index
    from ros_fairy.subcommands import reindex

    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    final = assembler.assemble(record, harvest)
    paths.index_db_path().unlink()

    args = SimpleNamespace(json=False, debug=False)
    assert reindex.run(args, console=_console()) == 0
    rows, total = index.query()
    assert total == 1
    assert rows[0]["archive_path"] == str(final)


def test_reindex_verb_json_and_empty_archive(fairy_dirs, capsys):
    from ros_fairy.subcommands import reindex

    args = SimpleNamespace(json=True, debug=False)
    assert reindex.run(args, console=_console()) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["missions"] == 0


# --- mission_delete -------------------------------------------------------------

def _delete(args, confirm=(True,), phrase=mission_delete.PHRASE, tty=True,
            sudo_ok=True):
    """Run mission_delete with scripted answers; sudo is simulated (sudo -v
    succeeds or not, `sudo rm -rf` really removes). Returns (rc, output,
    subprocess calls)."""
    import shutil as _shutil
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["sudo", "-v"]:
            return subprocess.CompletedProcess(cmd, 0 if sudo_ok else 1)
        if cmd[:3] == ["sudo", "rm", "-rf"]:
            _shutil.rmtree(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0)
        raise AssertionError(f"unexpected command {cmd}")

    console = _console()
    with mock.patch.object(mission_delete.sys.stdin, "isatty",
                           return_value=tty), \
            mock.patch.object(mission_delete.Confirm, "ask",
                              side_effect=list(confirm)), \
            mock.patch.object(mission_delete.Prompt, "ask",
                              return_value=phrase) as prompt, \
            mock.patch.object(mission_delete.subprocess, "run",
                              side_effect=fake_run), \
            mock.patch.object(mission_delete.os, "geteuid",
                              return_value=1000):
        rc = mission_delete.run(SimpleNamespace(**{
            "mission_id": None, "all": False, "today": False, **args}),
            console=console)
    out = " ".join(console.file.getvalue().split())
    return rc, out, calls, prompt.call_count


def _saved(fairy_dirs, *created):
    from ros_fairy.archive import index
    for c in created:
        _make_archive(fairy_dirs, created_at=c)
    rows, _ = index.query(limit=None)
    return {r["mission_id"]: Path(r["archive_path"]) for r in rows}


def test_mission_delete_one_by_id(fairy_dirs):
    from ros_fairy.archive import index
    saved = _saved(fairy_dirs, "2026-06-10T09:00:00+00:00",
                   "2026-06-11T09:00:00+00:00")
    victim, keep = sorted(saved)
    rc, out, calls, phrased = _delete({"mission_id": victim})
    assert rc == 0 and phrased == 1
    assert "There is no undo" in out and "recordings made outside" in out
    assert not saved[victim].exists() and saved[keep].exists()
    assert [r["mission_id"] for r in index.query()[0]] == [keep]
    assert calls == []  # a single delete needs no sudo


def test_mission_delete_wrong_phrase_or_no_deletes_nothing(fairy_dirs):
    saved = _saved(fairy_dirs, "2026-06-10T09:00:00+00:00")
    (mid, path), = saved.items()
    rc, out, _, _ = _delete({"mission_id": mid},
                            phrase="I understand flames and fire")
    assert rc == 1 and "nothing was deleted" in out and path.exists()
    rc, out, _, phrased = _delete({"mission_id": mid}, confirm=(False,))
    assert rc == 0 and phrased == 0 and path.exists()


def test_mission_delete_takes_exact_ids_only(fairy_dirs):
    saved = _saved(fairy_dirs, "2026-06-10T09:00:00+00:00")
    for bad in ("1", str(next(iter(saved.values()))), "m-nope"):
        rc, out, _, phrased = _delete({"mission_id": bad}, confirm=())
        assert rc == 1 and phrased == 0
    assert next(iter(saved.values())).exists()


def test_mission_delete_all_asks_for_sudo_first(fairy_dirs):
    from ros_fairy.archive import index
    saved = _saved(fairy_dirs, "2026-06-10T09:00:00+00:00",
                   "2026-06-11T09:00:00+00:00")
    rc, out, calls, _ = _delete({"all": True})
    assert rc == 0 and "needs administrator (sudo)" in out
    assert calls[0] == ["sudo", "-v"]
    assert [c[:3] for c in calls[1:]] == [["sudo", "rm", "-rf"]] * 2
    assert not any(p.exists() for p in saved.values())
    assert index.query()[1] == 0


def test_mission_delete_all_without_sudo_deletes_nothing(fairy_dirs):
    saved = _saved(fairy_dirs, "2026-06-10T09:00:00+00:00")
    rc, out, calls, _ = _delete({"all": True}, sudo_ok=False)
    assert rc == 1 and calls == [["sudo", "-v"]]
    assert all(p.exists() for p in saved.values())


def test_mission_delete_today_only_today(fairy_dirs):
    import time
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        today = datetime.now(timezone.utc).replace(hour=0, minute=30)
        saved = _saved(fairy_dirs, "2026-06-10T09:00:00+00:00",
                       today.isoformat())
        rc, _, calls, _ = _delete({"today": True})
        assert rc == 0 and calls[0] == ["sudo", "-v"]
        from ros_fairy.archive import index
        remaining = [r["created_at"] for r in index.query()[0]]
        assert remaining == ["2026-06-10T09:00:00+00:00"]
        assert sum(p.exists() for p in saved.values()) == 1
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


def test_mission_delete_refuses_without_a_terminal(fairy_dirs):
    saved = _saved(fairy_dirs, "2026-06-10T09:00:00+00:00")
    rc, out, _, _ = _delete({"all": True}, confirm=(), tty=False)
    assert rc == 1 and "interactive terminal" in out
    assert all(p.exists() for p in saved.values())


def test_mission_delete_never_leaves_the_archive(fairy_dirs, tmp_path):
    """An index row pointing outside the archive (corrupt or hand-edited)
    must not turn into an rm -rf of that path."""
    import sqlite3
    saved = _saved(fairy_dirs, "2026-06-10T09:00:00+00:00")
    (mid, _), = saved.items()
    outside = tmp_path / "precious"
    outside.mkdir()
    con = sqlite3.connect(paths.index_db_path())
    con.execute("UPDATE missions SET archive_path = ?", (str(outside),))
    con.commit()
    con.close()
    rc, out, calls, _ = _delete({"all": True})
    assert rc == 1 and "not inside the archive" in out
    assert outside.is_dir()
    assert not any(c[:2] == ["sudo", "rm"] for c in calls)
