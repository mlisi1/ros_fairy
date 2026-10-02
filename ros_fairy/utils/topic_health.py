"""Topic health analysis for recorded bags.

Produces the HealthWarning dicts. Every warning
carries a pre-rendered ``plain_text`` sentence; UI layers must display that
string and never the raw numbers.

Timestamp-level gap detection (gaps, low-rate) needs each message's receive
time; reading those out of a bag is delegated to ``utils/bag_storage`` so this
module never branches on storage format. Both sqlite3 and MCAP (Jazzy's
default) are supported; formats without a reader degrade to metadata-level
checks only (never_published). See ``utils/bag_storage`` for the extension
point.

The metadata header's ``starting_time``/``duration`` cannot be trusted blindly:
rosbag2 derives them from the minimum message timestamp, so a single message
stamped near the epoch (an un-stamped or latched sample) reports a 1970 start
and a duration of decades. ``bag_timing`` recovers the real span from the message
timestamps (and reports it unknown when the clock was broken for most of the
run), while ``read_clean_series`` drops those outliers before gap detection.
"""

import hashlib
import json
import statistics
from pathlib import Path
from typing import Any

import yaml

from ros_fairy.utils import bag_storage, ros_distro

GAP_THRESHOLD_S = 1.0
# A gap must also dwarf the topic's own cadence, or slow topics (0.2 Hz
# diagnostics etc.) would warn on every message.
GAP_MEDIAN_FACTOR = 5.0
# ...and dwarf the topic's own *pauses*: bursty topics (RTK corrections on
# /rtcm) have a tiny median but routinely rest between bursts, so the p95
# interval is the honest cadence to compare against (2026-07-03 field test).
# Only meaningful with a decent sample, and computed with the single largest
# interval excluded — that one may be the very outage being tested for.
GAP_P95_FACTOR = 2.0
GAP_P95_MIN_INTERVALS = 20
# A topic with only a handful of messages over the whole bag (latched
# /tf_static, one-shot codec headers) is not a periodic stream; unless it
# belongs to a declared sensor, gap analysis on it is noise.
GAP_MIN_MESSAGES = 10
# More gap warnings than this on one topic collapse into a single summary
# line, so one flaky channel cannot drown the mission_close review. Declared
# sensors get itemised gaps up to the cap; other topics get at most one line.
GAP_COLLAPSE_AFTER = 3
GAP_COLLAPSE_AFTER_OTHER = 1
LOW_RATE_FRACTION = 0.25
LOW_RATE_WINDOW_S = 10.0
LOW_RATE_MIN_MESSAGES = 20

# Timestamps before this (2000-01-01 UTC) cannot belong to a real field
# recording. rosbag2 sets metadata starting_time to the minimum message
# timestamp, so one such message drags the reported start back to ~1970 and
# inflates duration to decades; we drop these outliers and recompute the window.
EPOCH_FLOOR_S = 946684800.0
# A single recording longer than this is implausible; a header claiming more
# means starting_time is corrupt.
MAX_PLAUSIBLE_DURATION_S = 30 * 24 * 3600.0
# If fewer than this fraction of messages carry a plausible timestamp, the
# recording clock was broken for most of the run and the real window cannot be
# recovered from the surviving stamps.
RELIABLE_STAMP_FRACTION = 0.5

_FRIENDLY_TYPE = {
    "gps": "GPS",
    "lidar": "Lidar",
    "camera": "Camera",
    "imu": "Motion sensor (IMU)",
    "sonar": "Sonar",
}

# Standard image_transport plugin suffixes: a camera registered on its raw
# topic ("camera/image_raw") but recorded through one of these siblings
# ("camera/image_raw/compressed") did publish — just not on the exact topic
# name declared. Checked only for "camera" sensors; the convention doesn't
# apply to the other sensor types.
IMAGE_TRANSPORT_SUFFIXES = ("compressed", "compressedDepth", "theora")

# HealthWarning kinds that describe something worth knowing but not actually
# wrong — never counted as a "sensor produced no data" quality hit
# (manifest/quality.py) and rendered as a plain note, not a "⚠" warning
# (ui/review.py).
INFO_KINDS = frozenset({"compressed_transport"})


# Topics that only carry messages when something happens (a log line, a
# parameter change, a lifecycle transition). Gaps and low rates on them are
# meaningless — "/rosout dropped out 9 times" was pure noise (2026-10-02).
EVENT_TOPICS = frozenset({"/rosout", "/parameter_events"})
EVENT_TOPIC_SUFFIXES = ("/transition_event",)


def is_event_topic(topic: str) -> bool:
    return topic in EVENT_TOPICS or topic.endswith(EVENT_TOPIC_SUFFIXES)


def humanize_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        n = max(1, round(seconds))
        return f"{n} second{'s' if n != 1 else ''}"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, minutes = divmod(minutes, 60)
    if minutes == 0:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{hours}h {minutes}m"


def parse_bag_metadata(bag_dir: Path) -> dict[str, Any] | None:
    """Parse rosbag2's metadata.yaml. None if absent/unreadable."""
    meta_path = bag_dir / "metadata.yaml"
    if not meta_path.is_file():
        return None
    try:
        raw = yaml.safe_load(meta_path.read_text())
        info = raw["rosbag2_bagfile_information"]
    except (yaml.YAMLError, KeyError, TypeError):
        return None
    start_ns = info.get("starting_time", {}).get("nanoseconds_since_epoch", 0)
    duration_ns = info.get("duration", {}).get("nanoseconds", 0)
    topics = []
    for entry in info.get("topics_with_message_count") or []:
        tm = entry.get("topic_metadata") or {}
        topics.append({
            "name": tm.get("name", ""),
            "type": tm.get("type", ""),
            "message_count": entry.get("message_count", 0),
        })
    return {
        # rosbag2 normally records the format; when it is absent (old or
        # hand-rolled bags) infer the recording distro's default rather than
        # blindly assuming sqlite3.
        "storage_identifier": info.get("storage_identifier")
        or ros_distro.default_storage(),
        "start_s": start_ns / 1e9,
        "duration_s": duration_ns / 1e9,
        "message_count": info.get("message_count", 0),
        "topics": topics,
        "relative_file_paths": info.get("relative_file_paths") or [],
    }


def _friendly_name(sensor: dict | None, topic: str) -> str:
    """Plain-language name for a warning's subject.

    Always includes the operator's own ``sensor_id`` (the label they gave it
    at setup, e.g. "cam0") alongside the type/make — the type alone
    ("Camera") is ambiguous the moment a robot has two of the same type, and
    even the make/model can't disambiguate two identical units (a stereo
    pair, front/rear cameras). sensor_id is the one thing guaranteed unique
    per sensor, and it's how the operator already thinks of it.
    """
    if sensor is not None:
        label = _FRIENDLY_TYPE.get(sensor.get("type", ""),
                                   sensor.get("make_model") or "A sensor")
        sensor_id = sensor.get("sensor_id")
        return f"{label} ({sensor_id})" if sensor_id else label
    return f"One of the recorded data channels ({topic})"


def _signal_word(sensor: dict | None) -> str:
    return "signal" if sensor and sensor.get("type") == "gps" else "data"


def _compressed_variant(topic: str, recorded: dict[str, int]) -> str | None:
    """The image_transport sibling of ``topic`` that actually has messages,
    if any — e.g. ``<topic>/compressed`` when ``<topic>`` itself has none."""
    for suffix in IMAGE_TRANSPORT_SUFFIXES:
        candidate = f"{topic}/{suffix}"
        if recorded.get(candidate, 0) > 0:
            return candidate
    return None


def _gap_warnings(topic: str, sensor: dict | None, stamps: list[float],
                  bag_start: float, bag_end: float) -> list[dict]:
    if len(stamps) < 2:
        return []
    if sensor is None and len(stamps) < GAP_MIN_MESSAGES:
        return []  # latched / one-shot topic, not a stream worth gap analysis
    intervals = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    median = statistics.median(intervals)
    burst = 0.0
    if len(intervals) >= GAP_P95_MIN_INTERVALS:
        usual = sorted(intervals)[:-1]
        burst = GAP_P95_FACTOR * statistics.quantiles(usual, n=20)[-1]
    threshold = max(GAP_THRESHOLD_S, GAP_MEDIAN_FACTOR * median, burst)
    who = _friendly_name(sensor, topic)
    what = _signal_word(sensor)
    warnings = []
    gap_durations: list[float] = []
    for prev, cur in zip(stamps, stamps[1:], strict=False):
        gap = cur - prev
        if gap <= threshold:
            continue
        offset = prev - bag_start
        gap_durations.append(gap)
        warnings.append({
            "topic": topic,
            "sensor_id": sensor.get("sensor_id") if sensor else None,
            "kind": "gap",
            "start_offset_s": round(offset, 3),
            "duration_s": round(gap, 3),
            "plain_text": (
                f"{who} {what} was lost for {humanize_duration(gap)}, "
                f"starting {humanize_duration(offset)} in."),
        })
    trailing = bag_end - stamps[-1]
    if trailing > threshold:
        gap_durations.append(trailing)
        warnings.append({
            "topic": topic,
            "sensor_id": sensor.get("sensor_id") if sensor else None,
            "kind": "gap",
            "start_offset_s": round(stamps[-1] - bag_start, 3),
            "duration_s": round(trailing, 3),
            "plain_text": (
                f"{who} {what} stopped {humanize_duration(trailing)} before "
                f"the end of the recording and did not come back."),
        })
    cap = GAP_COLLAPSE_AFTER if sensor is not None else GAP_COLLAPSE_AFTER_OTHER
    if len(warnings) > cap:
        total = sum(gap_durations)
        warnings = [{
            "topic": topic,
            "sensor_id": sensor.get("sensor_id") if sensor else None,
            "kind": "gap",
            "start_offset_s": warnings[0]["start_offset_s"],
            "duration_s": round(total, 3),
            "plain_text": (
                f"{who} {what} dropped out {len(warnings)} times, for "
                f"{humanize_duration(total)} in total."),
        }]
    return warnings


def _low_rate_warning(topic: str, sensor: dict | None, stamps: list[float],
                      duration_s: float) -> dict | None:
    if len(stamps) < LOW_RATE_MIN_MESSAGES or duration_s <= LOW_RATE_WINDOW_S:
        return None
    windows: dict[int, int] = {}
    t0 = stamps[0]
    for ts in stamps:
        windows[int((ts - t0) / LOW_RATE_WINDOW_S)] = \
            windows.get(int((ts - t0) / LOW_RATE_WINDOW_S), 0) + 1
    peak_rate = max(windows.values()) / LOW_RATE_WINDOW_S
    avg_rate = len(stamps) / duration_s
    if peak_rate <= 0 or avg_rate >= LOW_RATE_FRACTION * peak_rate:
        return None
    return {
        "topic": topic,
        "sensor_id": sensor.get("sensor_id") if sensor else None,
        "kind": "low_rate",
        "start_offset_s": None,
        "duration_s": None,
        "plain_text": (
            f"{_friendly_name(sensor, topic)} sent data much more slowly "
            f"than usual for most of the recording."),
    }


def read_clean_series(bag_dir: Path,
                      meta: dict[str, Any]) -> dict[str, list[float]] | None:
    """Per-topic ascending message timestamps (seconds), with outliers removed.

    Returns None when no supported storage reader exists (timestamp-level work
    is impossible); an empty dict when a reader ran but found no plausible
    timestamps. Stamps before ``EPOCH_FLOOR_S`` are dropped so one un-stamped
    message cannot poison gap detection or the recording window.
    """
    reader = bag_storage.get_reader(meta["storage_identifier"])
    if reader is None or not reader.supported:
        return None
    try:
        series = reader.topic_timestamps(bag_dir, meta["relative_file_paths"])
    except bag_storage.BagStorageUnsupported:
        return None
    cleaned: dict[str, list[float]] = {}
    for topic, stamps in series.items():
        good = [s for s in stamps if s >= EPOCH_FLOOR_S]
        if good:
            cleaned[topic] = good
    return cleaned


def _plausible_window(start_s: float, duration_s: float) -> bool:
    return (start_s >= EPOCH_FLOOR_S
            and 0.0 <= duration_s <= MAX_PLAUSIBLE_DURATION_S)


def bag_timing(bag_dir: Path, meta: dict[str, Any],
               series: dict[str, list[float]] | None
               ) -> tuple[float | None, float | None, float | None]:
    """Best estimate of ``(start_s, end_s, duration_s)`` for the bag.

    Returns ``(None, None, None)`` when the recording clock was too unreliable
    to recover the real window. That happens when most messages carry a
    near-epoch timestamp (an unsynced system clock that jumped mid-recording):
    the surviving real stamps cover only a sliver of the run, so any duration or
    rate derived from them would be badly wrong. Better to report nothing.

    Otherwise prefers the span of real message timestamps, falling back to the
    metadata header when it is plausible and to the storage files' modification
    times when it is corrupt and no per-message timestamps are available.
    """
    if series:
        plausible = sum(len(stamps) for stamps in series.values())
        total = meta.get("message_count") or plausible
        if total > 0 and plausible / total < RELIABLE_STAMP_FRACTION:
            return None, None, None
        lo = min(stamps[0] for stamps in series.values())
        hi = max(stamps[-1] for stamps in series.values())
        return lo, hi, max(0.0, hi - lo)
    start_s, duration_s = meta["start_s"], meta["duration_s"]
    if _plausible_window(start_s, duration_s):
        return start_s, start_s + duration_s, duration_s
    mtimes = [f.stat().st_mtime for f in bag_dir.rglob("*") if f.is_file()]
    if mtimes and max(mtimes) - min(mtimes) > 0:
        return min(mtimes), max(mtimes), max(mtimes) - min(mtimes)
    return None, None, None


def _clock_unreliable_warning() -> dict:
    return {
        "topic": "",
        "sensor_id": None,
        "kind": "unreliable_clock",
        "start_offset_s": None,
        "duration_s": None,
        "plain_text": (
            "The recording device's clock was not set correctly, so most data "
            "is time-stamped incorrectly. The length of the recording and the "
            "data rates could not be measured, and playback timing may be off."),
    }


def analyse_bag(bag_dir: Path, sensors: list[dict] | None = None, *,
                meta: dict[str, Any] | None = None,
                series: dict[str, list[float]] | None = None) -> list[dict]:
    """Return HealthWarning dicts for one bag directory.

    ``sensors`` is the declared sensor list from robot_identity (may be
    empty/None when the robot was never set up). ``meta`` and ``series`` let a
    caller that already read them (the watchdog reads both once at finalise)
    avoid a second full pass over the bag.
    """
    sensors = sensors or []
    by_topic = {s["topic"]: s for s in sensors}
    if meta is None:
        meta = parse_bag_metadata(bag_dir)
    warnings: list[dict] = []

    if meta is None:
        return [{
            "topic": "", "sensor_id": None, "kind": "never_published",
            "start_offset_s": None, "duration_s": None,
            "plain_text": "The recording ended unexpectedly and may be "
                          "incomplete.",
        }]

    recorded = {t["name"]: t["message_count"] for t in meta["topics"]}
    for sensor in sensors:
        if recorded.get(sensor["topic"], 0) > 0:
            continue
        who = _friendly_name(sensor, sensor["topic"])
        compressed = (_compressed_variant(sensor["topic"], recorded)
                     if sensor.get("type") == "camera" else None)
        if compressed is not None:
            warnings.append({
                "topic": compressed,
                "sensor_id": sensor["sensor_id"],
                "kind": "compressed_transport",
                "start_offset_s": None,
                "duration_s": None,
                "plain_text": f"{who} was recorded through its compressed "
                              "stream instead of the raw one.",
            })
            continue
        warnings.append({
            "topic": sensor["topic"],
            "sensor_id": sensor["sensor_id"],
            "kind": "never_published",
            "start_offset_s": None,
            "duration_s": None,
            "plain_text": f"{who} produced no data at all during this "
                          "recording.",
        })

    if series is None:
        series = read_clean_series(bag_dir, meta)
    if not series:
        # No supported reader (or no plausible timestamps): the metadata-level
        # checks above are all we can offer.
        return warnings

    bag_start, bag_end, duration_s = bag_timing(bag_dir, meta, series)
    if duration_s is None or bag_start is None or bag_end is None:
        # Clock unreliable: per-message timing is meaningless. Flag it once and
        # skip gap/low-rate analysis (the never_published checks above stand).
        warnings.append(_clock_unreliable_warning())
        return warnings
    for topic, stamps in series.items():
        topic_sensor = by_topic.get(topic)
        if topic_sensor is None and is_event_topic(topic):
            continue  # silence between events is normal, not a dropout
        gaps = _gap_warnings(topic, topic_sensor, stamps, bag_start, bag_end)
        warnings.extend(gaps)
        if not gaps:
            low = _low_rate_warning(topic, topic_sensor, stamps, duration_s)
            if low:
                warnings.append(low)
    return warnings


def bag_fingerprint(bag: Any) -> str:
    """A content fingerprint for ``bag`` (a schema.Bag or an equal-shaped
    dict), cheap enough to compute before a mission is archived.

    Deliberately *not* a file checksum: those (``Bag.file_sha256``) are only
    known after ``assembler.assemble()`` has already copied/hashed the bag —
    by mission_close review time, before the operator has even decided to
    save, that work hasn't happened yet and re-hashing a multi-GB bag just
    to ask "have I seen this before?" would double the I/O cost of every
    single save. Size + message count + duration + the exact per-topic
    message counts is not cryptographic, but two independently-recorded
    bags matching on all of that simultaneously is practically impossible —
    good enough to detect the same recording being processed twice (a
    mission_close retry, a bag adopted more than once), which is what this
    is for. It is not meant to catch two merely-similar recordings.
    """

    def get(obj: Any, name: str) -> Any:
        return obj.get(name) if isinstance(obj, dict) else getattr(obj, name)

    topics = sorted(
        (get(t, "name"), get(t, "message_count")) for t in get(bag, "topics"))
    duration_s = get(bag, "duration_s")
    payload = json.dumps([
        get(bag, "size_bytes"),
        get(bag, "message_count"),
        round(duration_s, 3) if duration_s is not None else None,
        topics,
    ], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()
