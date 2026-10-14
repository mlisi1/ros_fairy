import importlib.util

import pytest
import yaml

from ros_fairy.utils import bag_repair, topic_health
from tests.conftest import make_mcap_bag

_MCAP_PRESENT = importlib.util.find_spec("mcap") is not None
pytestmark = pytest.mark.skipif(not _MCAP_PRESENT,
                                reason="mcap package not installed")

T0 = 1_750_000_000.0


def _bad_clock_topics():
    # 30 near-epoch stamps + 11 real ones: <50% plausible -> unrecoverable clock
    return {"/data": [float(i) for i in range(1, 31)]
            + [T0 + i * 0.5 for i in range(11)]}


def test_needs_repair_detects_bad_clock(tmp_path):
    bad = make_mcap_bag(tmp_path / "bad", _bad_clock_topics())
    good = make_mcap_bag(tmp_path / "good",
                         {"/data": [T0 + i * 0.1 for i in range(50)]})
    assert bag_repair.needs_repair(bad) is True
    assert bag_repair.needs_repair(good) is False


def test_restamp_produces_playable_bag(tmp_path):
    src = make_mcap_bag(tmp_path / "bad", _bad_clock_topics())
    dest = tmp_path / "fixed"
    summary = bag_repair.restamp_bag(src, dest, duration_s=10)

    assert summary["messages"] == 41
    assert (dest / "metadata.yaml").is_file()
    assert list(dest.glob("*.mcap"))
    # the repaired bag now has a usable, monotonic clock
    assert bag_repair.needs_repair(dest) is False
    meta = topic_health.parse_bag_metadata(dest)
    series = topic_health.read_clean_series(dest, meta)
    _s, _e, dur = topic_health.bag_timing(dest, meta, series)
    assert 9 < dur < 11
    # message count and topic preserved in regenerated metadata
    assert meta["message_count"] == 41
    assert any(t["name"] == "/data" for t in meta["topics"])
    # original is untouched
    assert bag_repair.needs_repair(src) is True


def test_restamp_keeps_per_message_compression(tmp_path):
    # Payloads are copied as they are, so a bag compressed per message must
    # still say so; one compressed per file can't be read at all.
    src = make_mcap_bag(tmp_path / "bad", _bad_clock_topics())
    meta = yaml.safe_load((src / "metadata.yaml").read_text())
    meta["rosbag2_bagfile_information"].update(
        compression_format="zstd", compression_mode="message")
    (src / "metadata.yaml").write_text(yaml.safe_dump(meta))
    bag_repair.restamp_bag(src, tmp_path / "fixed")
    info = yaml.safe_load((tmp_path / "fixed" / "metadata.yaml").read_text())[
        "rosbag2_bagfile_information"]
    assert (info["compression_format"], info["compression_mode"]) == \
        ("zstd", "message")
