"""Operator verbs and UI: FAILURE_CASES S1-S20 (S1, S4, S14 and S20 live
next to the existing tests in test_subcommands.py / test_verify.py)."""

import io
import json
import os
import signal
import sys
from types import SimpleNamespace
from unittest import mock

import pytest
from rich.console import Console

from ros_fairy.archive import assembler, index
from ros_fairy.manifest import builder
from ros_fairy.subcommands import (
    adopt,
    doctor,
    export,
    guarded_main,
    list_missions,
    mission_abort,
    mission_close,
    mission_delete,
    mission_diff,
    mission_record,
    mission_start,
    verify,
)
from ros_fairy.subcommands import setup as setup_cmd
from ros_fairy.ui import diff, status
from ros_fairy.utils import fsio, paths
from tests.conftest import make_bag
from tests.unit.test_archive import T0, _spool

ARGS = SimpleNamespace()


def _console():
    return Console(file=io.StringIO(), width=200, force_terminal=False)


ANSWERS = {"operator_name": "Jane", "goal": "Map", "location_name": "Lab",
           "environment": None, "notes": None}


# -- S2 / S7: mission_start and what the spool already holds ------------------

def _orphan_harvest(tmp_path):
    """A harvest left by `record_all test` with no mission open."""
    foreign = make_bag(tmp_path / "test_run", {"/fix": [T0, T0 + 1]})
    from tests.unit.test_foreign_bags import _bag_entry, good_pipeline
    doc = good_pipeline()
    doc["bags"] = [_bag_entry(foreign, "detected")]
    fsio.atomic_write_json(paths.harvest_json_path(), doc)
    return foreign


def test_recordings_without_a_mission_are_not_inherited(fairy_dirs, tmp_path):
    foreign = _orphan_harvest(tmp_path)
    console = _console()
    with mock.patch.object(mission_start.Confirm, "ask",
                           return_value=False) as ask, \
            mock.patch.object(mission_start.briefing, "ask_briefing",
                              return_value=ANSWERS):
        assert mission_start.run(ARGS, console=console) == 0
    assert "Include it in this new mission?" in ask.call_args.args[0]
    assert not paths.harvest_json_path().exists()  # not inherited
    assert foreign.is_dir()                          # still on disk
    assert str(foreign) in console.file.getvalue()


def test_recordings_without_a_mission_can_be_included(fairy_dirs, tmp_path):
    _orphan_harvest(tmp_path)
    with mock.patch.object(mission_start.Confirm, "ask", return_value=True), \
            mock.patch.object(mission_start.briefing, "ask_briefing",
                              return_value=ANSWERS):
        assert mission_start.run(ARGS, console=_console()) == 0
    assert len(builder.load_spool()[0]["bags"]) == 1


def test_spool_recordings_without_a_mission_block_the_start(fairy_dirs):
    make_bag(paths.bags_dir() / "unbriefed_1", {"/fix": [T0, T0 + 1]})
    fsio.atomic_write_json(paths.harvest_json_path(), {"bags": []})
    console = _console()
    with mock.patch.object(mission_start.briefing, "ask_briefing") as ask:
        assert mission_start.run(ARGS, console=console) == 1
    ask.assert_not_called()
    assert "made while no mission was open" in console.file.getvalue()


def test_ctrl_c_during_the_briefing_changes_nothing(fairy_dirs, tmp_path):
    """S7: the old harvest used to be deleted before the questions."""
    _orphan_harvest(tmp_path)
    with mock.patch.object(mission_start.Confirm, "ask", return_value=False), \
            mock.patch.object(mission_start.briefing, "ask_briefing",
                              side_effect=KeyboardInterrupt):
        assert guarded_main(lambda a: mission_start.run(a, _console()),
                            ARGS) == 130
    assert paths.harvest_json_path().exists()


# -- S3: Docker not captured --------------------------------------------------

def test_docker_not_captured_is_not_every_container_removed(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    a = builder.build(harvest, context)
    h2 = builder.compose_harvest(None, None, None,
                                 {"docker_containers": [], "available": False},
                                 None, {})
    assert h2["software"]["docker_containers"] is None
    b = a.model_copy(deep=True)
    b.software.docker_containers = None
    rows = diff._diff_software(a, b)
    assert ("containers captured", "yes", "no") in rows
    assert not any(r[0].startswith("container ") for r in rows)


# -- S5: index failure after deleting -----------------------------------------

def test_index_failure_after_delete_is_reported(fairy_dirs, monkeypatch):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    assembler.assemble(record, harvest)
    monkeypatch.setattr(mission_delete.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(index, "delete", mock.Mock(
        side_effect=index.sqlite3.OperationalError("readonly database")))
    console = _console()
    args = SimpleNamespace(mission_id=record.identity.mission_id, all=False,
                           today=False)
    with mock.patch.object(mission_delete.Confirm, "ask", return_value=True), \
            mock.patch.object(mission_delete.Prompt, "ask",
                              return_value=mission_delete.PHRASE):
        assert mission_delete.run(args, console=console) == 1
    out = console.file.getvalue()
    assert "Deleted 1 mission" in out and "reindex" in out


# -- S6 / S18: status shows every recording, only live ones as recording ------

def test_status_lists_foreign_recordings(fairy_dirs, tmp_path):
    foreign = _orphan_harvest(tmp_path)
    console = _console()
    status.show_status(None, None, console=console)
    out = console.file.getvalue()
    assert "Recordings waiting" in out and "1 (" in out
    assert str(foreign) in status.status_as_dict(None, None)["recordings"]


def test_status_does_not_show_a_dead_watchdogs_recording(fairy_dirs):
    bag = make_bag(paths.bags_dir() / "rosbag2_x", {"/fix": [T0, T0 + 1]})
    dead = {"state": "RECORDING", "pid": 0x7FFFFFFF,
            "active_bag_dir": str(bag)}
    console = _console()
    status.show_status(dead, None, console=console)
    out = console.file.getvalue()
    assert "growing" not in out and "Recordings waiting" in out
    live = {**dead, "pid": os.getpid()}
    assert status.status_as_dict(live, None)["recording_now"] == str(bag)


def test_close_with_nothing_recorded_mentions_adopt(fairy_dirs):
    console = _console()
    assert mission_close.run(ARGS, console=console) == 1
    assert "ros2 fairy adopt" in console.file.getvalue()


# -- S8: stopping the recorder ------------------------------------------------

class FakeChild:
    def __init__(self, interrupts=0):
        self.interrupts = interrupts
        self.signals, self.terminated = [], False

    def wait(self):
        if self.interrupts:
            self.interrupts -= 1
            raise KeyboardInterrupt
        return 0

    def send_signal(self, sig):
        self.signals.append(sig)

    def terminate(self):
        self.terminated = True


def test_extra_ctrl_c_waits_for_the_recorder_to_close(fairy_dirs):
    child = FakeChild(interrupts=1)
    console = _console()
    mission_record._wait_for_close(child, console)
    assert not child.terminated
    assert "Still saving the recording" in console.file.getvalue()


def test_repeated_ctrl_c_forces_a_stop(fairy_dirs):
    child = FakeChild(interrupts=mission_record.FORCE_AFTER_PRESSES)
    mission_record._wait_for_close(child, _console())
    assert child.terminated


def test_recorder_gets_exactly_one_sigint(fairy_dirs, monkeypatch):
    _spool(fairy_dirs)
    child = FakeChild(interrupts=1)  # the operator's Ctrl-C
    monkeypatch.setattr(mission_record.shutil, "which", lambda c: "/bin/ros2")
    monkeypatch.setattr(mission_record.clock, "is_synchronized", lambda: True)
    with mock.patch.object(mission_record, "_start", return_value=child) as st:
        assert mission_record.run(SimpleNamespace(yes=True),
                                  console=_console()) == 0
    assert child.signals == [signal.SIGINT]
    assert st.call_count == 1


@pytest.mark.skipif(sys.version_info < (3, 11), reason="process_group")
def test_recorder_runs_in_its_own_process_group(monkeypatch):
    popen = mock.Mock()
    monkeypatch.setattr(mission_record.subprocess, "Popen", popen)
    mission_record._start(["ros2", "bag", "record"])
    assert popen.call_args.kwargs.get("process_group") == 0


def test_hangup_stops_the_recorder_cleanly():
    child = mock.Mock()

    def hangup():
        os.kill(os.getpid(), signal.SIGHUP)
        return 0
    child.wait.side_effect = hangup
    with pytest.raises(KeyboardInterrupt):
        mission_record._wait(child)


def test_cut_off_recording_is_reported(tmp_path):
    bag = tmp_path / "rec"
    bag.mkdir()
    (bag / "rec_0.mcap").write_bytes(b"")
    assert "cut off" in mission_record._closed_note(str(bag))
    (bag / "metadata.yaml").write_text("x")
    assert mission_record._closed_note(str(bag)) == ""


# -- S9: no terminal ----------------------------------------------------------

def test_no_terminal_gives_a_plain_message(capsys):
    def prompt(args):
        raise EOFError
    assert guarded_main(prompt, SimpleNamespace()) == 2
    assert "--yes" in capsys.readouterr().err


def test_close_yes_saves_without_asking(fairy_dirs):
    _spool(fairy_dirs)
    with mock.patch.object(mission_close.Confirm, "ask") as ask:
        assert mission_close.run(SimpleNamespace(yes=True, note=""),
                                 console=_console()) == 0
    ask.assert_not_called()
    assert len([p for p in paths.archive_dir().iterdir()
                if not p.name.startswith(".")]) == 1


def test_close_without_briefing_and_terminal_says_what_is_missing(fairy_dirs):
    harvest, _ = _spool(fairy_dirs)
    paths.mission_context_path().unlink()
    console = _console()
    with mock.patch.object(mission_close.briefing, "ask_missing",
                           side_effect=EOFError):
        assert mission_close.run(SimpleNamespace(yes=True, note=""),
                                 console=console) == 1
    assert "mission_start" in console.file.getvalue()


def test_abort_yes(fairy_dirs):
    _spool(fairy_dirs)
    with mock.patch.object(mission_abort.Confirm, "ask") as ask:
        assert mission_abort.run(SimpleNamespace(yes=True),
                                 console=_console()) == 0
    ask.assert_not_called()
    assert not paths.harvest_json_path().exists()


# -- S10 / S11: --json is JSON, errors included -------------------------------

def _json_out(capsys):
    out = capsys.readouterr().out
    return json.loads(out)


def test_adopt_json_survives_long_paths_and_brackets(fairy_dirs, tmp_path,
                                                     capsys):
    from tests.unit.test_foreign_bags import good_pipeline
    fsio.atomic_write_json(paths.harvest_json_path(), good_pipeline())
    bag = make_bag(tmp_path / ("x" * 120) / "[x]run", {"/fix": [T0, T0 + 1]})
    args = SimpleNamespace(bagdir=str(bag), json=True, debug=False)
    assert adopt.run(args, console=Console(width=80)) == 0
    doc = _json_out(capsys)  # no wrapping, no markup, no progress line
    assert doc["path"] == str(bag.resolve()) and doc["status"] == "adopted"


@pytest.mark.parametrize("verb, args", [
    (verify, SimpleNamespace(mission="no-such-mission", json=True)),
    (mission_diff, SimpleNamespace(mission_a="1", mission_b=None, json=True)),
    (export, SimpleNamespace(mission="no-such", json=True, all=False,
                             today=False, output=None, format="zip",
                             force=False)),
])
def test_json_errors_are_json(fairy_dirs, capsys, verb, args):
    paths.index_db_path().touch()
    assert verb.run(args, console=_console()) == 1
    doc = _json_out(capsys)
    assert doc["status"] == "error" and doc["error"]


def test_list_json_index_unavailable(fairy_dirs, capsys, monkeypatch):
    paths.index_db_path().touch()
    monkeypatch.setattr(index, "query", mock.Mock(
        side_effect=index.IndexUnavailableError("no permission")))
    assert list_missions.run(SimpleNamespace(json=True),
                             console=_console()) == 1
    assert _json_out(capsys)["error"] == "no permission"


def test_unexpected_error_with_json_is_json(capsys):
    def boom(args):
        raise RuntimeError("kaput")
    assert guarded_main(boom, SimpleNamespace(json=True)) == 1
    assert "kaput" in _json_out(capsys)["error"]


def test_export_json_has_nothing_else_on_stdout(fairy_dirs, tmp_path, capsys):
    harvest, context = _spool(fairy_dirs)
    assembler.assemble(builder.build(harvest, context), harvest)
    args = SimpleNamespace(mission=None, json=True, all=False, today=False,
                           output=str(tmp_path / "out"), format="zip",
                           force=False)
    assert export.run(args) == 0
    doc = _json_out(capsys)
    assert doc["ok"] is True


# -- S12 / S16: setup ---------------------------------------------------------

def test_setup_rerun_keeps_the_recording_section(fairy_dirs, monkeypatch):
    current = {"robot": {"name": "Jo"}, "owner": {},
               "recording": {"topics": ["/imu/data"], "storage": "mcap"},
               "custom_note": "kept"}
    monkeypatch.setattr(setup_cmd, "_existing_identity", lambda: current)
    monkeypatch.setattr(setup_cmd, "ask_robot", lambda c, cur: {
        "robot": {"name": "Jo", "platform": "p", "serial_number": "s"},
        "owner": {"organization": "o", "contact_email": "a@b.c"}})
    monkeypatch.setattr(setup_cmd, "ask_sensors", lambda c, cur: ([], []))
    monkeypatch.setattr(setup_cmd, "review", lambda c, cfg: True)
    config = setup_cmd._collect(_console())
    assert config["recording"] == current["recording"]
    assert config["custom_note"] == "kept"


def test_setup_checks_ros_with_one_participant(monkeypatch):
    setup_cmd._graph_cache.clear()
    take = mock.Mock(return_value={"nodes": ["/a"],
                                   "topics": [{"name": "/t"}]})
    monkeypatch.setattr("ros_fairy.harvest.ros_snapshot.take", take)
    run = mock.Mock()
    monkeypatch.setattr(setup_cmd.subprocess, "run", run)
    assert setup_cmd._ros2_list("node") == ["/a"]
    assert setup_cmd._ros2_list("topic") == ["/t"]
    take.assert_called_once_with(nodes_only=True)  # one capture, cached
    run.assert_not_called()                        # no `ros2 node list`
    setup_cmd._graph_cache.clear()


# -- S13: the diff on partial data --------------------------------------------

def test_diff_unknown_values(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    a = builder.build(harvest, context)
    b = a.model_copy(deep=True)
    a.sensors[0].detected_at_start = True
    b.sensors[0].detected_at_start = None
    assert diff._diff_sensors(a, b)[0][1:] == ("✓ detected", "unknown")
    b.bags[0].duration_s = None
    row = next(r for r in diff._diff_recordings(a, b) if r[0] == "Duration")
    assert row[2] == "unknown"
    empty = a.model_copy(deep=True)
    empty.ros_graph.nodes, empty.ros_graph.parameters = [], {}
    notes = diff.diff_as_dict(empty, empty.model_copy(deep=True))["notes"]
    assert notes["ROS graph"] == "not captured in either mission"


# -- S15: doctor --------------------------------------------------------------

def test_doctor_spots_a_watchdog_on_old_code(fairy_dirs):
    from ros_fairy.utils import code_id
    paths.watchdog_state_path().write_text(json.dumps({"code_id": "0ld"}))
    assert doctor._check_watchdog_code()["status"] == doctor.WARN
    paths.watchdog_state_path().write_text(
        json.dumps({"code_id": code_id.code_id()}))
    assert doctor._check_watchdog_code()["status"] == doctor.OK
    paths.watchdog_state_path().write_text("{}")
    assert doctor._check_watchdog_code()["status"] == doctor.SKIP


def test_doctor_clock_hint_points_to_repair(monkeypatch):
    monkeypatch.setattr(doctor.clock, "is_synchronized", lambda: False)
    hint = doctor._check_clock()["hint"]
    assert "ros2 fairy repair" in hint and "docs/" not in hint


# -- S17: export paths and checksum failures ----------------------------------

def test_export_to_a_new_folder(tmp_path):
    crate = tmp_path / "2026-10-06_x"
    new_dir = tmp_path / "usb" / "missions"
    assert export._resolve_output(crate, str(new_dir), "zip") == \
        new_dir / "2026-10-06_x.zip"
    named = tmp_path / "usb" / "bundle.zip"
    assert export._resolve_output(crate, str(named), "zip") == named


def test_checksum_failure_removes_the_bundle(fairy_dirs, tmp_path,
                                             monkeypatch):
    harvest, context = _spool(fairy_dirs)
    crate = assembler.assemble(builder.build(harvest, context), harvest)
    real = fsio.atomic_write_text

    def full(path, text, mode=None):
        if str(path).endswith(".sha256"):
            raise OSError(28, "No space left on device")
        return real(path, text, mode)
    monkeypatch.setattr(export.fsio, "atomic_write_text", full)
    result = export._export_one(crate, str(tmp_path / "out") + "/", "zip",
                                False, _console())
    assert result["ok"] is False and "checksum" in result["error"]
    assert not list((tmp_path / "out").glob("*.zip"))


# -- S19 ----------------------------------------------------------------------

def test_today_help_says_recorded():
    import argparse
    parser = argparse.ArgumentParser()
    mission_delete.MissionDeleteVerb().add_arguments(parser, "ros2 fairy")
    assert "recorded today" in parser.format_help()
