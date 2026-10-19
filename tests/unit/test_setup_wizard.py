"""ros2 fairy setup's wizard: answers, the data folder, the service start."""

import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from rich.console import Console

from fairy.subcommands import setup as setup_cmd
from fairy.utils import paths


def _console():
    return Console(file=io.StringIO(), width=120, force_terminal=False)


def _run(args, collect=None, **collect_kwargs):
    """setup.run as a normal user, ROS visible; returns (code, output,
    the _collect mock)."""
    console = _console()
    with mock.patch.object(setup_cmd.os, "geteuid", return_value=1000), \
            mock.patch.object(setup_cmd, "_check_ros_visible",
                              return_value=True), \
            mock.patch.object(setup_cmd, "_choose_service_python",
                              return_value="/usr/bin/python3"), \
            mock.patch.object(setup_cmd.shutil, "which",
                              return_value="/usr/bin/docker"), \
            mock.patch.object(setup_cmd, "_collect",
                              **({"side_effect": collect} if collect else
                                 {"return_value": None}),
                              **collect_kwargs) as collect_mock:
        code = setup_cmd.run(args, console=console)
    return code, console.file.getvalue(), collect_mock


# --- a broken paths.yaml -------------------------------------------------------

@pytest.fixture
def broken_paths(fairy_dirs, monkeypatch):
    monkeypatch.delenv(paths.VAR_DIR_ENV)
    paths.paths_file().write_text("var_dir: relative/path\n")
    with pytest.raises(paths.PathsConfigError):
        paths.var_dir()


def test_var_dir_can_replace_a_broken_paths_file(broken_paths, tmp_path):
    # The broken file's own message says to run exactly this.
    target = tmp_path / "data"
    code, out, collect = _run(SimpleNamespace(var_dir=str(target)))
    assert code == 0, out
    assert collect.call_args.args[2] == target
    assert "isn't an absolute path" in out
    setup_cmd.write_paths_file(target)
    assert paths.var_dir() == target


def test_wizard_offers_the_default_folder_over_a_broken_paths_file(
        broken_paths):
    console = _console()
    assert setup_cmd.current_var_dir(console) is None
    with mock.patch.object(setup_cmd.Prompt, "ask",
                           side_effect=lambda *a, **k: k["default"]) as ask:
        assert setup_cmd.ask_var_dir(console, None) == \
            Path(paths.DEFAULT_VAR_DIR)
    assert ask.call_args.kwargs["default"] == paths.DEFAULT_VAR_DIR
    setup_cmd.write_paths_file(Path(paths.DEFAULT_VAR_DIR))
    assert not paths.paths_file().exists()


# --- the shell's ROS_FAIRY_VAR_DIR ---------------------------------------------

def test_wizard_refuses_when_the_shell_overrides_the_data_folder(
        fairy_dirs):
    with mock.patch.object(setup_cmd, "ask_robot") as ask_robot, \
            mock.patch.object(setup_cmd, "ask_var_dir") as ask_var_dir, \
            pytest.raises(setup_cmd.SetupAborted, match="ROS_FAIRY_VAR_DIR"):
        setup_cmd._collect(_console(), [])
    ask_robot.assert_not_called()   # before any question
    ask_var_dir.assert_not_called()


def test_wizard_with_var_dir_given_ignores_the_variable(fairy_dirs):
    with mock.patch.object(setup_cmd, "ask_robot",
                           return_value={"robot": {}, "owner": {}}), \
            mock.patch.object(setup_cmd, "ask_sensors",
                              return_value=([], [])), \
            mock.patch.object(setup_cmd, "review", return_value=True):
        assert setup_cmd._collect(_console(), [], fairy_dirs["var"]) is not None


# --- answers -------------------------------------------------------------------

def test_a_number_from_the_file_is_a_usable_default():
    # serial_number: 2031 in the YAML is an int; Enter keeps it.
    with mock.patch.object(setup_cmd.Prompt, "ask",
                           side_effect=lambda *a, **k: k["default"]):
        assert setup_cmd._ask(_console(), "Serial", default=2031) == "2031"


def test_giving_up_on_a_question_exits_1(fairy_dirs):
    def abort(*args):
        raise setup_cmd.SetupAborted("Gave up on 'Robot name' after 3 "
                                     "attempts.")
    code, out, _ = _run(SimpleNamespace(), collect=abort)
    assert code == 1
    assert "Gave up" in out and "Nothing was written" in out


def test_declining_the_review_still_exits_0(fairy_dirs):
    code, _, _ = _run(SimpleNamespace())
    assert code == 0


def test_wizard_names_a_new_calibration_apart_from_one_in_use(fairy_dirs,
                                                             tmp_path):
    # gps1 was gps0 once and still uses gps0_cal: a new gps0 mustn't add a
    # second gps0_cal.
    cal = tmp_path / "new.yaml"
    cal.write_text("k: 1\n")
    current = {
        "sensors": [{"sensor_id": "gps1", "type": "gps", "make_model": "X",
                     "topic": "/g1", "calibration": "gps0_cal"}],
        "calibrations": [{"name": "gps0_cal", "source_path": "/old.yaml"}]}
    confirms = iter([True, True, False])   # keep, add one, no more
    prompts = iter(["gps0", "gps", "Y", "/g0", "", str(cal)])
    with mock.patch.object(setup_cmd.Confirm, "ask",
                           side_effect=lambda *a, **k: next(confirms)), \
            mock.patch.object(setup_cmd.Prompt, "ask",
                              side_effect=lambda *a, **k: next(prompts)), \
            mock.patch.object(setup_cmd, "_live_topics", return_value=[]):
        sensors, calibrations = setup_cmd.ask_sensors(_console(), current)
    names = [c["name"] for c in calibrations]
    assert len(names) == len(set(names)) == 2
    assert sensors[0]["calibration"] == "gps0_cal"
    new = next(c for c in calibrations if c["name"] == sensors[1]["calibration"])
    assert new["source_path"] == str(cal.resolve())


# --- the watchdog service coming up --------------------------------------------

@pytest.mark.parametrize("state_pid, main_pids, ok", [
    (222, [222], True),        # the new watchdog wrote its state file
    (111, [222], True),        # still busy over the spool, up all along
    (111, [222, 333], False),  # crash loop: systemd keeps restarting it
])
def test_service_counts_only_a_state_file_from_the_new_watchdog(
        fairy_dirs, tmp_path, state_pid, main_pids, ok):
    # On a re-run the previous watchdog (pid 111) left watchdog.state behind;
    # systemd's new one is main_pids[0].
    paths.watchdog_state_path().write_text(json.dumps({"pid": state_pid}))
    pids = iter(main_pids)
    last = [main_pids[0]]

    def systemctl(cmd, **kwargs):
        if cmd[1] == "show":
            last[0] = next(pids, last[0])
            return SimpleNamespace(returncode=0, stdout=f"{last[0]}\n")
        out = {"is-active": "active\n"}.get(cmd[1], "")
        return SimpleNamespace(returncode=0, stdout=out)
    clock = iter(range(0, 100))
    with mock.patch.object(setup_cmd, "interpreter_root_safe",
                           return_value=True), \
            mock.patch.object(setup_cmd, "write_watchdog_env"), \
            mock.patch.object(setup_cmd, "UNIT_DIR", tmp_path), \
            mock.patch.object(setup_cmd.subprocess, "run",
                              side_effect=systemctl), \
            mock.patch.object(setup_cmd.time, "sleep"), \
            mock.patch.object(setup_cmd.time, "monotonic",
                              side_effect=lambda: next(clock)):
        assert setup_cmd.install_service(_console(), {}) is ok


def test_optional_sensor_answers_show_no_empty_default(monkeypatch):
    # Rich shows an empty default as "()": "TF frame id (Enter to skip) ():".
    answers = iter(["y", "gps0", "gps", "u-blox F9P", "/fix", "", "", "n"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    console = _console()
    with mock.patch.object(setup_cmd, "_live_topics", return_value=[]):
        sensors, _ = setup_cmd.ask_sensors(console, {})
    assert sensors[0]["sensor_id"] == "gps0"
    out = console.file.getvalue()
    assert "TF frame id (Enter to skip):" in out
    assert "()" not in out
