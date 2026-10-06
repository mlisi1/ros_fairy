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

import bisect
import hashlib
import json
import statistics
from array import array
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
# Clock discontinuities, seen in the arrival-ordered stream of every topic
# together. Going back by more than this is a step (receive times of
# different topics interleave by milliseconds, never seconds)...
CLOCK_STEP_BACK_S = 1.0
# ...and a silence on *every* topic at once longer than this, while the
# streams were running, is a forward step (an NTP correction after booting
# with a stale clock) or a paused recorder: either way not real dropouts on
# each topic.
CLOCK_STEP_FORWARD_S = 60.0
CLOCK_STEP_MAX_REPORTED = 3

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
# parameter change, a lifecycle transition, a command while driving, a plan
# while navigating). Gaps and low rates on them are meaningless — "/rosout
# dropped out 9 times" was pure noise (2026-10-02), and nav2's outputs gave
# a gap warning in ~34 of Jo's saved missions each (2026-10-06). Matched by
# suffix so namespaced copies (/jo/cmd_vel) count too. A declared sensor is
# always analysed, whatever its name.
EVENT_TOPICS = frozenset({"/rosout", "/parameter_events"})
EVENT_TOPIC_SUFFIXES = (
    "/transition_event", "/rosout", "/parameter_events",
    # commands: only while someone or something drives
    "/cmd_vel", "/cmd_vel_nav", "/cmd_vel_smoothed", "/cmd_vel_teleop",
    "/cmd_vel_unstamped", "/joy", "/speed_limit",
    # goals and plans: only while navigating
    "/goal_pose", "/initialpose", "/clicked_point", "/plan", "/local_plan",
    "/transformed_global_plan", "/received_global_plan", "/motion_target",
    "/slowdown", "/behavior_tree_log",
    # actions report only while a goal is active
    "/_action/feedback", "/_action/status",
    # latched: published once
    "/tf_static", "/robot_description",
)


def is_event_topic(topic: str) -> bool:
    return topic in EVENT_TOPICS or topic.endswith(EVENT_TOPIC_SUFFIXES)


class CleanSeries(dict):
    """topic -> ascending timestamps on a corrected timeline, plus what was
    learnt reading them: ``total_read`` (every stamp read, plausible or
    not), ``steps`` (clock discontinuities, ``(offset_s, delta_s)`` on the
    corrected timeline) and ``truncated`` (a storage file was cut off)."""
    total_read: int = 0
    steps: list[tuple[float, float]]
    truncated: bool = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.steps = []


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


def _section(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) \
        else 0


def parse_bag_metadata(bag_dir: Path) -> dict[str, Any] | None:
    """Parse rosbag2's metadata.yaml. None if absent/unreadable.

    Tolerates partial files (``info: null``, missing or null sections) the
    way a hand-edited or half-written metadata.yaml has them, rather than
    raising out of the watchdog's finalise.
    """
    meta_path = bag_dir / "metadata.yaml"
    if not meta_path.is_file():
        return None
    try:
        raw = yaml.safe_load(meta_path.read_text(errors="replace"))
        info = raw["rosbag2_bagfile_information"]
    except (OSError, yaml.YAMLError, KeyError, TypeError):
        return None
    if not isinstance(info, dict):
        return None
    start_ns = _number(_section(info.get("starting_time")).get(
        "nanoseconds_since_epoch"))
    duration_ns = _number(_section(info.get("duration")).get("nanoseconds"))
    topics = []
    entries = info.get("topics_with_message_count")
    for entry in entries if isinstance(entries, list) else []:
        entry = _section(entry)
        tm = _section(entry.get("topic_metadata"))
        topics.append({
            "name": str(tm.get("name") or ""),
            "type": str(tm.get("type") or ""),
            "message_count": _number(entry.get("message_count")),
        })
    paths = info.get("relative_file_paths")
    return {
        # rosbag2 normally records the format; when it is absent (old or
        # hand-rolled bags) infer the recording distro's default rather than
        # blindly assuming sqlite3.
        "storage_identifier": info.get("storage_identifier")
        or ros_distro.default_storage(),
        "start_s": start_ns / 1e9,
        "duration_s": duration_ns / 1e9,
        "message_count": _number(info.get("message_count")),
        "topics": topics,
        "relative_file_paths": [str(p) for p in paths]
        if isinstance(paths, list) else [],
    }


def metadata_from_storage(bag_dir: Path) -> dict[str, Any] | None:
    """A stand-in for metadata.yaml read from the storage files themselves,
    for a bag whose recorder died before writing it (SIGKILL, power cut).
    None when the folder holds no readable storage."""
    storage, topics, count = bag_storage.salvage_topics(bag_dir)
    if storage == "unknown":
        return None
    return {"storage_identifier": storage, "start_s": 0.0, "duration_s": 0.0,
            "message_count": count, "topics": topics,
            "relative_file_paths": [], "salvaged": True}


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
    leading = stamps[0] - bag_start
    if leading > threshold:
        gap_durations.append(leading)
        warnings.append({
            "topic": topic,
            "sensor_id": sensor.get("sensor_id") if sensor else None,
            "kind": "gap",
            "start_offset_s": 0.0,
            "duration_s": round(leading, 3),
            "plain_text": (
                f"{who} only started sending {what} "
                f"{humanize_duration(leading)} into the recording."),
        })
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
                      meta: dict[str, Any]) -> CleanSeries | None:
    """Per-topic ascending message timestamps (seconds), cleaned.

    Returns None when no supported storage reader exists (timestamp-level work
    is impossible); an empty CleanSeries when a reader ran but found no
    plausible timestamps (``total_read`` then says whether any were read).

    Stamps before ``EPOCH_FLOOR_S`` are dropped so one un-stamped message
    cannot poison gap detection or the recording window. Clock steps are
    found in the arrival order of all topics together (sorting each topic
    hid a backward step), and the timeline is corrected across them, so a
    clock that jumped by hours doesn't turn into an hours-long dropout on
    every topic.
    """
    reader = bag_storage.get_reader(meta["storage_identifier"])
    if reader is None or not reader.supported:
        return None
    try:
        ts = reader.read_timestamps(bag_dir, meta["relative_file_paths"])
    except bag_storage.BagStorageUnsupported:
        return None
    cleaned = CleanSeries()
    cleaned.total_read = ts.total
    cleaned.truncated = ts.truncated
    shift_at, steps = _clock_steps(ts)
    cleaned.steps = steps
    for topic, stamps in ts.stamps.items():
        order = ts.order[topic]
        good = []
        k, shift = 0, 0.0
        for o, t in zip(order, stamps, strict=True):
            while k < len(shift_at) and shift_at[k][0] <= o:
                shift = shift_at[k][1]
                k += 1
            if t >= EPOCH_FLOOR_S:
                good.append(t - shift)
        if good:
            good.sort()
            cleaned[topic] = good
    return cleaned


def _clock_steps(ts: bag_storage.Timestamps
                 ) -> tuple[list[tuple[int, float]], list[tuple[float, float]]]:
    """Clock discontinuities in the arrival-ordered stream.

    Returns ``(shift_at, steps)``: from arrival index ``shift_at[i][0]`` on,
    subtract ``shift_at[i][1]`` (cumulative) to undo the steps; ``steps``
    lists each step as ``(offset_s, delta_s)`` on the corrected timeline.

    A jump in the merged stream only counts when a topic that spans it jumps
    the same way on its own: a file written topic by topic (a converted or
    merged bag) also "jumps back" between topics, but no topic does.
    """
    if ts.total < 2:
        return [], []
    merged = array("d", bytes(8 * ts.total))
    for topic, stamps in ts.stamps.items():
        for o, t in zip(ts.order[topic], stamps, strict=True):
            merged[o] = t
    shift_at: list[tuple[int, float]] = []
    steps: list[tuple[float, float]] = []
    shift = 0.0
    first = prev = None
    for i, t in enumerate(merged):
        if t < EPOCH_FLOOR_S:
            continue  # broken stamps are bag_timing's concern
        if prev is None:
            first = prev = t
            continue
        delta = t - prev
        if (delta < -CLOCK_STEP_BACK_S or delta > CLOCK_STEP_FORWARD_S) \
                and _topic_spans_jump(ts, i, delta):
            offset = prev - shift - first
            shift += delta
            shift_at.append((i, shift))
            steps.append((round(offset, 3), round(delta, 3)))
        prev = t
    return shift_at, steps


def _topic_spans_jump(ts: bag_storage.Timestamps, i: int,
                      delta: float) -> bool:
    """Whether the topics with messages on both sides of arrival index ``i``
    confirm a clock step there.

    Backwards, one topic going back is proof (a topic's own receive times
    can't decrease otherwise). Forwards, one topic's silence is just its own
    outage: at least two topics, and half of those spanning ``i``, must jump
    by at least half of ``delta``.
    """
    spanning = jumping = 0
    for topic, order in ts.order.items():
        j = bisect.bisect_left(order, i)
        if j == 0 or j == len(order):
            continue
        stamps = ts.stamps[topic]
        if stamps[j] < EPOCH_FLOOR_S or stamps[j - 1] < EPOCH_FLOOR_S:
            continue
        spanning += 1
        own = stamps[j] - stamps[j - 1]
        if (own < 0) == (delta < 0) and abs(own) >= abs(delta) / 2:
            if delta < 0:
                return True
            jumping += 1
    return delta > 0 and jumping >= 2 and jumping * 2 >= spanning


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
    read = getattr(series, "total_read", 0) if series is not None else 0
    if series is not None and read and not series:
        # Every stamp read was near the epoch (an unset clock, or sim time
        # starting at 0): there is no real window to recover.
        return None, None, None
    if series:
        plausible = sum(len(stamps) for stamps in series.values())
        # Judge against what was actually read: a truncated file has fewer
        # messages than metadata.yaml counts, and that isn't a broken clock.
        total = read or meta.get("message_count") or plausible
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
    if series is None:
        # No supported reader: the metadata-level checks above are all we
        # can offer.
        return warnings
    if getattr(series, "truncated", False):
        warnings.append({
            "topic": "", "sensor_id": None, "kind": "truncated",
            "start_offset_s": None, "duration_s": None,
            "plain_text": "Part of the recording file is cut off at the end; "
                          "the data before the cut is intact."})
    if not series:
        if getattr(series, "total_read", 0):
            # Data was read, but none of it carries a real time.
            warnings.append(_clock_unreliable_warning())
        return warnings

    bag_start, bag_end, duration_s = bag_timing(bag_dir, meta, series)
    if duration_s is None or bag_start is None or bag_end is None:
        # Clock unreliable: per-message timing is meaningless. Flag it once and
        # skip gap/low-rate analysis (the never_published checks above stand).
        warnings.append(_clock_unreliable_warning())
        return warnings
    steps = getattr(series, "steps", [])
    for offset, delta in steps[:CLOCK_STEP_MAX_REPORTED]:
        how = ("jumped forward by {d}, {o} in (or the recording was paused "
               "that long)" if delta > 0 else "jumped back by {d}, {o} in")
        warnings.append({
            "topic": "", "sensor_id": None, "kind": "clock_step",
            "start_offset_s": offset, "duration_s": delta,
            "plain_text": (
                "The recording device's clock " + how.format(
                    d=humanize_duration(abs(delta)),
                    o=humanize_duration(offset))
                + ". Times and durations have been corrected around it.")})
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
    fields = [
        get(bag, "size_bytes"),
        get(bag, "message_count"),
        round(duration_s, 3) if duration_s is not None else None,
        topics,
    ]
    if not get(bag, "message_count"):
        # Two empty recordings of the same topics would otherwise match;
        # when they were made tells them apart (a retry of the same one
        # still matches). Non-empty bags keep their old fingerprint.
        start = get(bag, "start_time")
        fields.append(start.isoformat() if hasattr(start, "isoformat")
                      else start)
    payload = json.dumps(fields, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()
