"""Merge harvest.json + mission_context.json into a MissionRecord.

Also owns the two spool JSON shapes: ``compose_harvest`` is what the watchdog
uses to shape harvest.json, and ``new_mission_context`` is what mission_start
writes.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ros_fairy
from ros_fairy.manifest import validator
from ros_fairy.manifest.schema import FOREIGN_SOURCES, MissionRecord
from ros_fairy.utils import paths


class ManifestError(Exception):
    """Raised with a plain-language, user-facing message."""


# Ordered list of harvest modules — single source of truth for harvest_status keys.
# Add new modules here; callers that build a default status dict use this constant.
HARVEST_MODULES: tuple[str, ...] = (
    "robot_identity",
    "system_info",
    "python_env",
    "hardware_devices",
    "ros_graph",
    "docker_info",
    "ros_descriptions",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_mission_id(created_at: datetime | None = None) -> str:
    created_at = created_at or _now()
    return f"m-{created_at.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


def new_mission_context(operator_name: str, goal: str, location_name: str,
                        environment: str | None = None,
                        notes: str | None = None,
                        operator_contact: str | None = None) -> dict[str, Any]:
    created_at = _now()
    return {
        "identity": {
            "mission_id": new_mission_id(created_at),
            "created_at": created_at.isoformat(),
            "operator_name": operator_name,
            "operator_contact": operator_contact,
        },
        "intent": {
            "goal": goal,
            "location_name": location_name,
            "environment": environment,
            "notes": notes,
        },
    }


def compose_harvest(identity: dict | None, system: dict | None,
                    graph: dict | None, docker: dict | None,
                    descriptions: dict | None,
                    harvest_status: dict[str, str],
                    python_env: dict | None = None,
                    hardware_devices: dict | None = None) -> dict[str, Any]:
    """Shape the harvest.json document from raw harvest module outputs.

    Any module's output may be None (failed/skipped); the document always has
    every key so downstream code never probes for presence.
    """
    identity = identity or {}
    system = system or {}
    graph = graph or {}
    docker = docker or {}
    descriptions = descriptions or {}
    py = python_env or {}
    hw = hardware_devices or {}

    # Sensor liveness comes from the live graph, but only if we actually reached
    # it. When the graph harvest failed we genuinely don't know — mark it
    # unknown (None) rather than falsely claiming every sensor was absent
    # (recorded bags later upgrade this; see reconcile_sensor_detection).
    sensors = []
    # "partial" means the graph itself (nodes/topics) was reachable and only
    # the per-node parameter dump timed out — live_topics is still trustworthy.
    graph_reachable = harvest_status.get("ros_graph") in ("ok", "partial")
    live_topics = {t["name"] for t in graph.get("topics", [])}
    for sensor in identity.get("sensors", []):
        detected = sensor["topic"] in live_topics if graph_reachable else None
        sensors.append({**sensor, "detected_at_start": detected})

    return {
        "robot": identity.get("robot"),
        "sensors": sensors,
        "calibrations": identity.get("calibrations", []),
        "default_license": identity.get("default_license"),
        "software": {
            "ros_distro": system.get("ros_distro"),
            "ros_packages": graph.get("ros_packages"),
            # None when system_info failed or dpkg couldn't be asked.
            "apt_ros_versions": system.get("apt_ros_versions"),
            "docker_containers": docker.get("docker_containers", []),
            "ros_fairy_version": ros_fairy.__version__,
            "python_env": py.get("python_env"),
        },
        "ros_graph": {
            "captured_at": graph.get("captured_at"),
            "nodes": graph.get("nodes", []),
            "topics": graph.get("topics", []),
            "parameters": graph.get("parameters", {}),
            "parameters_not_captured": graph.get("parameters_not_captured")
            or {},
            "robot_description": descriptions.get("robot_description"),
            "tf_static": descriptions.get("tf_static"),
            "complete": graph.get("complete", False),
        },
        "hardware_devices": hw.get("devices", []),
        "usb": hw.get("usb"),
        "udev_rules": hw.get("udev_rules"),
        "bags": [],
        "provenance": {
            "ros_fairy_version": ros_fairy.__version__,
            "schema_version": ros_fairy.SCHEMA_VERSION,
            "harvested_at": None,  # set by the watchdog at FINALISING
            "hostname": system.get("hostname", ""),
            "kernel": system.get("kernel", ""),
            "arch": system.get("arch", ""),
            "clock_synchronized": system.get("clock_synchronized"),
            "harvest_status": harvest_status,
        },
        "raw_docker_inspect": docker.get("raw_inspect", []),
        "raw_python_env": {
            "pip_freeze": py.get("pip_freeze"),
            "pip_list_json": py.get("pip_list_json"),
        },
        "raw_hardware": {
            "lsusb_verbose": hw.get("lsusb_verbose"),
            "dmesg_usb": hw.get("dmesg_usb"),
            # archived in the crate: the non-default udev rules themselves
            # and the `udevadm test` trace of which rules acted where
            "udev_rule_files": hw.get("udev_rule_files") or {},
            "udev_trace": hw.get("udev_trace") or [],
        },
    }


def reconcile_sensor_detection(harvest: dict | None) -> None:
    """Upgrade ``detected_at_start`` with recorded-bag evidence, in place.

    Liveness is sampled from the live ROS graph at mission start, but the
    harvest process often can't see the graph the recorder can (issue #26): every
    sensor then looks not-detected even though the bag proves it was publishing.
    Recorded topics are ground truth — a sensor whose topic carries messages in
    any bag was clearly running, so mark it detected. Sensors with no recorded
    data are left as the graph saw them (False = confirmed absent, None =
    graph unreachable, so still unknown).
    """
    if not harvest:
        return
    recorded = {t["name"] for bag in harvest.get("bags", [])
                for t in bag.get("topics", []) if (t.get("message_count") or 0) > 0}
    for sensor in harvest.get("sensors", []):
        if sensor.get("topic") in recorded:
            sensor["detected_at_start"] = True


def load_spool() -> tuple[dict | None, dict | None]:
    """Read (harvest.json, mission_context.json); None for missing files."""
    docs = []
    for path in (paths.harvest_json_path(), paths.mission_context_path()):
        if path.is_file():
            try:
                docs.append(json.loads(path.read_text()))
            except json.JSONDecodeError:
                docs.append(None)
        else:
            docs.append(None)
    return docs[0], docs[1]


def _field_confidence(record: MissionRecord) -> dict[str, str]:
    """Dotted path -> 'auto' | 'user' for every populated leaf field."""
    user_paths = {"identity.operator_name", "intent.goal",
                  "intent.location_name", "intent.environment", "intent.notes"}

    confidence: dict[str, str] = {}

    def walk(value: Any, path: str) -> None:
        if value is None:
            return
        if isinstance(value, dict):
            for key, sub in value.items():
                walk(sub, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            for i, sub in enumerate(value):
                walk(sub, f"{path}[{i}]")
        else:
            confidence[path] = "user" if path in user_paths else "auto"

    dumped = record.model_dump(mode="json", exclude={"provenance":
                                                     {"field_confidence"}})
    # Graph parameters and the URDF string would bloat the map with thousands
    # of auto entries that say nothing; tag those subtrees at section level.
    dumped["ros_graph"].pop("parameters", None)
    dumped["ros_graph"].pop("robot_description", None)
    dumped["ros_graph"].pop("tf_static", None)
    walk(dumped, "")
    for section in ("parameters", "robot_description", "tf_static"):
        if getattr(record.ros_graph, section):
            confidence[f"ros_graph.{section}"] = "auto"
    return confidence


def build(harvest: dict | None, context: dict | None) -> MissionRecord:
    """Merge the two spool documents into a validated MissionRecord."""
    errors = validator.validate(harvest, context)
    if errors:
        raise ManifestError(" ".join(errors))
    assert harvest is not None and context is not None

    identity = dict(context["identity"])
    if not identity.get("operator_contact"):
        identity["operator_contact"] = (harvest.get("robot") or {}).get(
            "owner_contact")

    record = MissionRecord.model_validate({
        "identity": identity,
        "intent": context["intent"],
        "robot": harvest.get("robot"),
        "sensors": harvest.get("sensors", []),
        "software": harvest["software"],
        "ros_graph": harvest.get("ros_graph", {}),
        "calibrations": harvest.get("calibrations", []),
        "bags": harvest.get("bags", []),
        "hardware_devices": harvest.get("hardware_devices", []),
        "usb": harvest.get("usb"),
        "udev_rules": harvest.get("udev_rules"),
        "provenance": harvest["provenance"],
    })
    record.provenance.field_confidence = _field_confidence(record)
    return record


def _udev_not_in_effect(usb: dict | None) -> list[str]:
    """One warning per robot udev rule line whose setting a device lacks."""
    by_rule: dict[tuple[str, str, str], list[str]] = {}
    for dev in (usb or {}).get("devices", []):
        label = dev.get("product") or \
            f"USB device {dev.get('vendor_id')}:{dev.get('product_id')}"
        for applied in dev.get("udev_rules_applied", []):
            if applied.get("in_effect") is not False:
                continue
            setting = Path(applied.get("attribute") or "").name
            parent = Path(applied.get("attribute") or "").parent.name
            if parent == "power":
                setting = f"power/{setting}"
            key = (Path(applied["rule"]).name, setting,
                   applied.get("expected") or "")
            by_rule.setdefault(key, []).append(
                f"{label} has {applied.get('actual')!r}")
    return [f"The robot's udev rule {rule} should set {setting} to "
            f"{expected!r}, but {', '.join(devices)} — that device setting "
            "isn't in effect during this recording."
            for (rule, setting, expected), devices in sorted(by_rule.items())]


def harvest_level_warnings(harvest: dict | None) -> list[str]:
    """Plain-language warnings about gaps in the silent context capture."""
    if harvest is None:
        return ["I couldn't capture any background information about this "
                "recording — the recording assistant may not be running."]
    warnings = []
    status = (harvest.get("provenance") or {}).get("harvest_status", {})
    if status.get("robot_identity") == "failed" or not harvest.get("robot"):
        warnings.append("This robot hasn't been set up yet — ask your "
                        "engineer to run `ros2 fairy setup`.")
    if status.get("ros_graph") in ("failed", "timeout"):
        warnings.append("I couldn't capture the software versions and "
                        "settings because the robot software wasn't "
                        "reachable.")
    elif not (harvest.get("ros_graph") or {}).get("complete", False):
        warnings.append("Some software settings could not be captured in "
                        "time; the record may be missing a few details.")
    if status.get("python_env") == "failed":
        warnings.append("I couldn't capture the Python environment details.")
    if status.get("ros_descriptions") == "absent":
        warnings.append("The robot's physical description wasn't being "
                        "published, so it isn't included.")
    elif status.get("ros_descriptions") == "timeout":
        warnings.append("The robot's physical description was being "
                        "published but didn't arrive in time, so it isn't "
                        "included.")
    warnings += _udev_not_in_effect(harvest.get("usb"))
    containers = (harvest.get("software") or {}).get("docker_containers", [])
    if any(c.get("digest") is None for c in containers):
        warnings.append("Some software containers couldn't be pinned to an "
                        "exact version.")
    # A recording made outside mission_record is referenced where it was made;
    # if the operator has since moved or deleted it, it can't be saved.
    gone = sum(1 for bag in harvest.get("bags", [])
               if bag.get("source") in FOREIGN_SOURCES
               and not Path(bag.get("path", "")).is_dir())
    if gone:
        thing = "recording" if gone == 1 else "recordings"
        warnings.append(f"{gone} {thing} made earlier can no longer be found "
                        "where they were recorded, so they won't be saved.")
    # A sensor that also recorded no data at all is flagged from bag health
    # (topic_health's "never_published") below, with a more specific message
    # — don't say the same thing about it twice.
    silent_sensor_ids = {
        w.get("sensor_id") for bag in harvest.get("bags", [])
        for w in bag.get("health_warnings", [])
        if w.get("kind") == "never_published" and w.get("sensor_id")}
    for sensor in harvest.get("sensors", []):
        # Only warn when the live graph actually confirmed the sensor absent.
        # ``None`` means we couldn't reach the graph to check — don't accuse a
        # sensor of being down when a recorded bag may prove otherwise.
        if sensor.get("detected_at_start") is False and \
                sensor.get("sensor_id") not in silent_sensor_ids:
            warnings.append(f"{sensor['make_model']} ({sensor['sensor_id']}) "
                            f"didn't seem to be running when the recording "
                            f"started.")
    return warnings
