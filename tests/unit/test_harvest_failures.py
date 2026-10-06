"""Harvest probes: FAILURE_CASES H1-H19 (the parts testable without ROS;
the live snapshot was checked on Jo, see FAILURE_CASES.md)."""

import json
import subprocess
import time
from types import SimpleNamespace as V
from unittest import mock

import pytest

from ros_fairy.harvest import (
    docker_info,
    hardware_devices,
    python_env,
    robot_identity,
    ros_graph,
    ros_snapshot,
    system_info,
)
from ros_fairy.manifest import builder
from ros_fairy.watchdog import watchdog as wd_mod

# -- H13 / H11: the snapshot child's output ------------------------------------


def test_snapshot_survives_a_teardown_hang():
    """H13: the child prints its result before tearing rclpy down; a hang
    after that must not lose it."""
    done = json.dumps({"error": None, "nodes": ["/a"], "parameters": {}})
    hung = subprocess.TimeoutExpired("python3", 48,
                                     output=(done + "\n").encode())
    with mock.patch("subprocess.run", side_effect=hung):
        assert ros_snapshot.take()["nodes"] == ["/a"]


def test_snapshot_tolerates_non_utf8_output():
    """H11: an odd byte in a DDS warning must not cost the snapshot."""
    out = b"warn \xff\xfe\n" + json.dumps({"nodes": ["/a"]}).encode() + b"\n"
    proc = subprocess.CompletedProcess([], 0, out, b"\xff stderr")
    with mock.patch("subprocess.run", return_value=proc):
        assert ros_snapshot.take()["nodes"] == ["/a"]


def test_hardware_commands_decode_leniently():
    r = hardware_devices._run(["printf", "\\377ok"])
    assert r is not None and r.stdout.endswith("ok")


# -- H14 / H15 / H6: values, transforms, description topics --------------------

def test_nan_and_infinite_parameters_are_strict_json():
    assert ros_snapshot._value(V(type=3, double_value=float("nan"))) == "nan"
    assert ros_snapshot._value(V(type=3, double_value=float("-inf"))) == "-inf"
    assert ros_snapshot._value(V(type=8, double_array_value=[1.0,
                                                             float("inf")])) \
        == [1.0, "inf"]
    json.dumps({"v": ros_snapshot.json_float(float("nan"))}, allow_nan=False)


def test_a_frame_with_two_parents_is_reported():
    def tf(parent, child):
        return {"parent_frame": parent, "child_frame": child,
                "translation": {}, "rotation": {}}
    acc, conflicts = {}, []
    ros_snapshot.merge_transforms(acc, [tf("a", "b")], conflicts)
    ros_snapshot.merge_transforms(acc, [tf("a", "b")], conflicts)  # same
    ros_snapshot.merge_transforms(acc, [tf("c", "b")], conflicts)
    assert list(acc) == ["b"] and acc["b"]["parent_frame"] == "c"
    assert conflicts == ["b: a replaced by c"]


def test_namespaced_description_topics_are_found():
    topics = [("/jo/robot_description", ["std_msgs/msg/String"]),
              ("/jo/tf_static", ["tf2_msgs/msg/TFMessage"]),
              ("/tf_static", ["tf2_msgs/msg/TFMessage"]),
              ("/fake_robot_description", ["std_msgs/msg/String"]),
              ("/robot_description", ["sensor_msgs/msg/Image"])]
    urdf, tf = ros_snapshot._description_topics(topics)
    assert urdf == ["/jo/robot_description"]
    assert tf == ["/jo/tf_static", "/tf_static"]
    assert ros_snapshot.pick_description(
        {"/jo/robot_description": "<a/>"}) == ("/jo/robot_description", "<a/>")
    assert ros_snapshot.pick_description(
        {"/jo/robot_description": "<a/>", "/robot_description": "<r/>"}) == \
        ("/robot_description", "<r/>")
    assert ros_snapshot.pick_description({}) == (None, None)


# -- H2: parameters that never answered ----------------------------------------

SNAP = {"nodes": ["/n"], "topics": [], "captured_at": None,
        "parameters": {"/n": {"/n": {"ros__parameters": {"a": 1}}}},
        "params_missing": [], "params_partial": {"/n": ["b"]},
        "robot_description": None, "tf_static": []}


def test_partial_node_is_incomplete_and_lists_what_is_missing():
    with mock.patch.object(ros_snapshot, "take", return_value=SNAP), \
            mock.patch.object(ros_graph, "list_packages", return_value=["x"]):
        g = ros_graph.harvest()
    assert g["complete"] is False
    assert g["parameters_not_captured"] == {"/n": ["b"]}


def test_diff_does_not_call_a_not_captured_parameter_removed(fairy_dirs):
    from tests.unit.test_archive import _spool
    from ros_fairy.ui import diff
    harvest, context = _spool(fairy_dirs)
    a = builder.build(harvest, context)
    b = a.model_copy(deep=True)
    node = next(iter(a.ros_graph.parameters))
    inner = b.ros_graph.parameters[node][node]["ros__parameters"]
    inner.clear()  # b never got this node's values...
    inner["other"] = 2
    b.ros_graph.parameters_not_captured = {node: ["frame_id"]}
    rows = diff._diff_parameters(a, b)
    assert not any(r[0].endswith(": frame_id") for r in rows)
    assert any(r[0].endswith(": other") for r in rows)


# -- H4: no description publisher ----------------------------------------------

def _pipeline(monkeypatch, graph):
    monkeypatch.setattr(robot_identity, "harvest", lambda: {
        "robot": None, "sensors": [], "calibrations": [],
        "default_license": None})
    monkeypatch.setattr(system_info, "harvest", lambda: {
        "hostname": "r", "kernel": "L", "arch": "x", "ros_distro": "jazzy",
        "apt_ros_versions": {}})
    monkeypatch.setattr(python_env, "harvest", lambda: {"status": "ok"})
    monkeypatch.setattr(hardware_devices, "harvest", lambda: {"status": "ok"})
    monkeypatch.setattr(docker_info, "harvest", lambda: {"available": False})
    monkeypatch.setattr(ros_graph, "harvest", lambda: {
        "captured_at": None, "nodes": ["/n"], "topics": [],
        "ros_packages": [], "parameters": {}, "complete": True, **graph})
    return wd_mod.run_pipeline()


@pytest.mark.parametrize("graph, status", [
    ({"robot_description": None, "tf_static": [],
      "description_publishers": {"robot_description": 0, "tf_static": 0}},
     "absent"),
    ({"robot_description": None, "tf_static": None,
      "description_publishers": {"robot_description": 1, "tf_static": 0}},
     "timeout"),
    ({"robot_description": "<r/>", "tf_static": [],
      "description_publishers": {"robot_description": 1, "tf_static": 0}},
     "ok"),
])
def test_description_status(monkeypatch, graph, status):
    doc = _pipeline(monkeypatch, graph)
    assert doc["provenance"]["harvest_status"]["ros_descriptions"] == status
    assert "description_publishers" not in doc["ros_graph"]


def test_absent_description_is_not_retried():
    dog = wd_mod.Watchdog(inotify=mock.Mock(), scan_recorders=lambda: [],
                          harvest_in_thread=False)
    dog._schedule_retry({"ros_graph": "ok", "ros_descriptions": "absent"})
    assert dog._next_retry is None


# -- H16: image digests --------------------------------------------------------

def test_image_digests_in_one_call():
    out = ('sha256:aaa ["repo/a@sha256:1"]\n'
           'sha256:bbb []\n')  # built locally: no digest
    with mock.patch.object(docker_info, "_run", return_value=out) as run:
        got = docker_info._image_digests(["sha256:aaa", "sha256:bbb",
                                          "sha256:aaa", None])
    assert got == {"sha256:aaa": "repo/a@sha256:1", "sha256:bbb": None}
    run.assert_called_once()


# -- H10 / H17: hardware ---------------------------------------------------------

def test_udev_targets():
    t = hardware_devices._udev_target
    assert t({"device_class": "pci", "device_path": "00:14.0"}) == \
        "--path=/sys/bus/pci/devices/0000:00:14.0"
    assert t({"device_class": "usb", "device_path": None,
              "bus_path": "Bus 001 Device 004"}) == "--name=/dev/bus/usb/001/004"
    assert t({"device_class": "serial", "device_path": "/dev/ttyUSB0"}) == \
        "--name=/dev/ttyUSB0"
    assert t({"device_class": "usb", "device_path": None,
              "bus_path": None}) is None


def _dev(cls, path=None, bus=None):
    return {"device_class": cls, "device_path": path, "bus_path": bus,
            "driver": None, "serial_number": None, "vendor_name": None,
            "vendor_id": None, "product_name": None, "product_id": None,
            "udev_properties": None}


def test_sensors_are_enriched_before_the_chipset(monkeypatch):
    """H10: twenty PCI devices no longer use up the budget before the
    serial port and camera get their serial number and driver."""
    asked = []

    def fake(cmd, timeout=None, deadline=None):
        asked.append(cmd[-1])
        return V(returncode=0, stdout="ID_SERIAL_SHORT=SN1\nID_DRIVER=drv\n")
    monkeypatch.setattr(hardware_devices.shutil, "which", lambda c: c)
    monkeypatch.setattr(hardware_devices, "_run", fake)
    devices = [_dev("pci", f"00:{i:02d}.0") for i in range(20)] + \
        [_dev("video", "/dev/video0"), _dev("serial", "/dev/ttyUSB0")]
    assert hardware_devices._enrich_udev(devices) is True
    assert asked[:2] == ["--name=/dev/ttyUSB0", "--name=/dev/video0"]
    assert all(d["serial_number"] == "SN1" for d in devices)


def test_udev_budget_is_time_boxed(monkeypatch):
    monkeypatch.setattr(hardware_devices.shutil, "which", lambda c: c)
    monkeypatch.setattr(hardware_devices, "UDEV_BUDGET_S", 0)
    devices = [_dev("serial", "/dev/ttyUSB0")]
    assert hardware_devices._enrich_udev(devices) is False


def test_commands_are_skipped_past_the_deadline(monkeypatch):
    monkeypatch.setattr(hardware_devices.shutil, "which", lambda c: c)
    run = mock.Mock()
    monkeypatch.setattr(hardware_devices.subprocess, "run", run)
    assert hardware_devices._run(["lsusb"],
                                 deadline=time.monotonic() - 1) is None
    run.assert_not_called()


def test_dmesg_falls_back_when_the_flags_are_refused(monkeypatch):
    calls = []

    def fake(cmd, timeout=None, deadline=None):
        calls.append(cmd)
        if cmd == ["dmesg"]:
            return V(returncode=0, stdout="usb 1-1: new device\nnoise\n")
        if cmd[0] == "dmesg":
            return V(returncode=1, stdout="", stderr="unknown option")
        return None
    monkeypatch.setattr(hardware_devices, "_run", fake)
    monkeypatch.setattr(hardware_devices, "_enrich_udev", lambda d, dl: True)
    out = hardware_devices.harvest()
    assert out["dmesg_usb"] == "usb 1-1: new device"


# -- H19: robot identity -------------------------------------------------------

GOOD = """\
robot: {name: Jo, platform: Zotac, serial_number: J1}
owner: {organization: Lab, contact_email: a@b.c}
"""


@pytest.mark.parametrize("extra, match", [
    ("", None),
    ("sensors:\n  - just a string\n", "entry 1 of 'sensors'"),
    ("sensors: {gps0: x}\n", "'sensors' must be a list"),
    ("recording: [a, b]\n", "'recording' must be a section"),
    ("recording: {topics: [1, 2]}\n", "list of topic names"),
    ("recording: {storage: [mcap]}\n", "recording.storage"),
])
def test_identity_structure_errors_are_plain(fairy_dirs, extra, match):
    path = fairy_dirs["cfg"] / "robot_identity.yaml"
    path.write_text(GOOD + extra)
    if match is None:
        robot_identity.harvest()
        return
    with pytest.raises(robot_identity.RobotIdentityError, match=match):
        robot_identity.harvest()


def test_identity_robot_as_a_string_is_a_plain_error(fairy_dirs):
    path = fairy_dirs["cfg"] / "robot_identity.yaml"
    path.write_text("robot: Jo\nowner: {organization: L, contact_email: e}\n")
    with pytest.raises(robot_identity.RobotIdentityError,
                       match="'robot' must be a section"):
        robot_identity.harvest()


def test_identity_topics_are_normalised(fairy_dirs):
    path = fairy_dirs["cfg"] / "robot_identity.yaml"
    path.write_text(GOOD + "sensors:\n  - {sensor_id: g, type: gps, "
                    "make_model: F9P, topic: fix}\n"
                    "recording: {topics: imu/data}\n")
    ident = robot_identity.harvest()
    assert ident["sensors"][0]["topic"] == "/fix"
    assert ident["recording"]["topics"] == ["/imu/data"]  # not character-wise


def test_identity_not_utf8_is_a_plain_error(fairy_dirs):
    (fairy_dirs["cfg"] / "robot_identity.yaml").write_bytes(b"robot: \xff\n")
    with pytest.raises(robot_identity.RobotIdentityError, match="can't be read"):
        robot_identity.harvest()


# -- H18: not captured vs none, in the diff --------------------------------------

def test_diff_with_uncaptured_debs_says_so(fairy_dirs):
    from tests.unit.test_archive import _spool
    from ros_fairy.ui import diff
    harvest, context = _spool(fairy_dirs)
    a = builder.build(harvest, context)
    a.software.apt_ros_versions = {"ros-jazzy-rclpy": "7.1.0"}
    b = a.model_copy(deep=True)
    b.software.apt_ros_versions = None
    rows = diff._diff_software(a, b)
    assert ("ROS debs captured", "yes", "no") in rows
    assert not any(r[0] == "ros-jazzy-rclpy" for r in rows)
