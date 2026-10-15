"""mission_start from a script: answers as options, --yes, --json."""

import io
import json
from types import SimpleNamespace
from unittest import mock

import pytest
from rich.console import Console

from ros_fairy.manifest import builder
from ros_fairy.subcommands import guarded_main, mission_start
from ros_fairy.utils import fsio, paths
from tests.conftest import make_bag
from tests.unit.test_operator_failures import _orphan_harvest

T0 = 1_790_000_000.0
FULL = dict(operator="Ana", goal="Survey the pier", location="North pier",
            environment="marine", notes="calm sea")


def _console():
    return Console(file=io.StringIO(), width=200, force_terminal=False)


def _run(tty=False, **options):
    console = _console()
    args = SimpleNamespace(**options)
    with mock.patch.object(mission_start.sys.stdin, "isatty",
                           return_value=tty), \
            mock.patch.object(mission_start.Confirm, "ask",
                              side_effect=EOFError) as confirm, \
            mock.patch.object(mission_start.briefing, "ask_briefing",
                              side_effect=EOFError) as wizard:
        code = guarded_main(lambda a: mission_start.run(a, console=console),
                            args)
    return code, console.file.getvalue(), confirm, wizard


def _context():
    return json.loads(paths.mission_context_path().read_text())


def test_every_answer_as_options_asks_nothing(fairy_dirs):
    code, out, confirm, wizard = _run(**FULL)
    assert code == 0
    wizard.assert_not_called()
    context = _context()
    assert context["identity"]["operator_name"] == "Ana"
    assert context["intent"] == {"goal": "Survey the pier",
                                 "location_name": "North pier",
                                 "environment": "marine", "notes": "calm sea"}
    assert paths.session_env_path().is_file()


def test_optional_answers_may_be_left_out(fairy_dirs):
    code, _, _, _ = _run(operator="Ana", goal="Map", location="Lab")
    assert code == 0
    assert _context()["intent"]["environment"] is None
    assert _context()["intent"]["notes"] is None


def test_missing_required_answers_without_a_terminal(fairy_dirs):
    code, out, _, _ = _run(operator="Ana", goal="  ")
    assert code == 2
    assert "--goal, --location" in out
    assert not paths.mission_context_path().exists()


def test_missing_required_answers_are_asked_on_a_terminal(fairy_dirs):
    with mock.patch.object(mission_start.briefing, "ask_missing",
                           return_value={"location_name": "Lab"}) as ask:
        code, _, _, _ = _run(tty=True, operator="Ana", goal="Map")
    assert code == 0
    assert ask.call_args.args[0] == ["location_name"]
    assert _context()["intent"]["location_name"] == "Lab"


def test_json_prints_the_new_mission(fairy_dirs, capsys):
    code, out, _, _ = _run(json=True, operator="Ana", goal="Map",
                           location="Lab")
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["mission_id"] == _context()["identity"]["mission_id"]
    assert printed["goal"] == "Map"
    assert "Mission briefing saved" not in out


def test_json_without_answers_is_refused(fairy_dirs, capsys):
    code, _, _, wizard = _run(json=True)
    assert code == 2
    assert "--operator" in json.loads(capsys.readouterr().out)["error"]
    wizard.assert_not_called()


def _unfinished_mission():
    fsio.atomic_write_json(paths.mission_context_path(),
                           builder.new_mission_context("Sam", "Old", "Lab"))


def test_yes_replaces_an_unfinished_mission(fairy_dirs):
    _unfinished_mission()
    code, out, confirm, _ = _run(yes=True, **FULL)
    assert code == 0
    confirm.assert_not_called()
    assert "replace it? yes (--yes)" in out
    assert _context()["identity"]["operator_name"] == "Ana"


def test_without_yes_and_no_terminal_the_old_mission_stays(fairy_dirs):
    _unfinished_mission()
    code, _, _, _ = _run(**FULL)
    assert code == 2  # needed to ask, nobody to answer
    assert _context()["identity"]["operator_name"] == "Sam"


def test_yes_never_replaces_a_mission_with_recordings(fairy_dirs, capsys):
    _unfinished_mission()
    make_bag(paths.bags_dir() / "rec_1", {"/fix": [T0, T0 + 1]})
    code, _, _, _ = _run(json=True, **FULL)
    assert code == 1
    assert "mission_close" in json.loads(capsys.readouterr().out)["error"]
    assert _context()["identity"]["operator_name"] == "Sam"


def test_yes_leaves_earlier_recordings_out(fairy_dirs, tmp_path):
    foreign = _orphan_harvest(tmp_path)
    code, out, confirm, _ = _run(yes=True, **FULL)
    assert code == 0
    confirm.assert_not_called()
    assert "--yes keeps them out" in out
    assert not paths.harvest_json_path().exists()
    assert foreign.is_dir()


@pytest.mark.parametrize("given", [{}, {"notes": "x"}])
def test_no_answers_runs_the_wizard_on_a_terminal(fairy_dirs, given):
    answers = {"operator_name": "Ana", "goal": "Map", "location_name": "Lab",
               "environment": None, "notes": None}
    with mock.patch.object(mission_start.sys.stdin, "isatty",
                           return_value=True), \
            mock.patch.object(mission_start.briefing, "ask_briefing",
                              return_value=answers) as wizard, \
            mock.patch.object(mission_start.briefing, "ask_missing",
                              return_value={"operator_name": "Ana",
                                            "goal": "Map",
                                            "location_name": "Lab"}) as some:
        assert mission_start.run(SimpleNamespace(**given),
                                 console=_console()) == 0
    # No option: the whole wizard. One option: only the missing questions.
    assert wizard.called == (not given)
    assert some.called == bool(given)
