import argparse
import sqlite3

from ros_fairy.archive import index
from ros_fairy.subcommands import (
    completion,
    export,
    mission_delete,
    mission_diff,
    repair,
    verify,
)
from ros_fairy.utils import paths

MISSIONS = [("m-20261012-090000-aaaa", "2026-10-12T09:00:00+00:00",
             "Survey eelgrass beds"),
            ("m-20261013-100000-bbbb", "2026-10-13T10:00:00+00:00",
             "Sonar on the north pier " + "x" * 60)]


def _index(fairy_dirs):
    con = index._connect()
    with con:
        for mid, created, goal in MISSIONS:
            con.execute(
                "INSERT INTO missions (mission_id, created_at, operator, "
                "location, goal, archive_path, schema_version) "
                "VALUES (?, ?, 'Jane', 'Marsh', ?, ?, '1.2')",
                (mid, created, goal, f"/archive/{mid}"))
    con.close()


def test_mission_ids_newest_first_with_description(fairy_dirs):
    _index(fairy_dirs)
    found = completion.mission_ids("")
    assert list(found) == ["m-20261013-100000-bbbb", "m-20261012-090000-aaaa"]
    assert "Survey eelgrass beds" in found["m-20261012-090000-aaaa"]
    assert found["m-20261013-100000-bbbb"].endswith("…")
    assert list(completion.mission_ids("m-20261012")) == \
        ["m-20261012-090000-aaaa"]


def test_mission_ids_without_an_index_or_access(fairy_dirs, monkeypatch):
    assert completion.mission_ids("") == {}
    _index(fairy_dirs)

    def locked():
        raise sqlite3.OperationalError("unable to open database file")
    monkeypatch.setattr(index, "_connect_readonly", locked)
    assert completion.mission_ids("") == {}


def test_many_missions_offer_only_the_newest(fairy_dirs):
    con = index._connect()
    with con:
        for day in range(1, 29):
            for month in (1, 2):
                mid = f"m-2026{month:02d}{day:02d}-090000-{month}{day:03d}"
                con.execute(
                    "INSERT INTO missions (mission_id, created_at, operator, "
                    "location, goal, archive_path, schema_version) "
                    "VALUES (?, ?, 'J', 'M', 'g', ?, '1.2')",
                    (mid, f"2026-{month:02d}-{day:02d}T09:00:00+00:00",
                     f"/archive/{mid}"))
    con.close()
    newest = list(completion.mission_ids("m-"))
    assert len(newest) == completion.MAX_SUGGESTIONS
    assert newest[0] == "m-20260228-090000-2028"
    # Older ones are reached by typing their date.
    assert list(completion.mission_ids("m-2026011")) == [
        f"m-202601{d}-090000-1{d:03d}" for d in range(19, 9, -1)]
    assert list(completion.mission_ids("m-20260105")) == \
        ["m-20260105-090000-1005"]


def test_paths_complete_as_folders(fairy_dirs, tmp_path, monkeypatch):
    _index(fairy_dirs)
    (tmp_path / "bags" / "run_1").mkdir(parents=True)
    (tmp_path / "bags" / "notes.txt").write_text("")
    assert completion.missions_or_folders(f"{tmp_path}/bags/") == \
        {f"{tmp_path}/bags/run_1/": ""}
    monkeypatch.setenv("HOME", str(tmp_path))
    assert completion.missions_or_folders("~/ba") == {"~/bags/": ""}
    assert list(completion.missions_or_folders("m-2026101")) == \
        [m[0] for m in reversed(MISSIONS)]


def test_completion_does_not_write_the_index(fairy_dirs):
    _index(fairy_dirs)
    before = paths.index_db_path().stat().st_mtime_ns
    paths.index_db_path().chmod(0o444)
    try:
        assert completion.mission_ids("")
    finally:
        paths.index_db_path().chmod(0o644)
    assert paths.index_db_path().stat().st_mtime_ns == before


def _completers(verb):
    parser = argparse.ArgumentParser()
    verb.add_arguments(parser, "ros2 fairy")
    return {a.dest: getattr(a, "completer", None) for a in parser._actions}


def test_verbs_complete_missions():
    assert _completers(verify.VerifyVerb())["mission"] is \
        completion.missions_or_folders
    assert _completers(repair.RepairVerb())["mission"] is \
        completion.missions_or_folders
    diff = _completers(mission_diff.DiffVerb())
    assert diff["mission_a"] is diff["mission_b"] is \
        completion.missions_or_folders
    exp = _completers(export.ExportVerb())
    assert exp["mission"] is completion.missions_or_folders
    assert exp["exclude"] is completion.mission_ids
    assert exp["all"] is None
    assert _completers(mission_delete.MissionDeleteVerb())["mission_id"] is \
        completion.mission_ids
