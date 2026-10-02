"""Data-quality assessment for a finished mission.

The real-robot runs repeatedly produced archives that *looked* saved but were
nearly worthless — no ROS context captured, or bags with an unusable clock. The
tool happily archived them and the problem was only discovered later. This module
turns "is this mission actually any good?" into one explicit verdict, used to
gate the save decision at ``mission_close`` and to mark degraded missions in the
index so they are findable afterwards.

Levels:
  - ``ok``       — nothing important is missing.
  - ``degraded`` — usable, but with gaps (some sensors, some recordings).
  - ``poor``     — core content is missing (no ROS context, or no usable
                   recordings); saving should be a deliberate choice.
"""

from dataclasses import dataclass, field

from ros_fairy.manifest.schema import MissionRecord

OK, DEGRADED, POOR = "ok", "degraded", "poor"


@dataclass
class Quality:
    level: str
    reasons: list[str] = field(default_factory=list)


def assess(record: MissionRecord, harvest: dict | None = None) -> Quality:
    """Grade a built MissionRecord. ``harvest`` adds harvest_status context."""
    status = ((harvest or {}).get("provenance") or {}).get("harvest_status", {})
    major: list[str] = []
    minor: list[str] = []

    # No software/settings captured from the robot — the empty-archive failure.
    if status.get("ros_graph") in ("failed", "timeout") \
            or not record.ros_graph.nodes:
        major.append("No software or settings were captured from the robot, so "
                     "the recording can't be reproduced (the assistant couldn't "
                     "reach ROS).")

    # Recordings with no usable timing (broken clock) — unplayable.
    bags = record.bags
    unusable = [b for b in bags if b.duration_s is None]
    if bags and len(unusable) == len(bags):
        major.append("The recordings have no usable timing — the clock was "
                     "wrong, so they may not play back correctly.")
    elif unusable:
        minor.append(f"{len(unusable)} of {len(bags)} recordings have unusable "
                     "timing (the clock was wrong).")

    if record.robot is None:
        major.append("This robot hasn't been set up, so there's no robot or "
                     "sensor information.")

    # Sensors that produced no data at all, in every one of the mission's
    # recordings. A sensor missing from one short recording but present in
    # another did produce data (2026-10-02: one unrelated recording made
    # every sensor look silent); the per-recording warnings still say which.
    silent_per_bag = [{w.sensor_id for w in b.health_warnings
                       if w.kind == "never_published" and w.sensor_id}
                      for b in bags]
    silent = set.intersection(*silent_per_bag) if silent_per_bag else set()
    if silent:
        minor.append(f"{len(silent)} sensor(s) produced no data at all.")

    # Declared sensors the live graph confirmed absent at start. Sensors with
    # unknown liveness (None — graph unreachable) are not counted here. A
    # sensor already counted as silent above isn't counted again here — it's
    # the same underlying problem seen two ways, not two problems.
    if record.sensors:
        not_detected = [s for s in record.sensors
                        if s.detected_at_start is False
                        and s.sensor_id not in silent]
        if not_detected and len(not_detected) == len(record.sensors):
            minor.append("None of the declared sensors were detected when "
                         "recording started.")
        elif not_detected:
            minor.append(f"{len(not_detected)} of {len(record.sensors)} "
                         "sensors weren't detected when recording started.")

    level = POOR if major else DEGRADED if minor else OK
    return Quality(level=level, reasons=major + minor)
