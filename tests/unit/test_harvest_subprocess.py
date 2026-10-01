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


# --- ros_graph ---------------------------------------------------------------

NODE_LIST = "/navsat\n/controller\n"
TOPIC_LIST = "/fix [sensor_msgs/msg/NavSatFix]\n/depth [ping_msgs/msg/Ping]\n"
PKG_LIST = "rclpy\nnav2_core\n"
PARAM_DUMP = "/navsat:\n  ros__parameters:\n    rate: 5.0\n"


def test_ros_graph_harvest():
    def fake_run(cmd, **kw):
        out = {"node": NODE_LIST, "topic": TOPIC_LIST,
               "pkg": PKG_LIST, "param": PARAM_DUMP}[cmd[1]]
        return _completed(out)

    with mock.patch("subprocess.run", side_effect=fake_run):
        data = ros_graph.harvest()

    assert data["nodes"] == ["/controller", "/navsat"]
    assert {"name": "/fix", "type": "sensor_msgs/msg/NavSatFix"} in data["topics"]
    assert data["ros_packages"] == ["nav2_core", "rclpy"]
    assert data["parameters"]["/navsat"]["/navsat"]["ros__parameters"]["rate"] == 5.0
    assert data["complete"] is True
    assert data["captured_at"]


def test_ros_graph_ros_down():
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(RosGraphError, match="not found"):
            ros_graph.harvest()


def test_ros_graph_no_nodes_is_a_failure_not_an_empty_capture():
    """`ros2 node list` exits 0 with no output when discovery can't reach the
    robot; that was archived as a complete, empty graph (2026-10-01)."""
    def fake_run(cmd, **kw):
        return _completed({"node": "", "topic": "/rosout [rcl_interfaces/msg/"
                           "Log]\n", "pkg": PKG_LIST}[cmd[1]])

    with mock.patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(RosGraphError, match="no ROS nodes visible"):
            ros_graph.harvest()


def test_ros_graph_listings_bypass_the_ros2_daemon():
    """The daemon is chosen by domain ID only and may run with someone else's
    discovery settings; listings must use the watchdog's adopted env."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _completed({"node": NODE_LIST, "topic": TOPIC_LIST,
                           "pkg": PKG_LIST, "param": PARAM_DUMP}[cmd[1]])

    with mock.patch("subprocess.run", side_effect=fake_run):
        ros_graph.harvest()
    listings = [c for c in calls if c[1] in ("node", "topic")]
    assert len(listings) == 2
    assert all("--no-daemon" in c and "--spin-time" in c for c in listings)


def test_ros_graph_param_dump_failure_degrades():
    def fake_run(cmd, **kw):
        if cmd[1] == "param":
            return _completed("", returncode=1, stderr="boom")
        return _completed({"node": NODE_LIST, "topic": TOPIC_LIST,
                           "pkg": PKG_LIST}[cmd[1]])

    with mock.patch("subprocess.run", side_effect=fake_run):
        data = ros_graph.harvest()
    assert data["complete"] is False
    assert data["parameters"] == {}


def test_ros_graph_logs_which_nodes_lack_parameters():
    """Per-node failures were debug-only, so they never reached the journal
    or the archived watchdog.log."""
    import logging

    class Collect(logging.Handler):
        def __init__(self):
            super().__init__(logging.INFO)
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    def fake_run(cmd, **kw):
        if cmd[1] == "param" and cmd[3] == "/controller":
            return _completed("", returncode=1, stderr="timed out")
        return _completed({"node": NODE_LIST, "topic": TOPIC_LIST,
                           "pkg": PKG_LIST, "param": PARAM_DUMP}[cmd[1]])

    logger = logging.getLogger("ros_fairy.harvest.ros_graph")
    collect, old_level = Collect(), logger.level
    logger.addHandler(collect)
    logger.setLevel(logging.INFO)
    try:
        with mock.patch("subprocess.run", side_effect=fake_run):
            ros_graph.harvest()
    finally:
        logger.removeHandler(collect)
        logger.setLevel(old_level)
    assert "parameters not captured for 1 of 2 node(s): /controller" \
        in collect.lines


def test_ros_graph_timeout():
    with mock.patch("subprocess.run",
                    side_effect=subprocess.TimeoutExpired("ros2", 20)):
        with pytest.raises(RosGraphError, match="timed out"):
            ros_graph.list_nodes()


def test_ros_graph_skips_tf_listener_nodes():
    # tf2's TransformListener never declares parameters; dumping it wastes
    # a timeout for nothing, so it should never even be attempted.
    nodes_out = "/navsat\n/transform_listener_impl_5c1d50edeb00\n"
    dumped = []

    def fake_run(cmd, **kw):
        if cmd[1] == "param":
            dumped.append(cmd[3])
            return _completed(PARAM_DUMP)
        return _completed({"node": nodes_out, "topic": TOPIC_LIST,
                           "pkg": PKG_LIST}[cmd[1]])

    with mock.patch("subprocess.run", side_effect=fake_run):
        data = ros_graph.harvest()

    assert data["nodes"] == [
        "/navsat", "/transform_listener_impl_5c1d50edeb00"]
    assert dumped == ["/navsat"]
    assert data["complete"] is True


def test_ros_graph_slow_node_does_not_starve_others(monkeypatch):
    # Regression test: param dumps used to run one at a time against a
    # shared budget, so one unresponsive node (sorted first, here) used to
    # exhaust the whole budget and leave every node sorted after it
    # unattempted. They now run concurrently, so a slow node only costs its
    # own slot.
    monkeypatch.setattr(ros_graph, "PARAM_DUMP_BUDGET_S", 0.3)
    nodes_out = "/aaa_slow\n/zzz_fast\n"

    def fake_run(cmd, **kw):
        if cmd[1] == "param":
            if cmd[3] == "/aaa_slow":
                time.sleep(1.0)
            return _completed(PARAM_DUMP)
        return _completed({"node": nodes_out, "topic": TOPIC_LIST,
                           "pkg": PKG_LIST}[cmd[1]])

    with mock.patch("subprocess.run", side_effect=fake_run):
        started = time.monotonic()
        data = ros_graph.harvest()
        elapsed = time.monotonic() - started

    assert data["complete"] is False
    assert "/zzz_fast" in data["parameters"]
    assert "/aaa_slow" not in data["parameters"]
    assert elapsed < 1.0  # didn't block on the slow node to return


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
