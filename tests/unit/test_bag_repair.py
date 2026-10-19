import importlib.util
import io
from types import SimpleNamespace

import pytest
import yaml
from rich.console import Console

from fairy.subcommands import repair
from fairy.utils import bag_repair, topic_health
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


def _snapshot(bag_dir):
    return {p.name: p.read_bytes() for p in bag_dir.iterdir()}


@pytest.mark.parametrize("where", ["same", "inside", "parent"])
def test_restamp_never_writes_over_the_source(tmp_path, where):
    src = make_mcap_bag(tmp_path / "run1", _bad_clock_topics())
    before = _snapshot(src)
    dest = {"same": tmp_path / "." / "run1", "inside": src / "fixed",
            "parent": tmp_path}[where]
    with pytest.raises(bag_repair.BagRepairError, match="over the original"):
        bag_repair.restamp_bag(src, dest)
    assert _snapshot(src) == before


def test_repair_command_refuses_output_over_the_original(tmp_path):
    # `ros2 fairy repair ./run1 -o . --force` aims the copy at run1 itself.
    src = make_mcap_bag(tmp_path / "run1", _bad_clock_topics())
    before = _snapshot(src)
    console = Console(file=io.StringIO(), width=200, force_terminal=False)
    args = SimpleNamespace(mission=str(src), output=str(tmp_path), all=True,
                           duration=None, force=True, json=False)
    assert repair.run(args, console=console) == 1
    assert "would overwrite the original" in console.file.getvalue()
    assert _snapshot(src) == before


@pytest.mark.parametrize("keep", [1 / 3, 1 / 2, 5 / 6])
def test_restamp_cut_off_mcap_is_a_plain_error(tmp_path, keep):
    src = make_mcap_bag(tmp_path / "bad", _bad_clock_topics())
    f = next(src.glob("*.mcap"))
    data = f.read_bytes()
    f.write_bytes(data[:int(len(data) * keep)])
    before = _snapshot(src)
    dest = tmp_path / "fixed"
    with pytest.raises(bag_repair.BagRepairError, match="damaged"):
        bag_repair.restamp_bag(src, dest)
    assert not dest.exists()
    assert _snapshot(src) == before
