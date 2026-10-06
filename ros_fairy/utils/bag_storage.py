"""Pluggable rosbag2 storage readers (distro-agnostic groundwork).

The core stays portable across ROS 2 distros. One place
that still couples to a backend is timestamp-level health analysis: it needs
each message's receive time, and where those live differs by storage format.
sqlite3 keeps them in a ``messages(timestamp, topic_id)`` table; MCAP keeps
them in binary records. This module hides that behind a single reader
interface so callers (``utils/topic_health``) never branch on format.

Bags can be huge (Jo's field missions are 50–190 GB), and the watchdog reads
them as root at finalise time, so the readers never hold message payloads:

- ``sqlite3`` (``.db3``): streams ``(topic, timestamp)`` rows in insertion
  order through a cursor.
- ``mcap`` (``.mcap``, Jazzy's default): uses the per-chunk message indexes
  when the file has them (no chunk is decompressed: a 48 GB bag in ~8 s), and
  otherwise scans the records reading only each message's 22-byte header and
  seeking past its payload (a 64 GB unchunked bag in ~9 s). A chunk without
  an index (a file cut off mid-write) is decompressed on its own. The
  library's ``iter_messages`` loaded a whole bag into memory (1.4 GB of RAM
  for a 1.3 GB bag).

Timestamps are kept in arrival (file) order, each with its global arrival
index, so a clock that stepped backwards mid-recording can still be seen.

Adding a backend: implement a reader with ``storage_id``, ``supported = True``
and ``read_timestamps``, then register an instance in ``_READERS``.
"""

import importlib.util
import io
import logging
import sqlite3
import struct
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

log = logging.getLogger("ros_fairy.utils.bag_storage")

_MCAP_MAGIC = b"\x89MCAP0\r\n"
_OP_FOOTER, _OP_SCHEMA, _OP_CHANNEL, _OP_MESSAGE, _OP_CHUNK, _OP_DATA_END = \
    0x02, 0x03, 0x04, 0x05, 0x06, 0x0F


def _mcap_available() -> bool:
    """True if the optional ``mcap`` package is importable."""
    return importlib.util.find_spec("mcap") is not None


class BagStorageUnsupported(Exception):
    """A reader exists for the format but cannot extract timestamps yet."""


@dataclass
class Timestamps:
    """Per-topic receive timestamps (seconds) in arrival order.

    ``order[topic][i]`` is the arrival index (0..total-1, across all topics)
    of ``stamps[topic][i]``. ``truncated`` means a storage file ended
    mid-record (cut off by a crash or an incomplete copy).
    """
    stamps: dict[str, array] = field(default_factory=dict)
    order: dict[str, array] = field(default_factory=dict)
    total: int = 0
    truncated: bool = False
    types: dict[str, str] = field(default_factory=dict)  # topic -> type

    def add(self, topic: str, seconds: float) -> None:
        if topic not in self.stamps:
            self.stamps[topic] = array("d")
            self.order[topic] = array("q")
        self.stamps[topic].append(seconds)
        self.order[topic].append(self.total)
        self.total += 1


def storage_files(bag_dir: Path, rel_paths: list[str],
                  suffix: str) -> list[Path]:
    """The bag's storage files with ``suffix``.

    ``relative_file_paths`` from metadata.yaml when they exist (older
    rosbag2 versions prefixed the bag folder's name; a moved bag may have
    lost them), otherwise every ``*<suffix>`` in the folder, in split order
    (``_2`` before ``_10``). A compressed file (``.mcap.zstd``) has no
    reader: it is reported, not silently skipped.
    """
    listed = []
    for rel in rel_paths:
        rel = str(rel)
        if not rel.endswith(suffix):
            continue
        for candidate in (bag_dir / rel, bag_dir / Path(rel).name):
            if candidate.is_file():
                listed.append(candidate)
                break
    if listed:
        return listed
    found = sorted(bag_dir.glob(f"*{suffix}"), key=split_index)
    if not found and any(bag_dir.glob(f"*{suffix}.*")):
        log.warning("%s holds only compressed %s files; their timing can't "
                    "be analysed", bag_dir.name, suffix)
    return found


def split_index(path: Path) -> tuple:
    """Sort key putting rosbag2 splits in recording order: name_2 < name_10."""
    stem = path.name.split(".")[0]
    base, _, num = stem.rpartition("_")
    return (base, int(num)) if num.isdigit() else (stem, -1)


@runtime_checkable
class BagStorageReader(Protocol):
    """Reads per-message receive timestamps out of one rosbag2 storage format."""

    storage_id: str
    supported: bool

    def read_timestamps(self, bag_dir: Path,
                        rel_paths: list[str]) -> Timestamps:
        ...


def _sorted_series(ts: Timestamps) -> dict[str, list[float]]:
    return {topic: sorted(stamps) for topic, stamps in ts.stamps.items()}


class SqliteReader:
    """rosbag2 sqlite3 storage: timestamps live in the ``messages`` table,
    whose row ids follow arrival order."""

    storage_id = "sqlite3"
    supported = True

    def read_timestamps(self, bag_dir: Path,
                        rel_paths: list[str]) -> Timestamps:
        ts = Timestamps()
        for db_file in storage_files(bag_dir, rel_paths, ".db3"):
            try:
                con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
                try:
                    rows = con.execute(
                        "SELECT id, name, type FROM topics").fetchall()
                    names = {i: n for i, n, _t in rows}
                    ts.types.update({n: t for _i, n, t in rows})
                    for topic_id, stamp in con.execute(
                            "SELECT topic_id, timestamp FROM messages "
                            "ORDER BY id"):
                        ts.add(names.get(topic_id, "?"), stamp / 1e9)
                finally:
                    con.close()
            except sqlite3.Error as exc:
                log.warning("could not read %s: %s", db_file.name, exc)
                ts.truncated = True
        return ts

    def topic_timestamps(self, bag_dir: Path,
                         rel_paths: list[str]) -> dict[str, list[float]]:
        """Topic -> ascending timestamps (the older interface)."""
        return _sorted_series(self.read_timestamps(bag_dir, rel_paths))


class McapReader:
    """rosbag2 MCAP storage (Jazzy's default). Needs only the ``mcap``
    package for the container format; messages are never deserialised."""

    storage_id = "mcap"
    supported = _mcap_available()

    def read_timestamps(self, bag_dir: Path,
                        rel_paths: list[str]) -> Timestamps:
        if not _mcap_available():  # pragma: no cover - exercised without mcap
            raise BagStorageUnsupported(
                "the 'mcap' package is required to analyse MCAP bags")
        ts = Timestamps()
        for mcap_file in storage_files(bag_dir, rel_paths, ".mcap"):
            try:
                if not _read_indexed(mcap_file, ts):
                    _scan(mcap_file, ts)
            except OSError as exc:
                log.warning("could not read %s: %s", mcap_file.name, exc)
                ts.truncated = True
        return ts

    def topic_timestamps(self, bag_dir: Path,
                         rel_paths: list[str]) -> dict[str, list[float]]:
        """Topic -> ascending timestamps (the older interface)."""
        return _sorted_series(self.read_timestamps(bag_dir, rel_paths))


def _read_indexed(path: Path, ts: Timestamps) -> bool:
    """Read timestamps from the chunk message indexes. False when the file
    has none (unchunked, or no summary because it was cut off)."""
    from mcap.exceptions import McapError
    from mcap.reader import make_reader
    from mcap.records import MessageIndex
    from mcap.stream_reader import StreamReader
    with open(path, "rb") as f:
        try:
            summary = make_reader(f).get_summary()
        except (McapError, struct.error, ValueError):
            return False
        if summary is None or not summary.chunk_indexes or not all(
                ci.message_index_offsets for ci in summary.chunk_indexes):
            return False
        topics = {cid: ch.topic for cid, ch in summary.channels.items()}
        for ch in summary.channels.values():
            schema = summary.schemas.get(ch.schema_id)
            ts.types.setdefault(ch.topic, schema.name if schema else "unknown")
        chunks = sorted(summary.chunk_indexes,
                        key=lambda ci: ci.chunk_start_offset)
        for ci in chunks:
            f.seek(min(ci.message_index_offsets.values()))
            data = f.read(ci.message_index_length)
            entries: list[tuple[int, int, str]] = []  # offset, time, topic
            try:
                for rec in StreamReader(io.BytesIO(data),
                                        skip_magic=True).records:
                    if isinstance(rec, MessageIndex):
                        topic = topics.get(rec.channel_id, "?")
                        entries.extend((off, t, topic)
                                       for t, off in rec.records)
            except McapError:
                pass  # end of this chunk's index block
            # Within a chunk, the record offset is arrival order.
            for _off, t, topic in sorted(entries):
                ts.add(topic, t / 1e9)
    return True


def _scan(path: Path, ts: Timestamps) -> None:
    """Walk the file's records, reading message headers only."""
    topics: dict[int, str] = {}
    schemas: dict[int, str] = {}
    with open(path, "rb") as f:
        if f.read(8) != _MCAP_MAGIC:
            log.warning("%s is not an MCAP file", path.name)
            ts.truncated = True
            return
        while True:
            head = f.read(9)
            if not head:
                return
            if len(head) < 9:
                ts.truncated = True
                return
            op, length = head[0], struct.unpack_from("<Q", head, 1)[0]
            if op == _OP_MESSAGE:
                h = f.read(22)
                if len(h) < 22:
                    ts.truncated = True
                    return
                cid, _seq, log_time = struct.unpack_from("<HIQ", h)
                ts.add(topics.get(cid, "?"), log_time / 1e9)
                f.seek(length - 22, 1)
            elif op == _OP_SCHEMA:
                body = f.read(length)
                if len(body) < length:
                    ts.truncated = True
                    return
                sid, n = struct.unpack_from("<HI", body)
                schemas[sid] = body[6:6 + n].decode("utf-8", "replace")
            elif op == _OP_CHANNEL:
                body = f.read(length)
                if len(body) < length:
                    ts.truncated = True
                    return
                cid, sid, n = struct.unpack_from("<HHI", body)
                topics[cid] = body[8:8 + n].decode("utf-8", "replace")
                ts.types.setdefault(topics[cid], schemas.get(sid, "unknown"))
            elif op == _OP_CHUNK:
                body = f.read(length)
                if len(body) < length:
                    ts.truncated = True
                    return
                _chunk(head + body, topics, schemas, ts)
            elif op in (_OP_DATA_END, _OP_FOOTER):
                return
            else:
                f.seek(length, 1)
                if f.tell() > path.stat().st_size:
                    ts.truncated = True
                    return


def _chunk(record: bytes, topics: dict[int, str], schemas: dict[int, str],
           ts: Timestamps) -> None:
    """One chunk without an index: decompress it alone (bounded memory)."""
    from mcap.exceptions import McapError
    from mcap.records import Channel, Message, Schema
    from mcap.stream_reader import StreamReader
    try:
        for rec in StreamReader(io.BytesIO(record), skip_magic=True).records:
            if isinstance(rec, Schema):
                schemas[rec.id] = rec.name
            elif isinstance(rec, Channel):
                topics[rec.id] = rec.topic
                ts.types.setdefault(rec.topic, schemas.get(rec.schema_id,
                                                           "unknown"))
            elif isinstance(rec, Message):
                ts.add(topics.get(rec.channel_id, "?"), rec.log_time / 1e9)
    except McapError:
        pass  # end of the chunk


def salvage_topics(bag_dir: Path) -> tuple[str, list[dict], int]:
    """(storage format, topics, message count) of a bag without
    metadata.yaml, read from the storage files themselves.

    A recorder killed mid-write leaves no metadata, but the storage still
    names its topics and holds the messages up to the cut: an MCAP is read
    record by record up to where it stops, a SQLite database as is.
    """
    if any(bag_dir.glob("*.mcap")):
        storage, reader = "mcap", _READERS["mcap"]
    elif any(bag_dir.glob("*.db3")):
        storage, reader = "sqlite3", _READERS["sqlite3"]
    else:
        return "unknown", [], 0
    if not reader.supported:
        return storage, [], 0
    ts = reader.read_timestamps(bag_dir, [])
    topics = [{"name": topic, "type": ts.types.get(topic, "unknown"),
               "message_count": len(stamps)}
              for topic, stamps in ts.stamps.items()]
    for topic, type_ in ts.types.items():  # declared, but nothing recorded
        if topic not in ts.stamps:
            topics.append({"name": topic, "type": type_, "message_count": 0})
    return storage, topics, ts.total


_READERS: dict[str, BagStorageReader] = {
    reader.storage_id: reader for reader in (SqliteReader(), McapReader())
}


def get_reader(storage_id: str) -> BagStorageReader | None:
    """Return a reader for ``storage_id``, or None for an unknown format."""
    return _READERS.get(storage_id)


def supports_timestamps(storage_id: str) -> bool:
    """True if per-message timestamp analysis is available for this format."""
    reader = _READERS.get(storage_id)
    return reader is not None and reader.supported
