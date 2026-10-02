"""Tests for the subprocess-driven harvest modules with mocked subprocess."""

import json
import subprocess
import time
from unittest import mock

import pytest

from ros_fairy.harvest import docker_info, ros_graph, system_info
from ros_fairy.harvest.ros_graph import RosGraphError


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


# --- ros_graph (wraps the single-participant snapshot) -----------------------

from ros_fairy.harvest import ros_snapshot  # noqa: E402

PKG_LIST = "rclpy\nnav2_core\n"
SNAP = {
    "error": None,
    "captured_at": "2026-10-01T15:00:00+00:00",
    "nodes": ["/controller", "/navsat"],
    "topics": [{"name": "/fix", "type": "sensor_msgs/msg/NavSatFix"}],
    "parameters": {"/navsat": {"/navsat": {"ros__parameters": {"rate": 5.0}}},
                   "/controller": {"/controller": {
                       "ros__parameters": {"hz": 20}}}},
    "params_missing": [],
    "robot_description": "<robot/>",
    "tf_static": [{"parent_frame": "base", "child_frame": "gps"}],
}


def _graph(snap=None, pkg=None):
    with mock.patch.object(ros_snapshot, "take", return_value=snap or SNAP), \
            mock.patch("subprocess.run",
                       return_value=pkg or _completed(PKG_LIST)):
        return ros_graph.harvest()


def test_ros_graph_harvest():
    data = _graph()
    assert data["nodes"] == ["/controller", "/navsat"]
    assert {"name": "/fix", "type": "sensor_msgs/msg/NavSatFix"} in data["topics"]
    assert data["ros_packages"] == ["nav2_core", "rclpy"]
    assert data["parameters"]["/navsat"]["/navsat"]["ros__parameters"]["rate"] == 5.0
    assert data["complete"] is True
    assert data["robot_description"] == "<robot/>"
    assert data["tf_static"][0]["child_frame"] == "gps"


def test_ros_graph_snapshot_failure_raises_with_reason():
    with mock.patch.object(ros_snapshot, "take", side_effect=ros_snapshot
                           .SnapshotError("could not join the ROS graph")):
        with pytest.raises(RosGraphError, match="could not join"):
            ros_graph.harvest()


def test_ros_graph_no_nodes_is_a_failure_not_an_empty_capture():
    """An empty graph used to be archived as a complete capture (2026-10-01);
    a live recording always has at least the recorder node."""
    with pytest.raises(RosGraphError, match="no ROS nodes visible"):
        _graph({**SNAP, "nodes": [], "parameters": {}})


def test_ros_graph_missing_parameters_degrade_and_are_logged():
    import logging

    class Collect(logging.Handler):
        def __init__(self):
            super().__init__(logging.INFO)
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    snap = {**SNAP, "parameters": {"/navsat": SNAP["parameters"]["/navsat"]},
            "params_missing": ["/controller"]}
    logger = logging.getLogger("ros_fairy.harvest.ros_graph")
    collect, old_level = Collect(), logger.level
    logger.addHandler(collect)
    logger.setLevel(logging.INFO)
    try:
        data = _graph(snap)
    finally:
        logger.removeHandler(collect)
        logger.setLevel(old_level)
    assert data["complete"] is False
    assert "parameters not captured for 1 of 2 node(s): /controller" \
        in collect.lines


def test_ros_graph_package_list_failure_is_not_captured_not_fatal():
    data = _graph(pkg=_completed("", returncode=1, stderr="boom"))
    assert data["ros_packages"] is None
    assert data["nodes"] == ["/controller", "/navsat"]


def test_ros_graph_list_nodes_asks_for_a_nodes_only_snapshot():
    with mock.patch.object(ros_snapshot, "take", return_value=SNAP) as take:
        assert ros_graph.list_nodes() == ["/controller", "/navsat"]
    take.assert_called_once_with(nodes_only=True)


def test_ros_graph_package_list_timeout():
    with mock.patch("subprocess.run",
                    side_effect=subprocess.TimeoutExpired("ros2", 20)):
        with pytest.raises(RosGraphError, match="timed out"):
            ros_graph.list_packages()


# --- ros_snapshot (the child process boundary) --------------------------------

def test_snapshot_child_opts_out_of_cyclone_participant_slots(monkeypatch):
    """The robot can use every CycloneDDS participant slot; ros-fairy's own
    participant must not need one, and must not touch the robot's config."""
    monkeypatch.delenv("CYCLONEDDS_URI", raising=False)
    assert ros_snapshot.child_env()["CYCLONEDDS_URI"] == \
        ros_snapshot.CYCLONE_NO_INDEX
    monkeypatch.setenv("CYCLONEDDS_URI", "file:///robot/cyclonedds.xml")
    uri = ros_snapshot.child_env()["CYCLONEDDS_URI"]
    assert uri == "file:///robot/cyclonedds.xml," + ros_snapshot.CYCLONE_NO_INDEX
    import os
    assert os.environ["CYCLONEDDS_URI"] == "file:///robot/cyclonedds.xml"


def test_snapshot_take_parses_child_output():
    out = "some rcl warning on stdout\n" + json.dumps(SNAP) + "\n"
    with mock.patch("subprocess.run", return_value=_completed(out)) as run:
        assert ros_snapshot.take(nodes_only=True)["nodes"] == SNAP["nodes"]
    cmd = run.call_args.args[0]
    assert cmd[1:3] == ["-m", "ros_fairy.harvest.ros_snapshot"]
    assert "--nodes-only" in cmd
    assert "ParticipantIndex" in run.call_args.kwargs["env"]["CYCLONEDDS_URI"]


@pytest.mark.parametrize("result, match", [
    (_completed(json.dumps({"error": "could not join the ROS graph: x"})),
     "could not join"),
    (_completed("", returncode=1, stderr="Segmentation fault"),
     "Segmentation fault"),
])
def test_snapshot_take_failures(result, match):
    with mock.patch("subprocess.run", return_value=result):
        with pytest.raises(ros_snapshot.SnapshotError, match=match):
            ros_snapshot.take()


def test_snapshot_take_kills_a_hung_child():
    with mock.patch("subprocess.run",
                    side_effect=subprocess.TimeoutExpired("python3", 48)):
        with pytest.raises(ros_snapshot.SnapshotError, match="timed out"):
            ros_snapshot.take()


def test_snapshot_nests_dotted_names_like_ros2_param_dump():
    assert ros_snapshot.nest({"rate": 5, "qos.depth": 1, "qos.history": "x",
                              "a.b.c": True}) == {
        "rate": 5, "qos": {"depth": 1, "history": "x"},
        "a": {"b": {"c": True}}}


def test_snapshot_records_unset_parameters_as_none():
    """robot_localization declares unused slots (pose0, twist0...) without a
    value; one such name made rclcpp reject the whole batch get, losing every
    EKF parameter (2026-10-02). Unset is recorded, not dropped."""
    names = ["frequency", "pose0", "odom0"]
    assert ros_snapshot.complete_values(names, {"frequency": 30.1,
                                                "odom0": "/odom"}) == {
        "frequency": 30.1, "pose0": None, "odom0": "/odom"}


def test_snapshot_merges_every_tf_static_publisher():
    """Each /tf_static publisher (robot_state_publisher, every camera driver)
    sends its own latched message; keeping only the last made each mission
    capture a different publisher's frames (2026-10-02)."""
    def tf(parent, child, x=0.0):
        return {"parent_frame": parent, "child_frame": child,
                "translation": {"x": x, "y": 0.0, "z": 0.0},
                "rotation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}}
    acc = {}
    ros_snapshot.merge_transforms(acc, [tf("base_link", "velodyne"),
                                        tf("base_link", "imu_link")])
    ros_snapshot.merge_transforms(acc, [tf("front_camera_link",
                                           "front_camera_color_frame")])
    ros_snapshot.merge_transforms(acc, [tf("base_link", "velodyne", x=0.2)])
    assert sorted(c for _, c in acc) == ["front_camera_color_frame",
                                         "imu_link", "velodyne"]
    assert acc[("base_link", "velodyne")]["translation"]["x"] == 0.2


def test_snapshot_parameter_values():
    from types import SimpleNamespace as V
    assert ros_snapshot._value(V(type=1, bool_value=True)) is True
    assert ros_snapshot._value(V(type=3, double_value=2.5)) == 2.5
    assert ros_snapshot._value(V(type=5, byte_array_value=[b"\x01", 2])) \
        == [1, 2]
    assert ros_snapshot._value(V(type=9, string_array_value=["a"])) == ["a"]
    assert ros_snapshot._value(V(type=0)) is None


def test_snapshot_hidden_nodes():
    assert ros_snapshot.is_hidden("/_ros2cli_24980")
    assert ros_snapshot.is_hidden("/ns/_private")
    assert not ros_snapshot.is_hidden("/bt_navigator")
    assert ros_snapshot.TF_LISTENER_NODE.search(
        "/visodom/transform_listener_impl_592e0b6f7070")


# --- docker_info -------------------------------------------------------------

INSPECT = [{
    "Name": "/navstack",
    "Image": "sha256:abc",
    "Config": {
        "Image": "example/navstack:1.4.2",
        "Labels": {
            "com.docker.compose.project": "robot",
            "com.docker.compose.project.config_files": "/opt/robot/compose.yml",
        },
    },
}]


def test_docker_harvest():
    def fake_run(cmd, **kw):
        if cmd[1] == "ps":
            return _completed("c0ffee\n")
        if "--format" in cmd:
            return _completed('["example/navstack@sha256:7be1"]')
        return _completed(json.dumps(INSPECT))

    with mock.patch("subprocess.run", side_effect=fake_run):
        data = docker_info.harvest()

    assert data["available"] is True
    c = data["docker_containers"][0]
    assert c["name"] == "navstack"
    assert c["image"] == "example/navstack:1.4.2"
    assert c["digest"] == "example/navstack@sha256:7be1"
    assert c["compose_project"] == "robot"
    assert c["compose_file"] == "/opt/robot/compose.yml"


def test_docker_absent():
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        data = docker_info.harvest()
    assert data == {"docker_containers": [], "raw_inspect": [],
                    "available": False}


def test_docker_no_containers():
    with mock.patch("subprocess.run", return_value=_completed("\n")):
        data = docker_info.harvest()
    assert data["available"] is True
    assert data["docker_containers"] == []


RUNNING_INSPECT = [{
    "Id": "c0ffee",
    "Name": "/navstack",
    "State": {"Running": True},
    "Config": {
        "Image": "example/navstack:1.4.2",
        "Labels": {
            "com.docker.compose.project": "robot",
            "com.docker.compose.project.config_files": "/opt/robot/compose.yml",
        },
    },
}]


def test_docker_probes_running_container_for_ros_packages():
    """A robot whose ROS stack lives entirely in a container (nothing on
    the host) should still get a real package list — probed inside the
    container, not the host's mostly-empty one."""
    def fake_run(cmd, **kw):
        if cmd[1] == "ps":
            return _completed("c0ffee\n")
        if cmd[1] == "exec":
            assert cmd[2] == "c0ffee"
            assert cmd[3:] == ["ros2", "pkg", "list"]
            return _completed("nav2_bringup\nnav2_msgs\n")
        if "--format" in cmd:
            return _completed('["example/navstack@sha256:7be1"]')
        return _completed(json.dumps(RUNNING_INSPECT))

    with mock.patch("subprocess.run", side_effect=fake_run):
        data = docker_info.harvest()

    assert data["docker_containers"][0]["ros_packages"] == [
        "nav2_bringup", "nav2_msgs"]


def test_docker_falls_back_to_interactive_shell_for_ros_packages():
    """Many hand-rolled robot images only source ROS from ~/.bashrc, so a
    bare `docker exec ... ros2 pkg list` finds nothing — fall back to a
    login+interactive shell, same as an operator attaching manually would
    get."""
    def fake_run(cmd, **kw):
        if cmd[1] == "ps":
            return _completed("c0ffee\n")
        if cmd[1] == "exec":
            if "bash" in cmd:
                assert cmd[-1] == "ros2 pkg list"
                return _completed("nav2_bringup\n")
            return _completed("", returncode=127, stderr="ros2: not found")
        if "--format" in cmd:
            return _completed('["example/navstack@sha256:7be1"]')
        return _completed(json.dumps(RUNNING_INSPECT))

    with mock.patch("subprocess.run", side_effect=fake_run):
        data = docker_info.harvest()

    assert data["docker_containers"][0]["ros_packages"] == ["nav2_bringup"]


def test_docker_ros_packages_none_when_container_has_no_ros():
    def fake_run(cmd, **kw):
        if cmd[1] == "ps":
            return _completed("c0ffee\n")
        if cmd[1] == "exec":
            return _completed("", returncode=127, stderr="not found")
        if "--format" in cmd:
            return _completed('["example/navstack@sha256:7be1"]')
        return _completed(json.dumps(RUNNING_INSPECT))

    with mock.patch("subprocess.run", side_effect=fake_run):
        data = docker_info.harvest()

    assert data["docker_containers"][0]["ros_packages"] is None


def test_docker_does_not_exec_into_a_stopped_container():
    def fake_run(cmd, **kw):
        if cmd[1] == "ps":
            return _completed("c0ffee\n")
        if cmd[1] == "exec":
            raise AssertionError("must not exec into a stopped container")
        if "--format" in cmd:
            return _completed('["example/navstack@sha256:7be1"]')
        return _completed(json.dumps(INSPECT))  # no "State" key at all

    with mock.patch("subprocess.run", side_effect=fake_run):
        data = docker_info.harvest()

    assert data["docker_containers"][0]["ros_packages"] is None


# --- system_info -------------------------------------------------------------

def test_system_info(monkeypatch):
    monkeypatch.setenv("ROS_DISTRO", "jazzy")
    dpkg = _completed("ros-jazzy-rclpy 7.1.0\nros-jazzy-nav2 1.3.0\n")
    with mock.patch("subprocess.run", return_value=dpkg):
        data = system_info.harvest()
    assert data["ros_distro"] == "jazzy"
    assert data["apt_ros_versions"]["ros-jazzy-rclpy"] == "7.1.0"
    assert data["hostname"]
    assert data["kernel"].startswith("Linux")
    assert data["arch"]


def test_system_info_no_dpkg(monkeypatch):
    monkeypatch.delenv("ROS_DISTRO", raising=False)
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        data = system_info.harvest()
    assert data["ros_distro"] is None
    assert data["apt_ros_versions"] == {}


def test_system_info_records_clock_sync():
    dpkg = _completed("ros-jazzy-rclpy 7.1.0\n")
    with mock.patch("subprocess.run", return_value=dpkg), \
            mock.patch.object(system_info.clock, "is_synchronized",
                              return_value=False):
        data = system_info.harvest()
    # captured regardless of outcome so the archive shows the check ran (#27)
    assert data["clock_synchronized"] is False
