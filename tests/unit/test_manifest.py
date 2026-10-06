import pytest

import ros_fairy
from ros_fairy.manifest import builder, validator
from ros_fairy.manifest.builder import ManifestError

GRAPH = {
    "captured_at": "2026-06-12T14:03:00+00:00",
    "nodes": ["/navsat"],
    "topics": [{"name": "/fix", "type": "sensor_msgs/msg/NavSatFix"}],
    "ros_packages": ["rclpy"],
    "parameters": {"/navsat": {"rate": 5.0}},
    "complete": True,
}
IDENTITY = {
    "robot": {"name": "Heron-02", "platform": "Clearpath Heron USV",
              "serial_number": "H02", "owner_organization": "Lab",
              "owner_contact": "fleet@example.org"},
    "sensors": [
        {"sensor_id": "gps0", "type": "gps", "make_model": "ZED-F9P",
         "topic": "/fix", "frame_id": None, "calibration_ref": None},
        {"sensor_id": "sonar0", "type": "sonar", "make_model": "Ping2",
         "topic": "/depth", "frame_id": None, "calibration_ref": None},
    ],
    "calibrations": [],
    "default_license": None,
}
STATUS = {"robot_identity": "ok", "system_info": "ok", "ros_graph": "ok",
          "ros_descriptions": "timeout", "docker_info": "skipped"}
BAG = {
    "path": "bags/rosbag2_x", "storage_format": "sqlite3",
    "size_bytes": 1000, "start_time": "2026-06-12T14:03:00+00:00",
    "end_time": "2026-06-12T14:13:00+00:00", "duration_s": 600.0,
    "message_count": 100, "topics": [], "health_warnings": [],
}


def _harvest(with_bag=True):
    h = builder.compose_harvest(
        identity=IDENTITY,
        system={"hostname": "robot1", "kernel": "Linux 6.8", "arch": "aarch64",
                "ros_distro": "jazzy", "apt_ros_versions": {}},
        graph=GRAPH, docker=None, descriptions=None, harvest_status=STATUS)
    if with_bag:
        h["bags"] = [BAG]
    return h


def _context():
    return builder.new_mission_context(
        operator_name="Jane Doe", goal="Survey eelgrass",
        location_name="Marsh Creek")


def test_compose_harvest_sensor_liveness():
    h = _harvest()
    by_id = {s["sensor_id"]: s for s in h["sensors"]}
    assert by_id["gps0"]["detected_at_start"] is True
    assert by_id["sonar0"]["detected_at_start"] is False
    assert h["software"]["ros_fairy_version"] == ros_fairy.__version__
    assert h["provenance"]["harvest_status"] == STATUS


def test_clock_sync_persisted_to_provenance():
    h = builder.compose_harvest(
        identity=IDENTITY,
        system={"hostname": "robot1", "kernel": "Linux 6.8", "arch": "aarch64",
                "ros_distro": "jazzy", "apt_ros_versions": {},
                "clock_synchronized": False},
        graph=GRAPH, docker=None, descriptions=None, harvest_status=STATUS)
    assert h["provenance"]["clock_synchronized"] is False
    h["bags"] = [BAG]
    record = builder.build(h, _context())
    assert record.provenance.clock_synchronized is False


def test_clock_sync_defaults_none_when_absent():
    # pre-1.0 harvest (no clock key) parses without error, field stays None
    h = _harvest()
    assert h["provenance"]["clock_synchronized"] is None
    assert builder.build(h, _context()).provenance.clock_synchronized is None


def test_compose_harvest_liveness_unknown_when_graph_failed():
    # When the graph harvest failed we can't tell — don't claim sensors absent.
    status = {**STATUS, "ros_graph": "failed"}
    h = builder.compose_harvest(
        identity=IDENTITY, system={}, graph={}, docker=None, descriptions=None,
        harvest_status=status)
    by_id = {s["sensor_id"]: s for s in h["sensors"]}
    assert by_id["gps0"]["detected_at_start"] is None
    assert by_id["sonar0"]["detected_at_start"] is None


def test_reconcile_sensor_detection_upgrades_from_bag():
    status = {**STATUS, "ros_graph": "failed"}
    h = builder.compose_harvest(
        identity=IDENTITY, system={}, graph={}, docker=None, descriptions=None,
        harvest_status=status)
    h["bags"] = [{**BAG, "topics": [
        {"name": "/fix", "type": "sensor_msgs/msg/NavSatFix", "message_count": 50},
        {"name": "/depth", "type": "x", "message_count": 0},
    ]}]
    builder.reconcile_sensor_detection(h)
    by_id = {s["sensor_id"]: s for s in h["sensors"]}
    # /fix carried data -> detected; /depth was silent -> still unknown.
    assert by_id["gps0"]["detected_at_start"] is True
    assert by_id["sonar0"]["detected_at_start"] is None


def test_warnings_silent_when_liveness_unknown():
    status = {**STATUS, "ros_graph": "failed"}
    h = builder.compose_harvest(
        identity=IDENTITY, system={}, graph={}, docker=None, descriptions=None,
        harvest_status=status)
    warnings = builder.harvest_level_warnings(h)
    # Don't accuse sensors of being down when we couldn't reach the graph.
    assert not any("didn't seem to be running" in w for w in warnings)


def test_new_mission_context_shape():
    ctx = _context()
    assert ctx["identity"]["mission_id"].startswith("m-")
    assert len(ctx["identity"]["mission_id"]) == len("m-20260612-140258-9f3a")
    assert ctx["intent"]["environment"] is None


def test_build_record_and_confidence():
    record = builder.build(_harvest(), _context())
    assert record.identity.operator_name == "Jane Doe"
    # contact defaulted from robot identity
    assert record.identity.operator_contact == "fleet@example.org"
    assert record.robot.name == "Heron-02"
    assert record.bags[0].duration_s == 600.0
    fc = record.provenance.field_confidence
    assert fc["intent.goal"] == "user"
    assert fc["identity.operator_name"] == "user"
    assert fc["identity.mission_id"] == "auto"
    assert fc["robot.name"] == "auto"
    assert fc["ros_graph.parameters"] == "auto"
    assert "ros_graph.parameters./navsat" not in fc
    # round-trips through json mode
    from ros_fairy import SCHEMA_VERSION
    assert record.model_dump(mode="json")["schema_version"] == SCHEMA_VERSION


def test_validator_missing_fields():
    assert validator.missing_user_fields(None) == \
        ["operator_name", "goal", "location_name"]
    ctx = _context()
    ctx["intent"]["goal"] = "  "
    assert validator.missing_user_fields(ctx) == ["goal"]


def test_build_fails_plainly_without_briefing():
    with pytest.raises(ManifestError) as err:
        builder.build(_harvest(), None)
    msg = str(err.value)
    assert "who ran this mission" in msg
    assert "Traceback" not in msg


def test_build_fails_without_bags():
    with pytest.raises(ManifestError, match="nothing recorded"):
        builder.build(_harvest(with_bag=False), _context())


def test_harvest_level_warnings():
    h = _harvest()
    warnings = builder.harvest_level_warnings(h)
    assert any("physical description" in w for w in warnings)
    assert any("Ping2 (sonar0) didn't seem to be running" in w
              for w in warnings)

    h_no_robot = _harvest()
    h_no_robot["robot"] = None
    h_no_robot["provenance"]["harvest_status"]["robot_identity"] = "failed"
    assert any("hasn't been set up" in w
               for w in builder.harvest_level_warnings(h_no_robot))

    assert "recording assistant" in builder.harvest_level_warnings(None)[0]


def test_harvest_level_warnings_does_not_double_report_a_silent_sensor():
    """A sensor that was both undetected at start and never published data
    in the bag is one fact, not two — reported 2026-09-11: 'Realsense D456
    didn't seem to be running' and 'Camera (cam1) produced no data at all'
    both appearing for what was the same disconnected camera."""
    h = _harvest()
    h["bags"] = [{**BAG, "health_warnings": [
        {"topic": "/depth", "sensor_id": "sonar0", "kind": "never_published",
         "start_offset_s": None, "duration_s": None,
         "plain_text": "Sonar (sonar0) produced no data at all during this "
                       "recording."},
    ]}]
    warnings = builder.harvest_level_warnings(h)
    assert not any("didn't seem to be running" in w for w in warnings)


def test_load_spool(fairy_dirs):
    import json

    from ros_fairy.utils import paths
    assert builder.load_spool() == (None, None)
    paths.harvest_json_path().write_text(json.dumps(_harvest()))
    paths.mission_context_path().write_text(json.dumps(_context()))
    harvest, ctx = builder.load_spool()
    assert harvest["robot"]["name"] == "Heron-02"
    assert ctx["identity"]["operator_name"] == "Jane Doe"
