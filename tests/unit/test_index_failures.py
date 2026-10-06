"""Index, lookup and schema: FAILURE_CASES I1-I5."""

import json
import os
import shutil
import time
from datetime import datetime, timezone

import pytest

from ros_fairy import SCHEMA_VERSION
from ros_fairy.archive import assembler, duplicates, index, locate, ro_crate
from ros_fairy.manifest import builder
from ros_fairy.manifest.schema import read_record
from ros_fairy.subcommands import verify
from ros_fairy.utils import paths
from tests.unit.test_archive import _spool


def _saved(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    return assembler.assemble(record, harvest), record


def _edit_record(crate, change):
    path = crate / "mission_record.json"
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))
    (crate / "checksums.sha256").unlink(missing_ok=True)


# -- I1: records from a newer ros-fairy -----------------------------------------

def _newer_minor(data):
    data["schema_version"] = "1.9"
    data["future_section"] = {"x": 1}
    data["software"]["future_field"] = True


def test_newer_minor_record_is_read_and_indexed(fairy_dirs):
    crate, record = _saved(fairy_dirs)
    _edit_record(crate, _newer_minor)
    report = {}
    assert index.reindex(report=report) == 1 and report["skipped"] == []
    loaded, set_aside = locate.load_record_with_notes(crate)
    assert loaded.identity.mission_id == record.identity.mission_id
    assert set_aside == ["MissionRecord.future_section",
                         "Software.future_field"]
    checks = verify.verify_archive(crate)
    assert any("Saved by a newer ros-fairy" in c["title"] for c in checks)


def test_newer_major_record_is_refused_plainly(fairy_dirs):
    crate, _ = _saved(fairy_dirs)
    _edit_record(crate, lambda d: d.update(schema_version="2.0"))
    with pytest.raises(locate.LocateError, match="update ros-fairy"):
        locate.load_record(crate)
    report = {}
    assert index.reindex(report=report) == 0
    [(folder, reason)] = report["skipped"]
    assert folder == crate.name and "newer" in reason


def test_strict_validation_still_guards_new_records(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    harvest["software"]["typo_field"] = 1
    with pytest.raises(Exception):
        builder.build(harvest, context)


def test_record_format_is_versioned():
    assert SCHEMA_VERSION == "1.1"
    record, _ = read_record(json.loads(json.dumps({
        "identity": {"mission_id": "m", "created_at": "2026-10-06T08:00:00Z",
                     "operator_name": "J"},
        "intent": {"goal": "g", "location_name": "l"},
        "software": {"ros_fairy_version": "0.1.0"},
        "provenance": {"ros_fairy_version": "0.1.0"}})))
    assert record.schema_version == "1.1"  # written records carry it


def test_same_mission_saved_twice_is_reported(fairy_dirs):
    crate, record = _saved(fairy_dirs)
    copy = crate.with_name(crate.name + "_copy")
    shutil.copytree(crate, copy)
    _edit_record(copy, lambda d: d["provenance"].update(
        assembled_at="2099-01-01T00:00:00+00:00"))
    report = {}
    assert index.reindex(report=report) == 1
    assert report["duplicates"] == {
        record.identity.mission_id: sorted([str(crate), str(copy)])}
    # the most recently assembled save is the one listed
    assert index.find_mission(record.identity.mission_id)["archive_path"] \
        == str(copy)


# -- I2: one date for the ID, the folder and --since/--until ---------------------

@pytest.fixture
def rome():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Rome"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_id_and_folder_agree_after_midnight(fairy_dirs, rome):
    # 23:30 UTC on 6 October is 01:30 on 7 October in Italy.
    when = datetime(2026, 10, 6, 23, 30, tzinfo=timezone.utc)
    assert builder.new_mission_id(when).startswith("m-20261007-013000-")
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    record.identity.created_at = when
    assert assembler.archive_name(record).startswith("2026-10-07_01-30-00")


def test_since_and_until_are_local_days(fairy_dirs, rome):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    record.identity.created_at = datetime(2026, 10, 6, 23, 30,
                                          tzinfo=timezone.utc)
    index.insert(record, paths.archive_dir() / "x")
    q = index.query
    assert q(since="2026-10-07")[1] == 1   # it is the 7th in Italy
    assert q(until="2026-10-06")[1] == 0
    assert q(since="2026-10-07", until="2026-10-07")[1] == 1


def test_index_stores_utc(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    record.identity.created_at = datetime.fromisoformat(
        "2026-10-07T01:30:00+02:00")
    index.insert(record, paths.archive_dir() / "x")
    [row], _ = index.query()
    assert row["created_at"] == "2026-10-06T23:30:00+00:00"


# -- I3: timestamps without a timezone -------------------------------------------

def test_naive_times_do_not_crash_the_duplicate_check(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    index.insert(record, paths.archive_dir() / "x")
    # an old row stored without an offset
    con = index._connect()
    with con:
        con.execute("UPDATE missions SET created_at = ?, mission_id = 'old'",
                    (record.identity.created_at.replace(tzinfo=None)
                     .isoformat(),))
    con.close()
    rows = duplicates.find_similar(record)
    assert [r["mission_id"] for r in rows] == ["old"]
    assert "ago" in duplicates.describe(record, rows)


def test_naive_times_in_a_record_are_read_as_utc(fairy_dirs):
    crate, _ = _saved(fairy_dirs)
    _edit_record(crate, lambda d: d["identity"].update(
        created_at="2026-10-06T08:00:00"))
    record = locate.load_record(crate)
    assert record.identity.created_at.tzinfo is not None


# -- I4: resolving identifiers -----------------------------------------------------

def test_an_existing_folder_beats_the_number_reading(fairy_dirs, monkeypatch,
                                                     tmp_path):
    crate, _ = _saved(fairy_dirs)
    numbered = tmp_path / "2"
    shutil.copytree(crate, numbered)
    monkeypatch.chdir(tmp_path)
    assert locate.resolve_archive("2") == numbered.relative_to(tmp_path)


def test_only_plain_digits_are_numbers(fairy_dirs):
    _saved(fairy_dirs)
    assert locate.resolve_archive("1").is_dir()
    for odd in (" 1", "+1"):
        with pytest.raises(locate.LocateError, match="Can't find a mission"):
            locate.resolve_archive(odd)


def test_stale_index_entry_points_to_reindex(fairy_dirs):
    crate, record = _saved(fairy_dirs)
    crate.rename(crate.with_name("moved_by_hand"))
    for identifier in ("1", record.identity.mission_id):
        with pytest.raises(locate.LocateError, match="ros2 fairy reindex"):
            locate.resolve_archive(identifier)


# -- I5: RO-Crate identifiers --------------------------------------------------------

def test_odd_folder_names_give_valid_crate_ids(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    bag = paths.bags_dir() / "rosbag2_0"
    odd = bag.with_name("my run #2 100%")
    bag.rename(odd)
    harvest["bags"][0]["path"] = str(odd)
    crate = assembler.assemble(builder.build(harvest, context), harvest)
    graph = json.loads((crate / "ro-crate-metadata.json").read_text())["@graph"]
    ids = {e["@id"] for e in graph}
    assert "bags/my%20run%20%232%20100%25/" in ids
    assert not any("#2" in i or " " in i for i in ids if i.startswith("bags/"))
    checks = verify.verify_archive(crate)
    assert any(c["title"] == "All files referenced by the crate are present"
               for c in checks)
    assert not any(c["status"] == "fail" for c in checks)


def test_local_id():
    assert ro_crate._local_id("bags/a b/c#d.mcap") == "bags/a%20b/c%23d.mcap"
    assert ro_crate._local_id("harvest/harvest.json") == "harvest/harvest.json"
