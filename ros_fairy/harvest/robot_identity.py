"""Read and validate /etc/ros-fairy/robot_identity.yaml.

The yaml is written once by ``ros2 fairy setup``. Expected structure:

    robot:
      name: Heron-02
      platform: Clearpath Heron USV
      serial_number: H02-2031-XK
    owner:
      organization: Example Marine Robotics Lab
      contact_email: fleet@example.org
      default_license: https://spdx.org/licenses/CC-BY-4.0  # optional
    sensors:
      - sensor_id: gps0
        type: gps
        make_model: u-blox ZED-F9P
        topic: /fix
        frame_id: gps_link          # optional
        calibration: gps0_cal       # optional, must name a calibrations[] entry
    calibrations:
      - name: gps0_cal
        source_path: /opt/cal/gps0.yaml
        format: yaml                # optional
    recording:                      # optional
      topics: [/fix, /depth]        # optional, default: record all
      storage: mcap                 # optional, default: rosbag2 default
"""

from typing import Any

import yaml

from ros_fairy.utils import paths


class RobotIdentityError(Exception):
    """The identity file is missing or malformed."""


_REQUIRED_ROBOT = ("name", "platform", "serial_number")
_REQUIRED_OWNER = ("organization", "contact_email")
_REQUIRED_SENSOR = ("sensor_id", "type", "make_model", "topic")
_REQUIRED_CAL = ("name", "source_path")


def _require(section: dict, keys: tuple, where: str) -> None:
    for key in keys:
        value = section.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RobotIdentityError(f"missing or empty '{key}' in {where}")


def _mapping(value: Any, where: str) -> dict:
    """``value`` if it is a mapping (or absent), else a plain-language error
    — a hand-edited ``robot: Jo`` must not crash with an AttributeError."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RobotIdentityError(f"'{where}' must be a section with named "
                                 f"fields, not {type(value).__name__}")
    return value


def _entries(value: Any, where: str) -> list[dict]:
    """A list of mappings (or nothing), else a plain-language error."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise RobotIdentityError(f"'{where}' must be a list")
    for i, entry in enumerate(value, 1):
        if not isinstance(entry, dict):
            raise RobotIdentityError(f"entry {i} of '{where}' must be a "
                                     "section with named fields")
    return value


def _topic(name: str) -> str:
    """ROS topic names are absolute in the graph: `fix` is `/fix`."""
    name = name.strip()
    return name if name.startswith("/") else "/" + name


def harvest() -> dict[str, Any]:
    """Return the typed identity dict, raising RobotIdentityError on problems.

    Shape:
        robot: {name, platform, serial_number, owner_organization, owner_contact}
        sensors: [{sensor_id, type, make_model, topic, frame_id, calibration_ref}]
        calibrations: [{name, source_path, format}]
        recording: {topics: list[str] | None, storage: str | None}
        default_license: str | None
    """
    path = paths.robot_identity_path()
    if not path.is_file():
        raise RobotIdentityError(f"identity file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RobotIdentityError(f"identity file is not valid YAML: {exc}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise RobotIdentityError(f"identity file can't be read: {exc}") from exc
    if not isinstance(raw, dict):
        raise RobotIdentityError("identity file must be a YAML mapping")

    robot = _mapping(raw.get("robot"), "robot")
    owner = _mapping(raw.get("owner"), "owner")
    _require(robot, _REQUIRED_ROBOT, "robot")
    _require(owner, _REQUIRED_OWNER, "owner")

    calibrations = []
    for cal in _entries(raw.get("calibrations"), "calibrations"):
        _require(cal, _REQUIRED_CAL, "calibrations")
        calibrations.append({
            "name": cal["name"],
            "source_path": cal["source_path"],
            "format": cal.get("format"),
        })
    cal_names = {c["name"] for c in calibrations}

    sensors = []
    seen_ids: set[str] = set()
    for sensor in _entries(raw.get("sensors"), "sensors"):
        _require(sensor, _REQUIRED_SENSOR, "sensors")
        sid = sensor["sensor_id"]
        if sid in seen_ids:
            raise RobotIdentityError(f"duplicate sensor_id '{sid}'")
        seen_ids.add(sid)
        cal_ref = sensor.get("calibration")
        if cal_ref is not None and cal_ref not in cal_names:
            raise RobotIdentityError(
                f"sensor '{sid}' references unknown calibration '{cal_ref}'")
        sensors.append({
            "sensor_id": sid,
            "type": sensor["type"],
            "make_model": sensor["make_model"],
            "topic": _topic(sensor["topic"]),
            "frame_id": sensor.get("frame_id"),
            "calibration_ref": cal_ref,
        })

    recording = _mapping(raw.get("recording"), "recording")
    topics = recording.get("topics")
    if topics is not None:
        # A bare string would be recorded character by character.
        if isinstance(topics, str):
            topics = [topics]
        if not isinstance(topics, list) or \
                not all(isinstance(t, str) and t.strip() for t in topics):
            raise RobotIdentityError("'recording.topics' must be a list of "
                                     "topic names")
        topics = [_topic(t) for t in topics]
    storage = recording.get("storage")
    if storage is not None and not isinstance(storage, str):
        raise RobotIdentityError("'recording.storage' must be a name such as "
                                 "mcap or sqlite3")
    return {
        "robot": {
            "name": robot["name"],
            "platform": robot["platform"],
            "serial_number": robot["serial_number"],
            "owner_organization": owner["organization"],
            "owner_contact": owner["contact_email"],
        },
        "sensors": sensors,
        "calibrations": calibrations,
        "recording": {"topics": topics, "storage": storage},
        "default_license": owner.get("default_license"),
    }
