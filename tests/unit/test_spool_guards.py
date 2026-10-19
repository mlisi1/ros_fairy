"""Guards that keep mission_abort, drop, mission_close's discard and
mission_start from deleting or hiding recordings they shouldn't: an
interrupted save, a stale watchdog state, a recording started while a
question was open, a recording with no mission yet."""

import io
import os
from types import SimpleNamespace
from unittest import mock

from rich.console import Console

from fairy.archive import assembler
from fairy.subcommands import mission_abort, mission_close, mission_start
from fairy.utils import fsio, paths
from tests.conftest import make_bag
from tests.unit.test_archive import _spool
from tests.unit.test_drop import (
    _ids,
    mission,  # noqa: F401 (fixture)
)
from tests.unit.test_drop import _run as _drop
from tests.unit.test_subcommands import _abort, _open_mission

T0 = 1_790_000_000.0
DEAD = 0x7FFFFFFF  # no such process


def _console():
    return Console(file=io.StringIO(), width=200, force_terminal=False)


def _interrupted(kind):
    return mock.patch.object(
        assembler, "pending_saves",
        return_value=[assembler.PendingSave("m_x", kind, "Survey")])


# -- an interrupted save -------------------------------------------------------

def test_abort_refuses_while_a_save_is_unfinished(fairy_dirs):
    _spool(fairy_dirs)
    bag = paths.bags_dir() / "rosbag2_0"
    for kind in (assembler.RESUME, assembler.TIDY):
        with _interrupted(kind):
            rc, out, asked = _abort([])
        assert rc == 1 and asked == 0
        assert "mission_close" in out
        assert bag.is_dir() and paths.harvest_json_path().exists()


def test_abort_isnt_blocked_by_a_save_that_cant_be_resumed(fairy_dirs):
    _open_mission()
    with _interrupted(assembler.STUCK):
        rc, out, _ = _abort([True])
    assert rc == 0 and "Mission aborted" in out


def test_abort_refused_while_mission_close_runs(fairy_dirs):
    _open_mission()
    with assembler.save_lock():
        rc, out, asked = _abort([])
    assert rc == 1 and asked == 0
    assert "mission_close is already running" in out
    assert paths.mission_context_path().exists()


def test_drop_refuses_while_a_save_is_unfinished(mission):  # noqa: F811
    first, _, _ = mission
    with _interrupted(assembler.RESUME):
        code, out = _drop("1")
    assert code == 1 and "mission_close" in out
    assert first.exists() and len(_ids()) == 3


# -- a dead watchdog's state ----------------------------------------------------

def test_drop_ignores_a_dead_watchdogs_finalising(mission):  # noqa: F811
    first, _, _ = mission
    fsio.atomic_write_json(paths.watchdog_state_path(),
                           {"state": "FINALISING", "pid": DEAD})
    assert _drop("1")[0] == 0 and not first.exists()


def test_drop_waits_for_a_live_finalising(mission):  # noqa: F811
    first, _, _ = mission
    fsio.atomic_write_json(paths.watchdog_state_path(),
                           {"state": "FINALISING", "pid": os.getpid()})
    code, out = _drop("1")
    assert code == 1 and "still processing" in out and first.exists()


# -- a recording started while the question was open ----------------------------

def test_close_discard_refused_when_a_recording_was_made_meanwhile(
        fairy_dirs):
    """Its record is in the mission's files: discarding those would leave
    the new recording without one."""
    _spool(fairy_dirs)
    reviewed = paths.bags_dir() / "rosbag2_0"
    newer = paths.bags_dir() / "rosbag2_new"

    def decide(**kwargs):  # another terminal records while we're asked
        make_bag(newer, {"/fix": [T0, T0 + 1]})
        return "discard"

    with mock.patch.object(mission_close.review, "confirm_save",
                           side_effect=decide):
        assert mission_close.run(SimpleNamespace(),
                                 console=_console()) == 1
    assert reviewed.is_dir() and newer.is_dir()
    assert paths.harvest_json_path().exists()


def test_close_discard_refused_when_recording_started_meanwhile(fairy_dirs):
    _spool(fairy_dirs)
    bag = paths.bags_dir() / "rosbag2_0"
    console = _console()

    def decide(**kwargs):
        fsio.atomic_write_json(paths.watchdog_state_path(),
                               {"state": "RECORDING", "pid": os.getpid()})
        return "discard"

    with mock.patch.object(mission_close.review, "confirm_save",
                           side_effect=decide):
        assert mission_close.run(SimpleNamespace(), console=console) == 1
    assert "nothing was discarded" in " ".join(console.file.getvalue().split())
    assert bag.is_dir() and paths.harvest_json_path().exists()


def test_abort_refused_when_recording_started_at_the_question(fairy_dirs):
    _spool(fairy_dirs)
    bag = paths.bags_dir() / "rosbag2_0"

    def answer(*args, **kwargs):
        fsio.atomic_write_json(paths.watchdog_state_path(),
                               {"state": "RECORDING", "pid": os.getpid()})
        return True

    console = _console()
    with mock.patch.object(mission_abort.Confirm, "ask", side_effect=answer):
        assert mission_abort.run(SimpleNamespace(), console=console) == 1
    assert "nothing was deleted" in " ".join(console.file.getvalue().split())
    assert bag.is_dir() and paths.harvest_json_path().exists()


def test_abort_refused_when_a_recording_was_made_meanwhile(fairy_dirs):
    _spool(fairy_dirs)
    listed = paths.bags_dir() / "rosbag2_0"
    newer = paths.bags_dir() / "rosbag2_new"
    asked = iter([False, True])

    def answer(*args, **kwargs):
        if next(asked):  # the second question: the list was shown
            make_bag(newer, {"/fix": [T0, T0 + 1]})
        return True

    with mock.patch.object(mission_abort.Confirm, "ask", side_effect=answer):
        assert mission_abort.run(SimpleNamespace(),
                                 console=_console()) == 1
    assert listed.is_dir() and newer.is_dir()


# -- mission_start with nothing captured yet ------------------------------------

ANSWERS = {"operator_name": "Jane", "goal": "Map", "location_name": "L",
           "environment": None, "notes": None}


def _start():
    console = _console()
    with mock.patch.object(mission_start.briefing, "ask_briefing",
                           return_value=ANSWERS) as ask:
        rc = mission_start.run(SimpleNamespace(), console=console)
    return rc, " ".join(console.file.getvalue().split()), ask


def test_start_refuses_during_an_unbriefed_recording(fairy_dirs):
    """No briefing, no harvest.json yet (the first minute of a recording)."""
    fsio.atomic_write_json(paths.watchdog_state_path(),
                           {"state": "RECORDING", "pid": os.getpid()})
    rc, out, ask = _start()
    assert rc == 1 and "recording is in progress" in out
    ask.assert_not_called()
    assert not paths.mission_context_path().exists()


def test_start_refuses_with_unsaved_spool_bags_and_no_harvest(fairy_dirs):
    """The watchdog was down: the spool holds a bag nothing has recorded."""
    make_bag(paths.bags_dir() / "rosbag2_0", {"/fix": [T0, T0 + 1]})
    rc, out, ask = _start()
    assert rc == 1 and "mission_close" in out
    ask.assert_not_called()
    assert not paths.mission_context_path().exists()


def test_first_ever_start_with_an_empty_spool(fairy_dirs):
    paths.bags_dir().mkdir(parents=True, exist_ok=True)
    fsio.atomic_write_json(paths.watchdog_state_path(),
                           {"state": "RECORDING", "pid": DEAD})  # stale
    rc, _, ask = _start()
    assert rc == 0
    ask.assert_called_once()
    assert paths.mission_context_path().exists()
