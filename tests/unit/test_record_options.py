"""mission_record -- <options for ros2 bag record>."""

import argparse
import io
import sys
import types
from types import SimpleNamespace
from unittest import mock

import pytest
from rich.console import Console

from ros_fairy.subcommands import mission_record


class FakeRecordVerb:
    """The parts of ros2bag's RecordVerb these tests need, same dests."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument("-o", "--output")
        parser.add_argument("-s", "--storage", default="mcap")
        parser.add_argument("topics_positional", nargs="*", default=[])
        parser.add_argument("--topics", nargs="+")
        parser.add_argument("-a", "--all", action="store_true")
        parser.add_argument("-e", "--regex")
        parser.add_argument("--max-bag-size", type=int, default=0)
        parser.add_argument("--compression-mode", default="none",
                            choices=["none", "file", "message"])


@pytest.fixture
def configured(monkeypatch):
    """robot_identity.yaml's `recording:` section: /fix, as MCAP."""
    from ros_fairy.harvest import robot_identity
    monkeypatch.setattr(robot_identity, "harvest", lambda: {
        "recording": {"topics": ["/fix"], "storage": "mcap"}})


@pytest.fixture
def ros2bag(monkeypatch):
    verb = types.ModuleType("ros2bag.verb.record")
    verb.RecordVerb = FakeRecordVerb
    for name, module in (("ros2bag", types.ModuleType("ros2bag")),
                         ("ros2bag.verb", types.ModuleType("ros2bag.verb")),
                         ("ros2bag.verb.record", verb)):
        monkeypatch.setitem(sys.modules, name, module)


def test_options_go_last_and_keep_the_identity_selection(
        fairy_dirs, configured, ros2bag):
    cmd = mission_record.build_record_command(
        "/out", ["--max-bag-size", "1000", "--compression-mode", "file"])
    out = cmd.index("--output")
    assert cmd[out:] == ["--output", "/out", "--max-bag-size", "1000",
                         "--compression-mode", "file"]
    assert "/fix" in cmd[:out] and "--all" not in cmd


@pytest.mark.parametrize("extra", [["/scan"], ["--topics", "/scan"],
                                   ["-a"], ["-e", "/cam.*"]])
def test_a_selection_of_your_own_replaces_the_identity_one(
        fairy_dirs, configured, ros2bag, extra):
    cmd = mission_record.build_record_command("/out", extra)
    ours = cmd[:cmd.index("--output")]
    assert ours == ["ros2", "bag", "record", "--storage", "mcap"]
    assert cmd[-len(extra):] == extra


def test_without_identity_topics_all_is_used_unless_you_choose(fairy_dirs,
                                                              ros2bag):
    assert "--all" in mission_record.build_record_command("/out", [])
    cmd = mission_record.build_record_command("/out", ["/scan"])
    assert "--all" not in cmd and cmd[-1] == "/scan"


def test_storage_of_your_own_replaces_the_identity_one(fairy_dirs, configured,
                                                      ros2bag):
    cmd = mission_record.build_record_command("/out", ["-s", "sqlite3"])
    assert cmd.count("-s") + cmd.count("--storage") == 1
    assert cmd[-2:] == ["-s", "sqlite3"]


@pytest.mark.parametrize("extra", [["-o", "/elsewhere"],
                                   ["--output", "/elsewhere"]])
def test_output_is_refused(ros2bag, extra):
    with pytest.raises(mission_record.RecordOptionsError, match="spool"):
        mission_record.given_options(extra)


def test_output_is_refused_without_ros2bag(monkeypatch):
    monkeypatch.setitem(sys.modules, "ros2bag.verb.record", None)
    with pytest.raises(mission_record.RecordOptionsError):
        mission_record.given_options(["--output=/elsewhere"])
    assert mission_record.given_options(["--max-bag-size", "5"]) == set()


def test_rosbag2_rejects_what_it_doesnt_know(ros2bag):
    with pytest.raises(mission_record.RecordOptionsError,
                       match="unrecognized arguments: --frobnicate"):
        mission_record.given_options(["--frobnicate"])
    with pytest.raises(mission_record.RecordOptionsError, match="invalid"):
        mission_record.given_options(["--compression-mode", "zip"])


def _run(extra, **patches):
    console = Console(file=io.StringIO(), width=200)
    args = SimpleNamespace(record_args=extra, yes=True)
    with mock.patch.object(mission_record.shutil, "which",
                           return_value="/usr/bin/ros2"), \
            mock.patch.object(mission_record.clock, "is_synchronized",
                              return_value=True), \
            mock.patch.object(mission_record.subprocess, "Popen") as popen, \
            mock.patch.object(mission_record.subprocess, "run",
                              return_value=SimpleNamespace(returncode=0)
                              ) as run:
        popen.return_value.wait.return_value = 0
        code = mission_record.run(args, console=console)
    return code, console.file.getvalue(), popen, run


def test_run_refuses_bad_options_before_recording(fairy_dirs, ros2bag):
    code, out, popen, _ = _run(["-o", "/elsewhere"])
    assert code == 1
    assert "-o/--output can't be used" in out
    popen.assert_not_called()


def test_run_passes_options_through(fairy_dirs, ros2bag):
    code, out, popen, _ = _run(["/scan", "--max-bag-size", "5"])
    assert code == 0
    command = popen.call_args.args[0]
    assert command[-3:] == ["/scan", "--max-bag-size", "5"]
    assert "instead of robot_identity.yaml's selection" in out


def _verb_parser():
    parser = argparse.ArgumentParser()
    mission_record.MissionRecordVerb().add_arguments(parser, "ros2 fairy")
    return parser


def test_verb_takes_rosbag2_options_directly(ros2bag):
    args = _verb_parser().parse_args(
        ["--yes", "--max-bag-size", "5", "/a", "--debug"])
    assert args.yes is True and args.debug is True
    assert args.max_bag_size == 5 and args.topics_positional == ["/a"]


def test_verb_rejects_what_rosbag2_doesnt_have(ros2bag, capsys):
    with pytest.raises(SystemExit):
        _verb_parser().parse_args(["--frobnicate"])
    assert "unrecognized arguments: --frobnicate" in capsys.readouterr().err


def test_verb_help_lists_rosbag2_options(ros2bag, capsys):
    with pytest.raises(SystemExit):
        _verb_parser().parse_args(["-h"])
    out = capsys.readouterr().out
    assert "options passed on to ros2 bag record" in out
    assert "--max-bag-size" in out and "--yes" in out


def test_verb_without_rosbag2_takes_only_its_own(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "ros2bag.verb.record", None)
    with pytest.raises(SystemExit):
        _verb_parser().parse_args(["--max-bag-size", "5"])


@pytest.mark.parametrize("argv, tokens", [
    (["/opt/ros/jazzy/bin/ros2", "fairy", "mission_record", "--yes",
      "--max-bag-size", "5", "/a", "--debug"], ["--max-bag-size", "5", "/a"]),
    (["ros2", "fairy", "mission_record"], []),
    (["ros2", "fairy", "mission_record", "-y", "--", "/a"], ["/a"]),
    (["python3", "-m", "something"], []),
])
def test_record_tokens(argv, tokens):
    assert mission_record.record_tokens(argv) == tokens


def test_main_hands_the_typed_options_to_run(monkeypatch):
    seen = {}
    monkeypatch.setattr(mission_record, "guarded_main",
                        lambda run, args: seen.setdefault("args", args) and 0)
    monkeypatch.setattr(mission_record.sys, "argv",
                        ["ros2", "fairy", "mission_record", "-y",
                         "--qos-profile-overrides-path", "qos.yaml"])
    mission_record.MissionRecordVerb().main(args=SimpleNamespace(yes=True))
    assert seen["args"].record_args == ["--qos-profile-overrides-path",
                                        "qos.yaml"]


def test_a_saved_storage_rosbag2_lacks_stops_before_recording(
        fairy_dirs, monkeypatch):
    from ros_fairy.harvest import robot_identity
    from ros_fairy.subcommands import doctor
    monkeypatch.setattr(robot_identity, "harvest", lambda: {
        "robot": {}, "recording": {"topics": None, "storage": "bogus"}})
    monkeypatch.setattr(mission_record, "storage_names",
                        lambda: ["mcap", "sqlite3"])
    monkeypatch.setattr(mission_record.shutil, "which", lambda name: "/x")
    console = Console(file=io.StringIO(), width=200)
    with mock.patch.object(mission_record, "_start") as start:
        assert mission_record.run(SimpleNamespace(yes=True), console) == 1
    start.assert_not_called()
    out = " ".join(console.file.getvalue().split())
    assert "storage 'bogus', which rosbag2 here doesn't have (mcap, " \
        "sqlite3)" in out and "--storage NAME" in out
    check = doctor._check_identity()
    assert check["status"] == doctor.FAIL and "bogus" in check["detail"]
