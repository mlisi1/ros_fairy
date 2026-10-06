"""Bag analysis: FAILURE_CASES B1-B9 (readers, timing, health, repair)."""

import hashlib
import json
from pathlib import Path
from unittest import mock

import pytest
import yaml

from ros_fairy.utils import bag_repair, bag_storage, topic_health
from tests.conftest import make_bag

T0 = 1_790_000_000.0


def _mcap(bag_dir: Path, streams: dict[str, float], seconds: float, *,
          step_at: float | None = None, step: float = 0.0,
          chunked: bool = False, name: str | None = None,
          types: dict[str, str] | None = None,
          gaps: dict[str, tuple[float, float]] | None = None) -> Path:
    """An MCAP bag written in arrival order, as rosbag2 writes it.

    ``streams``: topic -> rate (Hz). ``step_at``/``step``: the clock jumps by
    ``step`` seconds at that time into the recording. ``gaps``: topic ->
    (from, to) seconds with no messages on it.
    """
    from mcap.writer import Writer
    bag_dir.mkdir(parents=True, exist_ok=True)
    name = name or f"{bag_dir.name}_0.mcap"
    events = []
    for topic, hz in streams.items():
        n = int(seconds * hz)
        for k in range(n):
            t = k / hz
            lo, hi = (gaps or {}).get(topic, (None, None))
            if lo is not None and lo <= t < hi:
                continue
            events.append((t, topic))
    events.sort()
    with open(bag_dir / name, "wb") as fh:
        w = Writer(fh, chunk_size=4096 if chunked else 1 << 30,
                   use_chunking=chunked)
        w.start()
        ids = {}
        for topic in streams:
            sid = w.register_schema((types or {}).get(topic, "std_msgs/msg/"
                                                      "String"), "ros2msg", b"")
            ids[topic] = w.register_channel(topic, "cdr", sid)
        count = 0
        for t, topic in events:
            stamp = T0 + t + (step if step_at is not None and t >= step_at
                              else 0.0)
            ns = int(stamp * 1e9)
            w.add_message(ids[topic], log_time=ns, publish_time=ns, data=b"x")
            count += 1
        w.finish()
    info = {"storage_identifier": "mcap", "relative_file_paths": [name],
            "starting_time": {"nanoseconds_since_epoch": int(T0 * 1e9)},
            "duration": {"nanoseconds": int(seconds * 1e9)},
            "message_count": count,
            "topics_with_message_count": [
                {"topic_metadata": {"name": t, "type": "std_msgs/msg/String"},
                 "message_count": sum(1 for _, x in events if x == t)}
                for t in streams]}
    (bag_dir / "metadata.yaml").write_text(
        yaml.safe_dump({"rosbag2_bagfile_information": info}))
    return bag_dir


STREAMS = {"/imu/data": 50.0, "/gnss": 5.0, "/scan": 10.0}


# -- B6: readers that never hold the bag ------------------------------------------

@pytest.mark.parametrize("chunked", [False, True])
def test_mcap_reader_streams_in_arrival_order(tmp_path, chunked):
    bag = _mcap(tmp_path / "b", STREAMS, 30, chunked=chunked)
    with mock.patch("mcap.reader.SeekingReader.iter_messages",
                    side_effect=AssertionError("loads the whole bag")):
        ts = bag_storage.McapReader().read_timestamps(bag, [])
    assert ts.total == 30 * 65 and not ts.truncated
    merged = sorted((o, t) for topic in ts.stamps
                    for o, t in zip(ts.order[topic], ts.stamps[topic]))
    assert [o for o, _ in merged] == list(range(ts.total))
    assert all(a <= b for (_, a), (_, b) in zip(merged, merged[1:]))
    assert ts.types["/imu/data"] == "std_msgs/msg/String"


def test_indexed_path_is_used_for_chunked_files(tmp_path):
    bag = _mcap(tmp_path / "b", STREAMS, 10, chunked=True)
    ts = bag_storage.Timestamps()
    assert bag_storage._read_indexed(next(bag.glob("*.mcap")), ts) is True
    assert ts.total == 650
    unchunked = _mcap(tmp_path / "u", STREAMS, 10)
    assert bag_storage._read_indexed(next(unchunked.glob("*.mcap")),
                                     bag_storage.Timestamps()) is False


def test_sqlite_reader_keeps_arrival_order_and_types(tmp_path):
    bag = make_bag(tmp_path / "s", {"/a": [T0, T0 + 2], "/b": [T0 + 1]})
    ts = bag_storage.SqliteReader().read_timestamps(bag, [])
    assert ts.total == 3 and set(ts.types) == {"/a", "/b"}


def test_truncated_mcap_is_not_a_broken_clock(tmp_path):
    """B6: a file cut off at half its length still has a good clock."""
    bag = _mcap(tmp_path / "b", STREAMS, 60)
    f = next(bag.glob("*.mcap"))
    data = f.read_bytes()
    f.write_bytes(data[: len(data) // 3])  # metadata still counts them all
    meta = topic_health.parse_bag_metadata(bag)
    series = topic_health.read_clean_series(bag, meta)
    assert series.truncated and series.total_read < meta["message_count"] / 2
    start, end, dur = topic_health.bag_timing(bag, meta, series)
    assert dur is not None and 10 < dur < 25
    kinds = [w["kind"] for w in topic_health.analyse_bag(bag, [], meta=meta)]
    assert "truncated" in kinds and "unreliable_clock" not in kinds


# -- B2: clock steps ----------------------------------------------------------------

@pytest.mark.parametrize("step", [3 * 3600.0, -90.0])
def test_clock_step_is_found_and_corrected(tmp_path, step):
    bag = _mcap(tmp_path / "b", STREAMS, 120, step_at=60, step=step)
    meta = topic_health.parse_bag_metadata(bag)
    series = topic_health.read_clean_series(bag, meta)
    [(offset, delta)] = series.steps
    assert abs(offset - 60) < 1 and abs(delta - step) < 1
    _s, _e, dur = topic_health.bag_timing(bag, meta, series)
    assert abs(dur - 120) < 1  # not 3 hours longer, nor 90 s shorter
    warnings = topic_health.analyse_bag(bag, [], meta=meta, series=series)
    assert [w["kind"] for w in warnings] == ["clock_step"]  # no gap noise
    word = "forward" if step > 0 else "back"
    assert f"clock jumped {word}" in warnings[0]["plain_text"]


def test_one_sensor_outage_is_a_gap_not_a_clock_step(tmp_path):
    bag = _mcap(tmp_path / "b", STREAMS, 300, gaps={"/gnss": (60, 200)})
    meta = topic_health.parse_bag_metadata(bag)
    series = topic_health.read_clean_series(bag, meta)
    assert series.steps == []
    warnings = topic_health.analyse_bag(bag, [], meta=meta, series=series)
    assert [(w["kind"], w["topic"]) for w in warnings] == [("gap", "/gnss")]


def test_bag_written_topic_by_topic_has_no_clock_step(tmp_path):
    """Converted/merged bags hold each topic as a block: the arrival order
    goes back between blocks, but no topic does."""
    bag = make_bag(tmp_path / "b", {
        "/a": [T0 + i for i in range(100)],
        "/b": [T0 + i for i in range(100)]})
    meta = topic_health.parse_bag_metadata(bag)
    assert topic_health.read_clean_series(bag, meta).steps == []


# -- B3: a clock that was never set -------------------------------------------------

def test_all_stamps_near_the_epoch_is_flagged(tmp_path):
    bag = make_bag(tmp_path / "b", {"/a": [float(i) for i in range(1, 50)]})
    meta = topic_health.parse_bag_metadata(bag)
    series = topic_health.read_clean_series(bag, meta)
    assert series == {} and series.total_read == 49
    assert topic_health.bag_timing(bag, meta, series) == (None, None, None)
    kinds = [w["kind"] for w in topic_health.analyse_bag(bag, [], meta=meta)]
    assert "unreliable_clock" in kinds
    assert bag_repair.needs_repair(bag) is True


# -- B1: a stream that starts late --------------------------------------------------

def test_leading_gap_is_reported(tmp_path):
    bag = _mcap(tmp_path / "b", STREAMS, 300, gaps={"/scan": (0, 120)})
    warnings = topic_health.analyse_bag(bag, [{"sensor_id": "lidar0",
                                               "type": "lidar",
                                               "topic": "/scan"}])
    [w] = warnings
    assert w["topic"] == "/scan" and w["start_offset_s"] == 0.0
    assert "only started sending data 2 minutes into the recording" in \
        w["plain_text"]


# -- B4: on-demand topics -----------------------------------------------------------

@pytest.mark.parametrize("topic", [
    "/cmd_vel", "/jo/cmd_vel", "/cmd_vel_nav", "/joy", "/goal_pose", "/plan",
    "/local_plan", "/transformed_global_plan", "/behavior_tree_log",
    "/navigate_to_pose/_action/feedback", "/x/_action/status", "/tf_static"])
def test_on_demand_topics_are_exempt(topic):
    assert topic_health.is_event_topic(topic)


@pytest.mark.parametrize("topic", ["/imu/data", "/scan", "/odom",
                                   "/cmd_vel_raw_stats", "/planner_stats"])
def test_streams_are_still_checked(topic):
    assert not topic_health.is_event_topic(topic)


def test_a_declared_sensor_on_an_event_name_is_still_checked(tmp_path):
    bag = _mcap(tmp_path / "b", {"/cmd_vel": 20.0, "/imu/data": 50.0}, 300,
                gaps={"/cmd_vel": (60, 200)})
    sensor = {"sensor_id": "drive", "type": "other", "topic": "/cmd_vel"}
    assert [w["topic"] for w in topic_health.analyse_bag(bag, [sensor])] == \
        ["/cmd_vel"]
    assert topic_health.analyse_bag(bag, []) == []


# -- B5: metadata problems ----------------------------------------------------------

@pytest.mark.parametrize("info", [
    None, "text", {"starting_time": None, "duration": None},
    {"topics_with_message_count": [None, {"topic_metadata": None}],
     "message_count": "many", "relative_file_paths": None}])
def test_odd_metadata_does_not_raise(tmp_path, info):
    (tmp_path / "metadata.yaml").write_text(
        yaml.safe_dump({"rosbag2_bagfile_information": info}))
    meta = topic_health.parse_bag_metadata(tmp_path)
    assert meta is None or meta["message_count"] == 0


def test_bag_without_metadata_is_timed_from_its_storage(fairy_dirs, tmp_path):
    from ros_fairy.watchdog import watchdog as wd
    bag = _mcap(tmp_path / "killed", STREAMS, 90)
    (bag / "metadata.yaml").unlink()  # recorder killed before closing
    rec = wd._bag_record(bag, "detected", [])
    assert rec["storage_format"] == "mcap"
    assert rec["message_count"] == 90 * 65
    assert abs(rec["duration_s"] - 90) < 1
    assert {t["name"] for t in rec["topics"]} == set(STREAMS)
    assert rec["health_warnings"][0]["plain_text"].startswith(
        "The recording ended unexpectedly")


# -- B7: finding the storage files --------------------------------------------------

def test_storage_files_handles_layouts(tmp_path):
    bag = tmp_path / "bag"
    bag.mkdir()
    for n in (10, 2, 0):
        (bag / f"bag_{n}.mcap").write_bytes(b"")
    # an older rosbag2 prefixed the folder name; a moved bag's names differ
    assert bag_storage.storage_files(bag, ["bag/bag_2.mcap"], ".mcap") == \
        [bag / "bag_2.mcap"]
    assert [p.name for p in bag_storage.storage_files(
        bag, ["elsewhere_0.mcap"], ".mcap")] == \
        ["bag_0.mcap", "bag_2.mcap", "bag_10.mcap"]
    zipped = tmp_path / "zipped"
    zipped.mkdir()
    (zipped / "zipped_0.mcap.zstd").write_bytes(b"")
    messages = []
    with mock.patch.object(bag_storage.log, "warning",
                           side_effect=lambda *a: messages.append(a[0] % a[1:])):
        assert bag_storage.storage_files(zipped, [], ".mcap") == []
    assert "compressed" in messages[0]


# -- B8: fingerprints of empty recordings --------------------------------------------

def _bag(count, start, topics=(("/a", 0),)):
    return {"size_bytes": 100, "message_count": count, "duration_s": None,
            "start_time": start,
            "topics": [{"name": n, "message_count": c} for n, c in topics]}


def test_empty_recordings_are_told_apart():
    fp = topic_health.bag_fingerprint
    assert fp(_bag(0, "2026-10-06T08:00:00+00:00")) != \
        fp(_bag(0, "2026-10-06T09:00:00+00:00"))
    assert fp(_bag(0, "2026-10-06T08:00:00+00:00")) == \
        fp(_bag(0, "2026-10-06T08:00:00+00:00"))  # a retry still matches


def test_non_empty_fingerprint_is_unchanged():
    """Fingerprints already in the index must keep matching."""
    bag = {"size_bytes": 5, "message_count": 3, "duration_s": 1.23456,
           "start_time": "2026-10-06T08:00:00+00:00",
           "topics": [{"name": "/a", "message_count": 3}]}
    old = json.dumps([5, 3, 1.235, [["/a", 3]]], sort_keys=True)
    assert topic_health.bag_fingerprint(bag) == \
        hashlib.sha256(old.encode()).hexdigest()


# -- B9: repair ---------------------------------------------------------------------

def _epoch_split(bag_dir, name, topic_types, n=20, offset=0):
    """A split of a bag recorded with an unset clock."""
    from mcap.writer import Writer
    with open(bag_dir / name, "wb") as fh:
        w = Writer(fh)
        w.start()
        for k in range(n):
            for topic, (type_, defn) in topic_types.items():
                sid = w.register_schema(type_, "ros2msg", defn)
                cid = w.register_channel(topic, "cdr", sid)
                w.add_message(cid, log_time=(offset + k) * 10**9,
                              publish_time=0, data=f"{name}:{k}".encode())
        w.finish()


def test_repair_keeps_split_order_and_each_schema(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _epoch_split(src, "src_2.mcap", {"/a": ("pkg/msg/A", b"int32 x")},
                 offset=100)
    _epoch_split(src, "src_10.mcap", {"/a": ("pkg/msg/B", b"float64 y")},
                 offset=200)
    _epoch_split(src, "src_0.mcap", {"/a": ("pkg/msg/A", b"int32 x")})
    (src / "metadata.yaml").write_text(yaml.safe_dump(
        {"rosbag2_bagfile_information": {
            "storage_identifier": "mcap", "relative_file_paths": [],
            "starting_time": {"nanoseconds_since_epoch": 0},
            "duration": {"nanoseconds": 0}, "message_count": 60,
            "topics_with_message_count": []}}))
    dest = tmp_path / "fixed"
    bag_repair.restamp_bag(src, dest)
    from mcap.reader import make_reader
    with open(dest / "fixed_0.mcap", "rb") as fh:
        rows = [(m.data.decode().split(":")[0], s.name)
                for s, _c, m in make_reader(fh).iter_messages()]
    assert [r[0] for r in rows[::20]] == ["src_0.mcap", "src_2.mcap",
                                          "src_10.mcap"]
    assert {r for r in rows if r[0] == "src_10.mcap"} == \
        {("src_10.mcap", "pkg/msg/B")}  # its own type, not the first one


def test_failed_repair_leaves_nothing(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    _epoch_split(src, "src_0.mcap", {"/a": ("pkg/msg/A", b"")})
    (src / "metadata.yaml").write_text(yaml.safe_dump(
        {"rosbag2_bagfile_information": {
            "storage_identifier": "mcap", "relative_file_paths": [],
            "message_count": 20}}))

    def broken(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(bag_repair, "_write_repaired_metadata", broken)
    dest = tmp_path / "fixed"
    with pytest.raises(OSError):
        bag_repair.restamp_bag(src, dest)
    assert not dest.exists()
