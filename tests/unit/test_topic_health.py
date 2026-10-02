import importlib.util

import pytest

from ros_fairy.utils import topic_health
from tests.conftest import make_bag, make_mcap_bag

_MCAP_PRESENT = importlib.util.find_spec("mcap") is not None

SENSORS = [
    {"sensor_id": "gps0", "type": "gps", "make_model": "u-blox ZED-F9P",
     "topic": "/fix"},
    {"sensor_id": "sonar0", "type": "sonar", "make_model": "Ping2",
     "topic": "/depth"},
]

T0 = 1_750_000_000.0


def _steady(start, end, hz):
    n = int((end - start) * hz)
    return [start + i / hz for i in range(n + 1)]


def test_healthy_bag(tmp_path):
    bag = make_bag(tmp_path / "bag", {
        "/fix": _steady(T0, T0 + 60, 10),
        "/depth": _steady(T0, T0 + 60, 5),
    })
    assert topic_health.analyse_bag(bag, SENSORS) == []


def test_gap_detection_plain_text(tmp_path):
    # GPS silent from t=120 to t=360 (4 minutes), recording is 12+ min long
    stamps = _steady(T0, T0 + 120, 10) + _steady(T0 + 360, T0 + 720, 10)
    bag = make_bag(tmp_path / "bag", {
        "/fix": stamps,
        "/depth": _steady(T0, T0 + 720, 5),
    })
    warnings = topic_health.analyse_bag(bag, SENSORS)
    assert len(warnings) == 1
    w = warnings[0]
    assert w["kind"] == "gap"
    assert w["sensor_id"] == "gps0"
    assert 239 < w["duration_s"] < 241
    assert w["plain_text"] == \
        "GPS (gps0) signal was lost for 4 minutes, starting 2 minutes in."


def test_slow_topic_not_flagged(tmp_path):
    # 0.2 Hz topic: 5 s intervals exceed 1 s but are its normal cadence
    bag = make_bag(tmp_path / "bag", {
        "/diagnostics": _steady(T0, T0 + 600, 0.2),
        "/fix": _steady(T0, T0 + 600, 10),
        "/depth": _steady(T0, T0 + 600, 5),
    })
    assert topic_health.analyse_bag(bag, SENSORS) == []


def test_never_published(tmp_path):
    bag = make_bag(tmp_path / "bag", {"/fix": _steady(T0, T0 + 60, 10)})
    warnings = topic_health.analyse_bag(bag, SENSORS)
    assert len(warnings) == 1
    w = warnings[0]
    assert w["kind"] == "never_published"
    assert w["sensor_id"] == "sonar0"
    assert "Sonar" in w["plain_text"]
    assert "no data at all" in w["plain_text"]
    assert w["plain_text"] == \
        "Sonar (sonar0) produced no data at all during this recording."


# Same make/model on purpose — a stereo pair or front/rear pair of identical
# cameras is the common case, and make/model alone couldn't disambiguate it;
# only sensor_id (chosen by the operator at setup) is guaranteed unique.
CAMERA_PAIR = [
    {"sensor_id": "cam0", "type": "camera",
     "make_model": "Intel Realsense D456", "topic": "/cam0/image_raw"},
    {"sensor_id": "cam1", "type": "camera",
     "make_model": "Intel Realsense D456", "topic": "/cam1/image_raw"},
]


def test_never_published_disambiguates_same_type_sensors(tmp_path):
    """Two declared cameras of the same make/model: a bare "Camera produced
    no data" warning can't tell the operator which one — name it by
    sensor_id, "like they were registered" (reported 2026-09-11: a real
    mission with two identical Realsense D456s where only the sensor_id
    could distinguish the working one from the dead one)."""
    bag = make_bag(tmp_path / "bag",
                   {"/cam0/image_raw": _steady(T0, T0 + 60, 10)})
    warnings = topic_health.analyse_bag(bag, CAMERA_PAIR)
    assert len(warnings) == 1
    assert warnings[0]["sensor_id"] == "cam1"
    assert warnings[0]["plain_text"] == (
        "Camera (cam1) produced no data at all during this recording.")


def test_event_topics_are_not_checked_for_dropouts(tmp_path):
    """/rosout only carries log lines; "/rosout dropped out 9 times" on every
    mission was pure noise (2026-10-02). A sensor's gap is still reported."""
    bursty = (_steady(T0, T0 + 5, 20) + _steady(T0 + 200, T0 + 205, 20)
              + _steady(T0 + 500, T0 + 505, 20))
    bag = make_bag(tmp_path / "bag", {
        "/rosout": bursty,
        "/parameter_events": bursty,
        "/controller_server/transition_event": bursty,
        "/fix": _steady(T0, T0 + 120, 10) + _steady(T0 + 360, T0 + 720, 10),
        "/depth": _steady(T0, T0 + 720, 5),
    })
    warnings = topic_health.analyse_bag(bag, SENSORS)
    assert [w["topic"] for w in warnings] == ["/fix"]


def test_is_event_topic():
    assert topic_health.is_event_topic("/rosout")
    assert topic_health.is_event_topic("/bt_navigator/transition_event")
    assert not topic_health.is_event_topic("/diagnostics")
    assert not topic_health.is_event_topic("/rosout_agg_custom")


def test_gap_warning_disambiguates_same_type_sensors(tmp_path):
    stamps = _steady(T0, T0 + 120, 10) + _steady(T0 + 360, T0 + 720, 10)
    bag = make_bag(tmp_path / "bag", {
        "/cam0/image_raw": stamps,
        "/cam1/image_raw": _steady(T0, T0 + 720, 10),
    })
    warnings = topic_health.analyse_bag(bag, CAMERA_PAIR)
    assert len(warnings) == 1
    assert warnings[0]["sensor_id"] == "cam0"
    assert "Camera (cam0)" in warnings[0]["plain_text"]


def test_never_published_falls_back_to_compressed_variant(tmp_path):
    """A camera registered on its raw topic but only recorded via
    image_transport's compressed sibling must not be reported as silent —
    it published, just not on the exact declared topic name (reported
    2026-09-11: a `ros2 bag record` that only captured .../compressed while
    the sensor was registered on the raw .../image_raw topic)."""
    bag = make_bag(tmp_path / "bag", {
        "/cam0/image_raw/compressed": _steady(T0, T0 + 60, 10),
    }, types={"/cam0/image_raw/compressed": "sensor_msgs/msg/CompressedImage"})
    warnings = topic_health.analyse_bag(bag, [CAMERA_PAIR[0]])
    assert len(warnings) == 1
    w = warnings[0]
    assert w["kind"] == "compressed_transport"
    assert w["sensor_id"] == "cam0"
    assert "compressed stream" in w["plain_text"]
    assert "no data at all" not in w["plain_text"]


def test_never_published_stands_when_compressed_variant_also_silent(tmp_path):
    """If neither the raw topic nor any compressed sibling has data, the
    sensor really is silent — the original warning must still fire."""
    bag = make_bag(tmp_path / "bag", {"/unrelated": _steady(T0, T0 + 60, 10)})
    warnings = topic_health.analyse_bag(bag, [CAMERA_PAIR[0]])
    assert len(warnings) == 1
    assert warnings[0]["kind"] == "never_published"


def test_compressed_variant_fallback_is_camera_only(tmp_path):
    """The image_transport convention doesn't apply to non-camera sensors —
    a coincidentally "/compressed"-suffixed topic must not silence a
    genuinely-missing GPS/lidar/etc. warning."""
    sensor = {"sensor_id": "gps0", "type": "gps",
             "make_model": "u-blox ZED-F9P", "topic": "/fix"}
    bag = make_bag(tmp_path / "bag",
                   {"/fix/compressed": _steady(T0, T0 + 60, 10)})
    warnings = topic_health.analyse_bag(bag, [sensor])
    assert len(warnings) == 1
    assert warnings[0]["kind"] == "never_published"


def test_trailing_gap(tmp_path):
    # depth stops 5 minutes before the end
    bag = make_bag(tmp_path / "bag", {
        "/fix": _steady(T0, T0 + 600, 10),
        "/depth": _steady(T0, T0 + 300, 5),
    })
    warnings = topic_health.analyse_bag(bag, SENSORS)
    assert len(warnings) == 1
    w = warnings[0]
    assert w["kind"] == "gap"
    assert w["sensor_id"] == "sonar0"
    assert "did not come back" in w["plain_text"]


def test_missing_metadata(tmp_path):
    bag = tmp_path / "bag"
    bag.mkdir()
    warnings = topic_health.analyse_bag(bag, SENSORS)
    assert warnings[0]["plain_text"] == \
        "The recording ended unexpectedly and may be incomplete."


def test_unknown_topic_friendly_name(tmp_path):
    stamps = _steady(T0, T0 + 100, 10) + _steady(T0 + 200, T0 + 300, 10)
    bag = make_bag(tmp_path / "bag", {
        "/mystery": stamps,
        "/fix": _steady(T0, T0 + 300, 10),
        "/depth": _steady(T0, T0 + 300, 5),
    })
    warnings = topic_health.analyse_bag(bag, SENSORS)
    assert len(warnings) == 1
    assert "(/mystery)" in warnings[0]["plain_text"]
    assert warnings[0]["sensor_id"] is None


def test_single_bad_timestamp_is_ignored(tmp_path):
    """One near-epoch message corrupts rosbag2's metadata (a ~1970 start and a
    decades-long duration), but the real window is recovered from the rest and
    no clock warning is raised."""
    bag = make_bag(tmp_path / "bag", {
        "/fix": [1.0] + _steady(T0, T0 + 60, 10),   # one bogus stamp
        "/depth": _steady(T0, T0 + 60, 5),
    })
    meta = topic_health.parse_bag_metadata(bag)
    assert meta["start_s"] < topic_health.EPOCH_FLOOR_S
    assert meta["duration_s"] > topic_health.MAX_PLAUSIBLE_DURATION_S

    series = topic_health.read_clean_series(bag, meta)
    start_s, end_s, dur = topic_health.bag_timing(bag, meta, series)
    assert start_s is not None and end_s is not None
    assert 59 < dur < 61
    assert topic_health.analyse_bag(bag, SENSORS) == []


def test_unreliable_clock_reported(tmp_path):
    """When most messages carry near-epoch timestamps the clock was broken for
    the whole run: report duration unknown instead of guessing a tiny window."""
    bad = [float(i) for i in range(1, 31)]      # 30 near-epoch stamps
    good = _steady(T0, T0 + 5, 2)               # 11 real stamps
    bag = make_bag(tmp_path / "bag", {"/data": bad + good})

    meta = topic_health.parse_bag_metadata(bag)
    series = topic_health.read_clean_series(bag, meta)
    assert topic_health.bag_timing(bag, meta, series) == (None, None, None)

    warnings = topic_health.analyse_bag(bag, sensors=[])
    assert len(warnings) == 1
    assert warnings[0]["kind"] == "unreliable_clock"
    assert "clock was not set correctly" in warnings[0]["plain_text"]


def test_humanize_duration():
    h = topic_health.humanize_duration
    assert h(1) == "1 second"
    assert h(45) == "45 seconds"
    assert h(243.2) == "4 minutes"
    assert h(3600) == "1 hour"
    assert h(5460) == "1h 31m"


def test_mcap_bag_metadata_checks_only(tmp_path):
    bag = make_bag(tmp_path / "bag", {
        "/fix": [T0, T0 + 100],  # would be a huge gap if analysed
        "/depth": _steady(T0, T0 + 100, 5),
    }, storage="mcap")
    assert topic_health.analyse_bag(bag, SENSORS) == []


@pytest.mark.skipif(not _MCAP_PRESENT, reason="mcap package not installed")
def test_mcap_bag_gap_detection_end_to_end(tmp_path):
    """With the mcap reader available, gap detection works on MCAP bags —
    the Jazzy-default case that previously produced no timestamp warnings."""
    bag = make_mcap_bag(tmp_path / "bag", {
        "/fix": [T0, T0 + 1, T0 + 2, T0 + 30, T0 + 31],  # ~28 s GPS dropout
        "/depth": _steady(T0, T0 + 31, 5),
    })
    warnings = topic_health.analyse_bag(bag, SENSORS)
    gaps = [w for w in warnings if w["kind"] == "gap"]
    assert any(w["topic"] == "/fix" for w in gaps)
    assert any("GPS" in w["plain_text"] for w in gaps)


def test_metadata_without_storage_id_infers_from_distro(tmp_path, monkeypatch):
    """A bag whose metadata omits storage_identifier falls back to the
    recording distro's default rather than blindly assuming sqlite3."""
    bag = make_bag(tmp_path / "bag", {"/fix": [T0, T0 + 1]})
    meta_path = bag / "metadata.yaml"
    lines = [ln for ln in meta_path.read_text().splitlines()
             if "storage_identifier" not in ln]
    meta_path.write_text("\n".join(lines))

    monkeypatch.setenv("ROS_DISTRO", "jazzy")
    assert topic_health.parse_bag_metadata(bag)["storage_identifier"] == "mcap"
    monkeypatch.setenv("ROS_DISTRO", "humble")
    assert topic_health.parse_bag_metadata(bag)["storage_identifier"] == \
        "sqlite3"


# --- field-noise heuristics (2026-07-03 open item: bursty/latched topics) ----

def _bursty(start, end, burst_every_s=2.0, burst_n=10, spacing=0.01):
    """RTK-corrections shape: tight bursts separated by multi-second rests."""
    stamps = []
    t = start
    while t < end:
        stamps.extend(t + i * spacing for i in range(burst_n))
        t += burst_every_s
    return stamps


def test_bursty_topic_produces_no_gap_noise(tmp_path):
    # /rtcm-style: median interval 10 ms, but 2 s rests between bursts. The
    # old median-only threshold warned on every rest; the p95 term must not.
    bag = make_bag(tmp_path / "bag", {
        "/rtcm": _bursty(T0, T0 + 60),
        "/fix": _steady(T0, T0 + 60, 10),
        "/depth": _steady(T0, T0 + 60, 5),
    })
    assert topic_health.analyse_bag(bag, SENSORS) == []


def test_latched_topic_produces_no_gap_noise(tmp_path):
    # /tf_static-style: two messages over the whole bag, not a stream.
    bag = make_bag(tmp_path / "bag", {
        "/tf_static": [T0, T0 + 0.1],
        "/camera/theora": [T0, T0 + 1.0, T0 + 40.0],
        "/fix": _steady(T0, T0 + 60, 10),
        "/depth": _steady(T0, T0 + 60, 5),
    })
    assert topic_health.analyse_bag(bag, SENSORS) == []


def test_sensor_outage_still_detected_alongside_bursts(tmp_path):
    # The noise heuristics must not blunt real sensor outages.
    stamps = _steady(T0, T0 + 20, 10) + _steady(T0 + 50, T0 + 60, 10)
    bag = make_bag(tmp_path / "bag", {
        "/rtcm": _bursty(T0, T0 + 60),
        "/fix": stamps,
        "/depth": _steady(T0, T0 + 60, 5),
    })
    warnings = topic_health.analyse_bag(bag, SENSORS)
    assert [w["topic"] for w in warnings] == ["/fix"]
    assert warnings[0]["kind"] == "gap"


def test_repeated_sensor_gaps_collapse_into_summary(tmp_path):
    # Five separate GPS outages -> one plain-language summary, not five lines.
    stamps = []
    for i in range(6):
        stamps.extend(_steady(T0 + i * 100, T0 + i * 100 + 40, 10))
    bag = make_bag(tmp_path / "bag", {
        "/fix": stamps,
        "/depth": _steady(T0, T0 + 540, 5),
    })
    warnings = topic_health.analyse_bag(bag, SENSORS)
    fix = [w for w in warnings if w["topic"] == "/fix"]
    assert len(fix) == 1
    assert "times" in fix[0]["plain_text"]
    assert fix[0]["duration_s"] > 200  # aggregate lost time, not one gap


def test_non_sensor_topic_gets_at_most_one_gap_line(tmp_path):
    # An incidental (non-sensor) topic with several real gaps: one line only.
    stamps = (_steady(T0, T0 + 10, 5) + _steady(T0 + 30, T0 + 40, 5)
              + _steady(T0 + 60, T0 + 70, 5))
    bag = make_bag(tmp_path / "bag", {
        "/diagnostics": stamps,
        "/fix": _steady(T0, T0 + 70, 10),
        "/depth": _steady(T0, T0 + 70, 5),
    })
    warnings = topic_health.analyse_bag(bag, SENSORS)
    diag = [w for w in warnings if w["topic"] == "/diagnostics"]
    assert len(diag) == 1
    assert "dropped out" in diag[0]["plain_text"]


# -- bag_fingerprint -----------------------------------------------------------

def _bag_dict(**overrides):
    base = {"size_bytes": 1_048_576, "message_count": 6001,
           "duration_s": 600.0,
           "topics": [{"name": "/fix", "message_count": 6001}]}
    base.update(overrides)
    return base


def test_bag_fingerprint_matches_for_identical_content():
    assert topic_health.bag_fingerprint(_bag_dict()) == \
        topic_health.bag_fingerprint(_bag_dict())


def test_bag_fingerprint_differs_on_message_count():
    a = topic_health.bag_fingerprint(_bag_dict())
    b = topic_health.bag_fingerprint(_bag_dict(message_count=6002))
    assert a != b


def test_bag_fingerprint_differs_on_topic_counts():
    a = topic_health.bag_fingerprint(_bag_dict())
    b = topic_health.bag_fingerprint(_bag_dict(
        topics=[{"name": "/fix", "message_count": 6000}]))
    assert a != b


def test_bag_fingerprint_ignores_topic_order():
    a = _bag_dict(topics=[{"name": "/fix", "message_count": 1},
                         {"name": "/depth", "message_count": 2}])
    b = _bag_dict(topics=[{"name": "/depth", "message_count": 2},
                         {"name": "/fix", "message_count": 1}])
    assert topic_health.bag_fingerprint(a) == topic_health.bag_fingerprint(b)


def test_bag_fingerprint_accepts_pydantic_bag_model():
    from ros_fairy.manifest.schema import Bag, BagTopic
    bag = Bag(path="bags/x", storage_format="sqlite3", size_bytes=1_048_576,
             duration_s=600.0, message_count=6001,
             topics=[BagTopic(name="/fix", type="t", message_count=6001)])
    assert topic_health.bag_fingerprint(bag) == \
        topic_health.bag_fingerprint(_bag_dict())
