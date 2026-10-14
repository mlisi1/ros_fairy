"""Repair a bag (MCAP or sqlite3) whose recording clock was broken.

When the system clock was unsynchronised during recording, most messages are
stamped near the epoch (1970) and a few at the real time, so ``ros2 bag play``
honours that 1970↔now timeline and stalls. This rewrites a **new** bag (the
original is never touched) with a dense, monotonic synthetic clock, and
regenerates ``metadata.yaml`` so the result is immediately playable — no manual
``ros2 bag reindex`` step.

The new timing is synthetic: messages keep their original order, types and bytes,
but inter-message spacing is fabricated (spread evenly over the chosen duration).
Good for inspection/playback; not for time-critical processing. The only real fix
is to sync the clock before recording.

Shared by ``ros2 fairy repair`` and ``tools/restamp_bag.py``.
"""

import copy
import sqlite3
import time
from pathlib import Path
from typing import Any

import yaml

from ros_fairy.utils import bag_storage, topic_health

DEFAULT_DURATION_S = 60.0


class BagRepairError(Exception):
    """Plain-language reason a bag can't be repaired."""


def needs_repair(bag_dir: Path) -> bool:
    """True if the bag's clock is unreliable (its real timing is
    unrecoverable) — including a bag whose every stamp is near the epoch."""
    meta = topic_health.parse_bag_metadata(bag_dir)
    if meta is None:
        return False
    series = topic_health.read_clean_series(bag_dir, meta)
    if series is None:
        return False  # no reader: can't tell
    _start, _end, duration = topic_health.bag_timing(bag_dir, meta, series)
    return duration is None


def _raw_metadata(bag_dir: Path) -> dict[str, Any]:
    raw = yaml.safe_load((bag_dir / "metadata.yaml").read_text())
    return raw["rosbag2_bagfile_information"]


def _span_ns(total: int, good: list[int], duration_s: float | None) -> int:
    """How long the repaired recording lasts: ``duration_s`` if given, else
    the span of the few real stamps, else DEFAULT_DURATION_S."""
    if duration_s is not None:
        return int(duration_s * 1e9)
    if len(good) >= 2:
        return max(good) - min(good)
    return int(DEFAULT_DURATION_S * 1e9)


def restamp_bag(src_bag_dir: Path, dest_bag_dir: Path,
                duration_s: float | None = None) -> dict[str, Any]:
    """Write a re-stamped, playable copy of a bag into ``dest_bag_dir``.

    Returns a summary dict. Raises BagRepairError for a storage format that
    can't be repaired (anything but MCAP and sqlite3, or a file-compressed
    bag) or when the ``mcap`` package is needed but unavailable.
    """
    meta = topic_health.parse_bag_metadata(src_bag_dir)
    if meta is None:
        raise BagRepairError(f"{src_bag_dir.name} has no readable metadata")
    storage = meta["storage_identifier"]
    if storage == "sqlite3":
        return _restamp_sqlite(src_bag_dir, dest_bag_dir, meta, duration_s)
    if storage != "mcap":
        raise BagRepairError(
            f"only MCAP and sqlite3 bags can be repaired "
            f"({src_bag_dir.name} is {storage})")
    return _restamp_mcap(src_bag_dir, dest_bag_dir, meta, duration_s)


def _no_files_error(src_bag_dir: Path, suffix: str) -> BagRepairError:
    if any(src_bag_dir.glob(f"*{suffix}.*")):
        return BagRepairError(
            f"{src_bag_dir.name} is stored compressed ({suffix} files "
            "compressed as a whole); decompress it first, e.g. with "
            "`ros2 bag convert`")
    return BagRepairError(f"no {suffix} data found in {src_bag_dir.name}")


def _restamp_mcap(src_bag_dir: Path, dest_bag_dir: Path,
                  meta: dict[str, Any],
                  duration_s: float | None) -> dict[str, Any]:
    if not bag_storage.supports_timestamps("mcap"):
        raise BagRepairError("the 'mcap' package is required to repair bags")
    try:
        from mcap.reader import make_reader
        from mcap.writer import Writer
    except ImportError as exc:  # pragma: no cover - guarded above
        raise BagRepairError("the 'mcap' package is required to repair "
                             "bags") from exc

    # Splits in recording order (name_2 before name_10).
    src_files = bag_storage.storage_files(
        src_bag_dir, meta["relative_file_paths"], ".mcap")
    if not src_files:
        raise _no_files_error(src_bag_dir, ".mcap")

    # Count messages first, and note any real-stamp span to size the default.
    total, good = 0, []
    floor = topic_health.EPOCH_FLOOR_S * 1e9
    for f in src_files:
        with open(f, "rb") as h:
            for _s, _c, m in make_reader(h).iter_messages(log_time_order=False):
                total += 1
                if m.log_time >= floor:
                    good.append(m.log_time)
    if total == 0:
        raise BagRepairError(f"{src_bag_dir.name} contains no messages")

    step = max(1, _span_ns(total, good, duration_s) // total)
    base = time.time_ns()  # a plausible recent epoch so the bag also looks sane

    created_dir = not dest_bag_dir.exists()
    dest_bag_dir.mkdir(parents=True, exist_ok=True)
    out_name = f"{dest_bag_dir.name}_0.mcap"
    out_path = dest_bag_dir / out_name
    meta_path = dest_bag_dir / "metadata.yaml"

    # A schema is its name *and* definition, a channel its topic *and* type
    # and encoding: a topic recorded with two message types (or one type
    # whose definition changed between splits) keeps each one's own schema.
    schema_map: dict[tuple, int] = {}
    channel_map: dict[tuple, int] = {}
    i = 0
    try:
        with open(out_path, "wb") as g:
            writer = Writer(g)
            writer.start()
            for f in src_files:
                with open(f, "rb") as h:
                    for schema, channel, message in \
                            make_reader(h).iter_messages(log_time_order=False):
                        skey = (schema.name, schema.encoding, schema.data) \
                            if schema else None
                        if skey is not None and skey not in schema_map:
                            schema_map[skey] = writer.register_schema(
                                name=schema.name, encoding=schema.encoding,
                                data=schema.data)
                        ckey = (channel.topic, channel.message_encoding, skey)
                        if ckey not in channel_map:
                            channel_map[ckey] = writer.register_channel(
                                topic=channel.topic,
                                message_encoding=channel.message_encoding,
                                schema_id=schema_map.get(skey, 0)
                                if skey else 0,
                                metadata=dict(channel.metadata))
                        ts = base + i * step
                        writer.add_message(
                            channel_id=channel_map[ckey],
                            log_time=ts, publish_time=ts,
                            sequence=message.sequence, data=message.data)
                        i += 1
            writer.finish()
        new_span = (total - 1) * step
        _write_repaired_metadata(src_bag_dir, dest_bag_dir, "mcap",
                                 [(out_name, base, new_span, total)])
    except BaseException:
        # Leave nothing that looks like a repaired bag: no data file without
        # metadata, no half-written metadata, no folder we made for nothing.
        out_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        if created_dir:
            try:
                dest_bag_dir.rmdir()
            except OSError:
                pass
        raise
    return {
        "source": str(src_bag_dir),
        "dest": str(dest_bag_dir),
        "messages": total,
        "new_duration_s": round(new_span / 1e9, 3),
    }


def _sqlite_stats(db_file: Path) -> tuple[int, list[int]]:
    """(message count, [first, last] real stamp or fewer) of one .db3."""
    floor = int(topic_health.EPOCH_FLOOR_S * 1e9)
    con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    try:
        total = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        lo, hi = con.execute(
            "SELECT MIN(timestamp), MAX(timestamp) FROM messages "
            "WHERE timestamp >= ?", (floor,)).fetchone()
    finally:
        con.close()
    return total, [t for t in (lo, hi) if t is not None]


def _restamp_sqlite(src_bag_dir: Path, dest_bag_dir: Path,
                    meta: dict[str, Any],
                    duration_s: float | None) -> dict[str, Any]:
    """Copy each split with SQLite's backup API, then rewrite its
    ``messages.timestamp`` column in row-id (arrival) order. Topics, payloads
    and row ids are kept, and the splits stay separate files."""
    src_files = bag_storage.storage_files(
        src_bag_dir, meta["relative_file_paths"], ".db3")
    if not src_files:
        raise _no_files_error(src_bag_dir, ".db3")
    try:
        stats = [_sqlite_stats(f) for f in src_files]
    except sqlite3.Error as exc:
        raise BagRepairError(
            f"{src_bag_dir.name} can't be read as sqlite3: {exc}") from exc
    total = sum(n for n, _ in stats)
    if total == 0:
        raise BagRepairError(f"{src_bag_dir.name} contains no messages")
    good = [t for _, ends in stats for t in ends]
    step = max(1, _span_ns(total, good, duration_s) // total)
    base = time.time_ns()

    created_dir = not dest_bag_dir.exists()
    dest_bag_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    files: list[tuple[str, int, int, int]] = []
    meta_path = dest_bag_dir / "metadata.yaml"
    i = 0
    try:
        for src, (count, _) in zip(src_files, stats, strict=True):
            out = dest_bag_dir / src.name
            written.append(out)
            source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
            con = sqlite3.connect(out)
            try:
                source.backup(con)
                with con:
                    con.execute("CREATE TEMP TABLE arrival "
                                "(id INTEGER PRIMARY KEY, n INTEGER)")
                    con.execute("INSERT INTO arrival SELECT id, ROW_NUMBER() "
                                "OVER (ORDER BY id) - 1 FROM messages")
                    con.execute(
                        "UPDATE messages SET timestamp = ? + (? + (SELECT n "
                        "FROM arrival WHERE arrival.id = messages.id)) * ?",
                        (base, i, step))
            finally:
                con.close()
                source.close()
            files.append((out.name, base + i * step,
                          max(0, count - 1) * step, count))
            i += count
        new_span = (total - 1) * step
        info = _write_repaired_metadata(src_bag_dir, dest_bag_dir, "sqlite3",
                                        files)
        for out in written:
            _update_stored_metadata(out, info)
    except BaseException:
        for path in [*written, meta_path]:
            path.unlink(missing_ok=True)
            for sidecar in ("-wal", "-shm", "-journal"):
                Path(f"{path}{sidecar}").unlink(missing_ok=True)
        if created_dir:
            try:
                dest_bag_dir.rmdir()
            except OSError:
                pass
        raise
    return {
        "source": str(src_bag_dir),
        "dest": str(dest_bag_dir),
        "messages": total,
        "new_duration_s": round(new_span / 1e9, 3),
    }


def _update_stored_metadata(db_file: Path, info: dict[str, Any]) -> None:
    """rosbag2 from Iron on also keeps the bag's metadata inside each .db3
    (the ``metadata`` table, read when metadata.yaml is lost). Give it the
    new timing too, so the copy inside doesn't contradict the file next
    to it."""
    con = sqlite3.connect(db_file)
    try:
        has_table = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'metadata'").fetchone()
        if has_table:
            with con:
                con.execute("UPDATE metadata SET metadata = ?",
                            (yaml.safe_dump(info, default_flow_style=False,
                                            sort_keys=False),))
    finally:
        con.close()


def _write_repaired_metadata(src_bag_dir: Path, dest_bag_dir: Path,
                             storage: str,
                             files: list[tuple[str, int, int, int]]
                             ) -> dict[str, Any]:
    """Reuse the source metadata (keeps topic types/QoS), fix only timing and
    the storage files so the repaired bag is immediately playable.

    ``files`` holds (name, start ns, span ns, message count) per storage
    file, in order. Per-message compression is kept (the payloads are copied
    as they are); the files themselves are never compressed.
    """
    info = copy.deepcopy(_raw_metadata(src_bag_dir))
    start = files[0][1]
    end = max(s + d for _n, s, d, _c in files)
    info["storage_identifier"] = storage
    info["relative_file_paths"] = [name for name, *_ in files]
    info["starting_time"] = {"nanoseconds_since_epoch": start}
    info["duration"] = {"nanoseconds": end - start}
    info["message_count"] = sum(c for *_, c in files)
    if info.get("compression_mode") != "message":
        info["compression_format"] = ""
        info["compression_mode"] = ""
    info["files"] = [{
        "path": name,
        "starting_time": {"nanoseconds_since_epoch": file_start},
        "duration": {"nanoseconds": span},
        "message_count": count,
    } for name, file_start, span, count in files]
    (dest_bag_dir / "metadata.yaml").write_text(
        yaml.safe_dump({"rosbag2_bagfile_information": info},
                       default_flow_style=False, sort_keys=False))
    return info
