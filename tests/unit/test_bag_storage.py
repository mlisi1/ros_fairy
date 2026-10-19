import importlib.util
import io
import struct
from pathlib import Path

import pytest

from fairy.utils import bag_storage
from tests.conftest import make_bag, make_mcap_bag

T0 = 1_750_000_000.0

_MCAP_PRESENT = importlib.util.find_spec("mcap") is not None


def test_get_reader_dispatch():
    assert isinstance(bag_storage.get_reader("sqlite3"),
                      bag_storage.SqliteReader)
    assert isinstance(bag_storage.get_reader("mcap"), bag_storage.McapReader)
    assert bag_storage.get_reader("rosbag_v9000") is None


def test_supports_timestamps():
    assert bag_storage.supports_timestamps("sqlite3") is True
    # MCAP support tracks whether the optional mcap package is installed.
    assert bag_storage.supports_timestamps("mcap") is _MCAP_PRESENT
    assert bag_storage.supports_timestamps("unknown") is False


def test_sqlite_reader_returns_sorted_series(tmp_path):
    bag = make_bag(tmp_path / "bag", {
        "/fix": [T0 + 2, T0, T0 + 1],   # deliberately out of order
        "/depth": [T0, T0 + 0.5],
    })
    series = bag_storage.SqliteReader().topic_timestamps(
        bag, [f"{bag.name}_0.db3"])
    assert series["/fix"] == [T0, T0 + 1, T0 + 2]
    assert series["/depth"] == [T0, T0 + 0.5]


def test_sqlite_reader_falls_back_to_glob(tmp_path):
    """A bag whose metadata omits relative_file_paths still resolves .db3."""
    bag = make_bag(tmp_path / "bag", {"/fix": [T0, T0 + 1]})
    series = bag_storage.SqliteReader().topic_timestamps(bag, [])
    assert series["/fix"] == [T0, T0 + 1]


@pytest.mark.skipif(not _MCAP_PRESENT, reason="mcap package not installed")
def test_mcap_reader_returns_sorted_series(tmp_path):
    bag = make_mcap_bag(tmp_path / "bag", {
        "/fix": [T0 + 2, T0, T0 + 1],   # writer preserves insertion order
        "/depth": [T0, T0 + 0.5],
    })
    series = bag_storage.McapReader().topic_timestamps(
        bag, [f"{bag.name}_0.mcap"])
    assert series["/fix"] == [T0, T0 + 1, T0 + 2]
    assert series["/depth"] == [T0, T0 + 0.5]


@pytest.mark.skipif(not _MCAP_PRESENT, reason="mcap package not installed")
def test_mcap_reader_falls_back_to_glob(tmp_path):
    bag = make_mcap_bag(tmp_path / "bag", {"/fix": [T0, T0 + 1]})
    series = bag_storage.McapReader().topic_timestamps(bag, [])
    assert series["/fix"] == [T0, T0 + 1]


@pytest.mark.skipif(not _MCAP_PRESENT, reason="mcap package not installed")
def test_mcap_reader_skips_corrupt_file(tmp_path):
    bag = tmp_path / "bag"
    bag.mkdir()
    (bag / "bag_0.mcap").write_bytes(b"not a real mcap file")
    assert bag_storage.McapReader().topic_timestamps(bag, []) == {}


def _unchunked_mcap(n: int = 5) -> tuple[bytes, int]:
    """(whole file, offset where the last message record ends)."""
    from mcap.writer import Writer
    buf = io.BytesIO()
    writer = Writer(buf, use_chunking=False)
    writer.start()
    sid = writer.register_schema(name="x", encoding="ros2msg", data=b"")
    cid = writer.register_channel(topic="/a", message_encoding="cdr",
                                  schema_id=sid)
    for i in range(n):
        writer.add_message(channel_id=cid, log_time=int((T0 + i) * 1e9),
                           publish_time=0, data=b"y" * 100)
    end = buf.tell()
    writer.finish()
    return buf.getvalue(), end


@pytest.mark.skipif(not _MCAP_PRESENT, reason="mcap package not installed")
@pytest.mark.parametrize("cut", [
    "boundary", "payload", "zero_tail", "huge_length"])
def test_scan_flags_every_kind_of_cut_off_file(tmp_path, cut):
    data, end = _unchunked_mcap()
    tails = {
        "boundary": data[:end],                    # no DataEnd/Footer
        "payload": data[:end - 50],                # last message cut
        "zero_tail": data[:end] + bytes(9 * 100),  # preallocated, unwritten
        "huge_length": data[:end] + b"\x03" + struct.pack("<Q", 2 ** 40)
        + bytes(30),                               # garbage, no MemoryError
    }
    path = tmp_path / "bag_0.mcap"
    path.write_bytes(tails[cut])
    ts = bag_storage.Timestamps()
    bag_storage._scan(path, ts)
    assert ts.truncated
    assert ts.total == (4 if cut == "payload" else 5)


@pytest.mark.skipif(not _MCAP_PRESENT, reason="mcap package not installed")
def test_scan_complete_files_are_not_truncated(tmp_path):
    data, _end = _unchunked_mcap()
    path = tmp_path / "bag_0.mcap"
    path.write_bytes(data)
    fixture = Path(__file__).parents[1] / "fixtures" / "real_mcap"
    for f in (path, fixture / "real_mcap_0.mcap"):
        ts = bag_storage.Timestamps()
        bag_storage._scan(f, ts)
        assert not ts.truncated and ts.total == 5
    ts = bag_storage.McapReader().read_timestamps(fixture, [])
    assert not ts.truncated and ts.total == 5
