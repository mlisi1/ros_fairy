import sqlite3

import pytest
import yaml

from ros_fairy.utils import bag_repair, topic_health
from tests.conftest import make_bag

T0 = 1_750_000_000.0


def _bad_clock_topics():
    # 30 near-epoch stamps + 11 real ones: <50% plausible -> unrecoverable clock
    return {"/data": [float(i) for i in range(1, 31)],
            "/fix": [T0 + i * 0.5 for i in range(11)]}


def _rows(db_file):
    con = sqlite3.connect(db_file)
    try:
        return con.execute(
            "SELECT id, topic_id, timestamp, data FROM messages "
            "ORDER BY id").fetchall()
    finally:
        con.close()


def test_restamp_sqlite_produces_playable_bag(tmp_path):
    src = make_bag(tmp_path / "bad", _bad_clock_topics())
    assert bag_repair.needs_repair(src) is True
    dest = tmp_path / "fixed"
    summary = bag_repair.restamp_bag(src, dest, duration_s=10)

    assert summary["messages"] == 41
    assert bag_repair.needs_repair(dest) is False
    meta = topic_health.parse_bag_metadata(dest)
    assert meta["storage_identifier"] == "sqlite3"
    assert meta["message_count"] == 41
    series = topic_health.read_clean_series(dest, meta)
    _s, _e, dur = topic_health.bag_timing(dest, meta, series)
    assert 9 < dur < 11
    # Same rows, topics and payloads, in arrival order, now evenly spaced.
    before, after = _rows(src / "bad_0.db3"), _rows(dest / "bad_0.db3")
    assert [(i, t, d) for i, t, _s, d in before] == \
        [(i, t, d) for i, t, _s, d in after]
    stamps = [s for _i, _t, s, _d in after]
    assert stamps == sorted(stamps) and len(set(stamps)) == 41
    # The original is untouched.
    assert _rows(src / "bad_0.db3") == before


def test_restamp_sqlite_keeps_splits_in_order(tmp_path):
    src = make_bag(tmp_path / "src", {"/a": [float(i) for i in range(20)]})
    first = src / "src_0.db3"
    second = src / "src_1.db3"
    second.write_bytes(first.read_bytes())
    meta = yaml.safe_load((src / "metadata.yaml").read_text())
    meta["rosbag2_bagfile_information"]["relative_file_paths"] = \
        ["src_0.db3", "src_1.db3"]
    (src / "metadata.yaml").write_text(yaml.safe_dump(meta))

    dest = tmp_path / "fixed"
    summary = bag_repair.restamp_bag(src, dest, duration_s=4)
    assert summary["messages"] == 40
    last_of_first = _rows(dest / "src_0.db3")[-1][2]
    first_of_second = _rows(dest / "src_1.db3")[0][2]
    assert first_of_second > last_of_first
    info = yaml.safe_load((dest / "metadata.yaml").read_text())[
        "rosbag2_bagfile_information"]
    assert info["relative_file_paths"] == ["src_0.db3", "src_1.db3"]
    assert [f["message_count"] for f in info["files"]] == [20, 20]
    assert info["files"][1]["starting_time"]["nanoseconds_since_epoch"] == \
        first_of_second


def test_restamp_sqlite_updates_embedded_metadata(tmp_path):
    # rosbag2 from Iron on keeps a copy of the metadata inside the .db3.
    src = make_bag(tmp_path / "bad", _bad_clock_topics())
    con = sqlite3.connect(src / "bad_0.db3")
    con.execute("CREATE TABLE metadata (id INTEGER PRIMARY KEY, "
                "metadata_version INTEGER, metadata TEXT)")
    con.execute("INSERT INTO metadata VALUES (1, 9, 'starting_time: old')")
    con.commit()
    con.close()

    dest = tmp_path / "fixed"
    bag_repair.restamp_bag(src, dest)
    con = sqlite3.connect(dest / "bad_0.db3")
    version, text = con.execute(
        "SELECT metadata_version, metadata FROM metadata").fetchone()
    con.close()
    stored = yaml.safe_load(text)
    assert version == 9
    assert stored["message_count"] == 41
    assert stored["relative_file_paths"] == ["bad_0.db3"]


def test_restamp_rejects_file_compressed_bag(tmp_path):
    src = make_bag(tmp_path / "bad", _bad_clock_topics())
    db = src / "bad_0.db3"
    db.rename(src / "bad_0.db3.zstd")
    meta = yaml.safe_load((src / "metadata.yaml").read_text())
    meta["rosbag2_bagfile_information"]["relative_file_paths"] = \
        ["bad_0.db3.zstd"]
    (src / "metadata.yaml").write_text(yaml.safe_dump(meta))
    with pytest.raises(bag_repair.BagRepairError, match="compressed"):
        bag_repair.restamp_bag(src, tmp_path / "fixed")
    assert not (tmp_path / "fixed").exists()


def test_restamp_rejects_unknown_storage(tmp_path):
    src = make_bag(tmp_path / "odd", {"/data": [T0, T0 + 1]})
    meta = yaml.safe_load((src / "metadata.yaml").read_text())
    meta["rosbag2_bagfile_information"]["storage_identifier"] = "custom"
    (src / "metadata.yaml").write_text(yaml.safe_dump(meta))
    with pytest.raises(bag_repair.BagRepairError,
                       match="only MCAP and sqlite3"):
        bag_repair.restamp_bag(src, tmp_path / "out")


def test_failed_sqlite_repair_leaves_nothing(tmp_path, monkeypatch):
    src = make_bag(tmp_path / "bad", _bad_clock_topics())

    def broken(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(bag_repair, "_write_repaired_metadata", broken)
    dest = tmp_path / "fixed"
    with pytest.raises(OSError):
        bag_repair.restamp_bag(src, dest)
    assert not dest.exists()
