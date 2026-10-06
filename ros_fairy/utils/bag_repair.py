"""Repair an MCAP bag whose recording clock was broken.

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


def restamp_bag(src_bag_dir: Path, dest_bag_dir: Path,
                duration_s: float | None = None) -> dict[str, Any]:
    """Write a re-stamped, playable copy of an MCAP bag into ``dest_bag_dir``.

    Returns a summary dict. Raises BagRepairError for non-MCAP bags or when the
    ``mcap`` package is unavailable.
    """
    if not bag_storage.supports_timestamps("mcap"):
        raise BagRepairError("the 'mcap' package is required to repair bags")
    try:
        from mcap.reader import make_reader
        from mcap.writer import Writer
    except ImportError as exc:  # pragma: no cover - guarded above
        raise BagRepairError("the 'mcap' package is required to repair "
                             "bags") from exc

    meta = topic_health.parse_bag_metadata(src_bag_dir)
    if meta is None:
        raise BagRepairError(f"{src_bag_dir.name} has no readable metadata")
    if meta["storage_identifier"] != "mcap":
        raise BagRepairError(
            f"only MCAP bags can be repaired ({src_bag_dir.name} is "
            f"{meta['storage_identifier']})")

    # Splits in recording order (name_2 before name_10).
    src_files = bag_storage.storage_files(
        src_bag_dir, meta["relative_file_paths"], ".mcap")
    if not src_files:
        raise BagRepairError(f"no .mcap data found in {src_bag_dir.name}")

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

    if duration_s is not None:
        span_ns = int(duration_s * 1e9)
    elif len(good) >= 2:
        span_ns = max(good) - min(good)
    else:
        span_ns = int(DEFAULT_DURATION_S * 1e9)
    step = max(1, span_ns // total)
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
        _write_repaired_metadata(src_bag_dir, dest_bag_dir, out_name, base,
                                 new_span, total)
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


def _write_repaired_metadata(src_bag_dir: Path, dest_bag_dir: Path,
                             out_name: str, base_ns: int, span_ns: int,
                             count: int) -> None:
    """Reuse the source metadata (keeps topic types/QoS), fix only timing and
    the storage file so the repaired bag is immediately playable."""
    info = copy.deepcopy(_raw_metadata(src_bag_dir))
    info["storage_identifier"] = "mcap"
    info["relative_file_paths"] = [out_name]
    info["starting_time"] = {"nanoseconds_since_epoch": base_ns}
    info["duration"] = {"nanoseconds": span_ns}
    info["message_count"] = count
    info["compression_format"] = ""
    info["compression_mode"] = ""
    info["files"] = [{
        "path": out_name,
        "starting_time": {"nanoseconds_since_epoch": base_ns},
        "duration": {"nanoseconds": span_ns},
        "message_count": count,
    }]
    (dest_bag_dir / "metadata.yaml").write_text(
        yaml.safe_dump({"rosbag2_bagfile_information": info},
                       default_flow_style=False, sort_keys=False))
