"""Pydantic models for the MissionRecord.

The spec file is authoritative; any field change must land there first.
schema_version: see ros_fairy.SCHEMA_VERSION for the format's history and
the compatibility rule; ``read_record`` loads an archived record.
"""

from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    model_validator,
)

from ros_fairy import SCHEMA_VERSION


class _Model(BaseModel):
    """Strict when building a record (an unknown field is a bug), lenient
    when reading one from an archive with ``context={"lenient": ...}``: a
    field this version doesn't know (the record was written by a newer
    ros-fairy) is set aside and noted in the context's ``dropped`` list."""
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _set_aside_unknown(cls, data: Any, info: ValidationInfo) -> Any:
        context = info.context
        if not context or not context.get("lenient") \
                or not isinstance(data, dict):
            return data
        known = set(cls.model_fields)
        unknown = [k for k in data if k not in known]
        if not unknown:
            return data
        context.setdefault("dropped", []).extend(
            f"{cls.__name__}.{k}" for k in unknown)
        return {k: v for k, v in data.items() if k in known}


def _assume_utc(value: datetime) -> datetime:
    """A timestamp without a timezone (an old or hand-edited record) is
    UTC — what ros-fairy always writes — so time arithmetic on it works."""
    return value if value.tzinfo is not None \
        else value.replace(tzinfo=timezone.utc)


AwareDatetime = Annotated[datetime, AfterValidator(_assume_utc)]


class Identity(_Model):
    mission_id: str
    created_at: AwareDatetime
    operator_name: str = Field(min_length=1, max_length=80)
    operator_contact: str | None = None


class Intent(_Model):
    goal: str = Field(min_length=1, max_length=280)
    location_name: str = Field(min_length=1)
    environment: str | None = None
    notes: str | None = None


class Robot(_Model):
    name: str
    platform: str
    serial_number: str
    owner_organization: str
    owner_contact: str


class Sensor(_Model):
    sensor_id: str
    type: str
    make_model: str
    topic: str
    frame_id: str | None = None
    calibration_ref: str | None = None
    # True = seen publishing, False = live graph confirmed it absent,
    # None = couldn't tell (the harvest couldn't reach the ROS graph).
    detected_at_start: bool | None = None


class DockerContainer(_Model):
    name: str
    image: str
    digest: str | None = None
    compose_project: str | None = None
    compose_file: str | None = None
    # `ros2 pkg list` exec'd into this container, if it was running and the
    # probe succeeded. None means "couldn't tell" (not running, no ROS
    # found, or the probe failed) — distinct from Software.ros_packages,
    # which only ever reflects the host (see harvest/ros_graph.py).
    ros_packages: list[str] | None = None
    # The Python distributions this container's python3 sees (name/version),
    # read for containers running ROS: the robot's Python, which
    # Software.python_env (the host interpreter running ros-fairy) is not.
    # None = not probed or couldn't be read.
    python_packages: list["PythonPackage"] | None = None


class PythonPackage(_Model):
    name: str
    version: str
    installer: str | None = None
    editable: bool = False
    location: str | None = None


class PythonEnv(_Model):
    executable: str
    version: str
    venv_path: str | None = None
    pip_version: str | None = None
    packages: list[PythonPackage] = Field(default_factory=list)
    ros_fairy_editable: bool = False
    sys_path: list[str] = Field(default_factory=list)


class HardwareDevice(_Model):
    device_class: str | None = None
    vendor_id: str | None = None
    product_id: str | None = None
    vendor_name: str | None = None
    product_name: str | None = None
    serial_number: str | None = None
    device_path: str | None = None
    bus_path: str | None = None
    driver: str | None = None
    source_command: str
    udev_properties: dict[str, str] | None = None


class UdevRule(_Model):
    """A udev rule file that is the robot's own configuration (not a
    package's unmodified default). Its content is archived in the crate."""
    path: str
    reason: str            # "local" (/etc, /run) | "modified" | "unpackaged"
    package: str | None = None
    sha256: str
    size_bytes: int
    masks: str | None = None       # name of a default rule it disables
    overrides: str | None = None   # path of the default rule it replaces
    archived_path: str | None = None


class DefaultUdevRule(_Model):
    path: str
    package: str


class UdevRules(_Model):
    custom: list[UdevRule] = Field(default_factory=list)
    default: list[DefaultUdevRule] = Field(default_factory=list)


class AppliedUdevRule(_Model):
    """A non-default rule line that acts on a USB device or its children
    (from `udevadm test`). For an attribute it writes, ``in_effect`` says
    whether the device really has that value now."""
    node: str
    rule: str                     # "<file>:<line>"
    action: str
    attribute: str | None = None
    expected: str | None = None
    actual: str | None = None
    in_effect: bool | None = None


class UsbInterface(_Model):
    name: str
    interface_class: str | None = None
    driver: str | None = None


class UsbSerialPort(_Model):
    tty: str
    latency_timer_ms: str | None = None


class UsbDevice(_Model):
    sysfs_name: str
    port_path: str | None = None
    bus: str | None = None
    device: str | None = None
    vendor_id: str | None = None
    product_id: str | None = None
    manufacturer: str | None = None
    product: str | None = None
    serial: str | None = None
    speed_mbps: str | None = None
    usb_version: str | None = None
    max_power: str | None = None
    removable: str | None = None
    authorized: str | None = None
    quirks: str | None = None
    avoid_reset_quirk: str | None = None
    driver: str | None = None
    power: dict[str, str] = Field(default_factory=dict)
    interfaces: list[UsbInterface] = Field(default_factory=list)
    serial_ports: list[UsbSerialPort] = Field(default_factory=list)
    udev_rules_applied: list[AppliedUdevRule] = Field(default_factory=list)


class UsbPort(_Model):
    port: str
    hub: str
    connected: str | None = None
    peer: str | None = None
    power_control: str | None = None
    connect_type: str | None = None
    disable: str | None = None
    usb3_lpm_permit: str | None = None
    state: str | None = None
    over_current_count: str | None = None
    quirks: str | None = None
    location: str | None = None


class UsbState(_Model):
    """How every USB device and port is managed, from sysfs."""
    usbcore: dict[str, str] = Field(default_factory=dict)
    driver_parameters: dict[str, dict[str, str]] = Field(default_factory=dict)
    devices: list[UsbDevice] = Field(default_factory=list)
    ports: list[UsbPort] = Field(default_factory=list)


class Software(_Model):
    ros_distro: str | None = None
    # `ros2 pkg list` on the host. None = not captured (probe failed); an
    # empty list never happens on a ROS host, so old records' [] means the same.
    ros_packages: list[str] | None = None
    # Installed ros-* debs. None = not captured (dpkg couldn't be asked);
    # {} = none installed.
    apt_ros_versions: dict[str, str] | None = Field(default_factory=dict)
    # None = not captured (Docker unreachable, or no Docker on this robot);
    # [] = Docker answered and nothing was running.
    docker_containers: list[DockerContainer] | None = Field(
        default_factory=list)
    ros_fairy_version: str
    python_env: PythonEnv | None = None


class TopicInfo(_Model):
    name: str
    type: str


class RosGraph(_Model):
    captured_at: AwareDatetime | None = None
    nodes: list[str] = Field(default_factory=list)
    topics: list[TopicInfo] = Field(default_factory=list)
    parameters: dict[str, dict] = Field(default_factory=dict)
    # node -> parameter names it listed but never returned a value for (the
    # call timed out): "not captured", unlike a None value in `parameters`,
    # which means "declared without a value".
    parameters_not_captured: dict[str, list[str]] = Field(default_factory=dict)
    robot_description: str | None = None
    tf_static: list[dict] | None = None
    complete: bool = False


class Calibration(_Model):
    name: str
    source_path: str
    archived_path: str | None = None
    sha256: str | None = None
    format: str | None = None


class BagTopic(_Model):
    name: str
    type: str
    message_count: int
    avg_frequency_hz: float | None = None


class HealthWarning(_Model):
    topic: str
    sensor_id: str | None = None
    # gap | never_published | low_rate | unreliable_clock | clock_step |
    # truncated | compressed_transport (the last is informational — see
    # topic_health.INFO_KINDS)
    kind: str
    start_offset_s: float | None = None
    duration_s: float | None = None
    plain_text: str


# Bag.source values for recordings made outside `ros2 fairy mission_record`
# (referenced in place at their original path, copied into the crate at archive
# time rather than moved).
FOREIGN_SOURCES = frozenset({"detected", "adopted"})


class Bag(_Model):
    path: str
    # How the recording entered the pipeline: "mission_record" (recorded by the
    # wrapper into the spool, moved into the crate), "detected" (found running
    # outside the wrapper by the watchdog's /proc poller) or "adopted" (ingested
    # after the fact by `ros2 fairy adopt`). The latter two are "foreign" — see
    # FOREIGN_SOURCES — and are referenced in place then copied, not moved.
    source: str = "mission_record"
    storage_format: str
    size_bytes: int
    # None when the recording clock was too unreliable to recover the real
    # window (e.g. an unsynced system clock that left most messages stamped near
    # the epoch). A health warning explains it; rates are then None too.
    start_time: AwareDatetime | None = None
    end_time: AwareDatetime | None = None
    duration_s: float | None = None
    message_count: int
    topics: list[BagTopic] = Field(default_factory=list)
    health_warnings: list[HealthWarning] = Field(default_factory=list)
    # Bag-relative file path -> sha256, recorded at archive time (the bag is
    # moved verbatim, so this pins its bytes). Empty for pre-1.0 archives.
    file_sha256: dict[str, str] = Field(default_factory=dict)


class SoftwareBuild(_Model):
    """One ros-fairy build (see ros_fairy/build_info.py). ``code_id`` hashes
    the source files, so it also tells apart builds of the same commit with
    local edits."""
    version: str
    commit: str | None = None
    describe: str | None = None
    branch: str | None = None
    dirty: bool | None = None
    built_at: AwareDatetime | None = None
    code_id: str | None = None
    source: str | None = None    # "install" | "checkout" | "unknown"


class Provenance(_Model):
    ros_fairy_version: str
    schema_version: str = SCHEMA_VERSION
    harvested_at: AwareDatetime | None = None
    assembled_at: AwareDatetime | None = None
    hostname: str = ""
    kernel: str = ""
    arch: str = ""
    # Result of the pre-record clock-sync check (utils/clock.is_synchronized),
    # recorded regardless of outcome so the archive shows it was performed.
    # True/False, or None when it couldn't be determined / pre-1.0 records.
    clock_synchronized: bool | None = None
    field_confidence: dict[str, str] = Field(default_factory=dict)
    harvest_status: dict[str, str] = Field(default_factory=dict)
    # Overall data-quality verdict from manifest/quality.assess at close time:
    # "ok" | "degraded" | "poor". None for records written before 1.0.
    data_quality: str | None = None
    # The build that captured the context (the watchdog, or the CLI for an
    # adopted or salvaged recording) and the one that saved the crate
    # (mission_close). None in records written before 1.2.
    harvested_by: SoftwareBuild | None = None
    assembled_by: SoftwareBuild | None = None


class MissionRecord(_Model):
    schema_version: str = SCHEMA_VERSION
    identity: Identity
    intent: Intent
    robot: Robot | None = None
    sensors: list[Sensor] = Field(default_factory=list)
    software: Software
    ros_graph: RosGraph = Field(default_factory=RosGraph)
    calibrations: list[Calibration] = Field(default_factory=list)
    bags: list[Bag] = Field(default_factory=list)
    hardware_devices: list[HardwareDevice] = Field(default_factory=list)
    # None = not captured (older records, or the probe failed).
    usb: UsbState | None = None
    udev_rules: UdevRules | None = None
    provenance: Provenance


class NewerRecordError(Exception):
    """The record's format is a newer *major* version than this ros-fairy
    understands."""


def _major(version: str) -> int:
    try:
        return int(str(version).split(".")[0])
    except ValueError:
        return 0


def read_record(data: Any) -> tuple["MissionRecord", list[str]]:
    """Validate an archived record; returns ``(record, set_aside)``.

    A record from a newer *minor* format (added fields) is read with the
    fields this version doesn't know set aside, and listed. A newer *major*
    format raises NewerRecordError rather than misreading it.
    """
    version = data.get("schema_version", "1.0") if isinstance(data, dict) \
        else "1.0"
    if _major(version) > _major(SCHEMA_VERSION):
        raise NewerRecordError(
            f"it was saved in record format {version}, newer than this "
            f"ros-fairy understands ({SCHEMA_VERSION}); update ros-fairy "
            "to read it")
    context: dict[str, Any] = {"lenient": True, "dropped": []}
    record = MissionRecord.model_validate(data, context=context)
    return record, sorted(set(context["dropped"]))
