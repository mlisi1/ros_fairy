"""`ros2 fairy drop`: take one recording out of the open mission, by the
number mission_status shows."""

import io
from types import SimpleNamespace
from unittest import mock

import pytest
from rich.console import Console

from ros_fairy.manifest import builder
from ros_fairy.subcommands import drop
from ros_fairy.ui import status
from ros_fairy.utils import fsio, paths
from ros_fairy.watchdog import watchdog as wd
from tests.conftest import make_bag

T0 = 1_790_000_000.0


@pytest.fixture
def mission(fairy_dirs, tmp_path):
    """An open mission with two spool recordings and one made elsewhere."""
    fsio.atomic_write_json(paths.mission_context_path(),
                           builder.new_mission_context("Ana", "Map", "Lab"))
    first = make_bag(paths.bags_dir() / "m_20261015-100000",
                     {"/fix": [T0, T0 + 1]})
    second = make_bag(paths.bags_dir() / "m_20261015-110000",
                      {"/fix": [T0 + 10, T0 + 11]})
    outside = make_bag(tmp_path / "elsewhere" / "run1",
                       {"/scan": [T0 + 20, T0 + 21]})
    wd.append_bag_record(first)
    wd.append_bag_record(second)
    wd.append_bag_record(outside, source="detected")
    return first, second, outside


def _run(recording=None, yes=True, tty=False, answer=""):
    console = Console(file=io.StringIO(), width=200)
    with mock.patch.object(drop.sys.stdin, "isatty", return_value=tty), \
            mock.patch.object(drop.Prompt, "ask", return_value=answer):
        code = drop.run(SimpleNamespace(recording=recording, yes=yes),
                        console=console)
    return code, " ".join(console.file.getvalue().split())


def _ids():
    return {b["path"]: b["recording_id"]
            for b in builder.load_spool()[0]["bags"]}


def test_recordings_are_numbered_as_they_join(mission):
    first, second, outside = mission
    assert _ids() == {str(first): 1, str(second): 2, str(outside): 3}


def test_numbers_stay_put_and_are_never_reused(mission, tmp_path):
    first, second, outside = mission
    assert _run("2")[0] == 0
    assert _ids() == {str(first): 1, str(outside): 3}
    assert _run("#3")[0] == 0  # the highest: still not given out again
    later = make_bag(paths.bags_dir() / "m_20261015-120000",
                     {"/fix": [T0 + 30, T0 + 31]})
    wd.append_bag_record(later)
    assert _ids() == {str(first): 1, str(later): 4}


def test_re_recording_a_folder_keeps_its_number(mission):
    first, _, _ = mission
    wd.append_bag_record(first)
    assert _ids()[str(first)] == 1


def test_drop_a_spool_recording(mission):
    first, second, outside = mission
    code, out = _run("1")
    assert code == 0
    assert not first.exists()
    assert set(_ids()) == {str(second), str(outside)}
    assert "now has 2 recordings" in out


def test_detach_a_recording_made_elsewhere(mission):
    _, _, outside = mission
    code, out = _run("3")
    assert code == 0
    assert outside.is_dir()  # stays on disk
    assert str(outside) not in _ids()
    assert f"ros2 fairy adopt {outside}" in out


def test_by_path(mission):
    _, second, _ = mission
    assert _run(str(second))[0] == 0 and not second.exists()


def test_declining_changes_nothing(mission):
    first, _, _ = mission
    with mock.patch.object(drop, "confirm", return_value=False):
        code, out = _run("1", yes=False)
    assert code == 0 and "Nothing was changed" in out
    assert first.exists() and len(_ids()) == 3


def test_unknown_number(mission):
    code, out = _run("7")
    assert code == 1 and "no recording 7" in out
    assert len(_ids()) == 3


def test_without_an_argument_and_no_terminal_it_lists(mission):
    code, out = _run(None)
    assert code == 2
    assert "#1" in out and "#3" in out and "run1" in out
    assert "ros2 fairy drop <#>" in out


def test_without_an_argument_on_a_terminal_it_asks(mission):
    first, _, _ = mission
    assert _run(None, tty=True, answer="#1")[0] == 0
    assert not first.exists()
    code, out = _run(None, tty=True, answer="")
    assert code == 0 and "Nothing was changed" in out


def test_an_unprocessed_recording_has_no_number_but_drops_by_path(fairy_dirs):
    bag = make_bag(paths.bags_dir() / "m_1", {"/fix": [T0, T0 + 1]})
    [rec] = status.recording_list()
    assert rec["id"] is None
    assert "being processed" in status.recording_line(rec)
    assert _run("1")[0] == 1
    assert _run(str(bag))[0] == 0 and not bag.exists()


def test_a_dropped_spool_recording_isnt_salvaged_at_close(mission):
    # mission_close re-attaches spool folders missing from harvest.json; the
    # folder must be gone, not only the record.
    first, _, _ = mission
    _run("1")
    assert first not in status.mission_recordings()


def test_refused_while_recording(mission):
    with mock.patch.object(drop.mission_close, "_recording_in_progress",
                           return_value=True):
        code, out = _run("1")
    assert code == 1 and "recording is in progress" in out
    assert len(_ids()) == 3


def test_nothing_to_drop(fairy_dirs):
    code, out = _run("1")
    assert code == 0 and "no recordings" in out


# --- where the numbers show up -------------------------------------------------

def test_mission_status_shows_the_numbers(mission):
    console = Console(file=io.StringIO(), width=200)
    status.show_status(None, builder.load_spool()[1], console=console)
    out = console.file.getvalue()
    assert " #1  " in out and " #3  " in out
    assert "ros2 fairy drop <#>" in out
    ids = status.status_as_dict(None, None)["recording_ids"]
    assert sorted(ids.values()) == [1, 2, 3]


def test_the_counter_survives_a_context_harvest(mission):
    from tests.unit.test_watchdog import good_pipeline
    first, second, outside = mission
    _run("3")
    wd.Watchdog._save_harvest(None, good_pipeline())
    later = make_bag(paths.bags_dir() / "m_later", {"/fix": [T0, T0 + 1]})
    wd.append_bag_record(later)
    assert _ids()[str(later)] == 4


def test_the_saved_record_has_no_numbers(mission):
    harvest, context = builder.load_spool()
    from tests.unit.test_watchdog import good_pipeline
    harvest = {**good_pipeline(), "bags": harvest["bags"]}
    record = builder.build(harvest, context)
    assert len(record.bags) == 3  # validated without recording_id
