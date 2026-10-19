"""The watchdog's scan of the containers' ROS processes, and how doctor and
setup --from-container use it."""

import io
import json
import os

import pytest
from rich.console import Console

from fairy.harvest import docker_info
from fairy.subcommands import doctor
from fairy.subcommands import setup as setup_cmd
from fairy.utils import paths, ros_env
from fairy.watchdog import ros_processes
from tests.unit.test_containers import (
    ROS,
    _inspect,
    _service,
    docker,  # noqa: F401 (a fixture)
)

HOST_NS, NAV_NS, CAM_NS = "pid:[1]", "pid:[2]", "pid:[3]"


@pytest.fixture
def proc(tmp_path, monkeypatch):
    """A fabricated /proc: add(pid, ns, inner, env, cmd)."""
    root = tmp_path / "proc"
    root.mkdir()
    monkeypatch.setattr(ros_processes, "PROC", root)

    def add(pid, ns, inner, env=None, cmd="node", readable=True):
        d = root / str(pid)
        (d / "ns").mkdir(parents=True)
        os.symlink(ns, d / "ns" / "pid")
        (d / "status").write_text(f"Name:\tx\nNSpid:\t{pid}\t{inner}\n"
                                  if inner != pid else f"NSpid:\t{pid}\n")
        (d / "cmdline").write_bytes(cmd.encode() + b"\0")
        if readable:
            (d / "environ").write_bytes(b"\0".join(
                f"{k}={v}".encode() for k, v in (env or {}).items()))
    (root / "self" / "ns").mkdir(parents=True)
    os.symlink(HOST_NS, root / "self" / "ns" / "pid")
    return add


def test_scan_finds_what_start_up_scripts_exported(proc):
    proc(1, HOST_NS, 1, {"ROS_DISTRO": "jazzy", "ROS_DOMAIN_ID": "3"})
    proc(500, NAV_NS, 1, {"PATH": "/bin"}, cmd="/entrypoint.sh")
    proc(510, NAV_NS, 7, {**ROS, "ROS_DOMAIN_ID": "7", "SECRET": "x"})
    proc(511, NAV_NS, 8, {**ROS, "ROS_DOMAIN_ID": "7"})
    proc(512, NAV_NS, 9, {**ROS, "ROS_DOMAIN_ID": "0"},
         cmd="/usr/bin/python3 _ros2_daemon")
    proc(600, CAM_NS, 1, {"PATH": "/bin"})            # no ROS in it
    assert ros_processes.scan() == [{
        "pid": 500, "processes": 2,
        "settings": [{"ROS_DISTRO": "jazzy", "ROS_DOMAIN_ID": "7"}]}]


def test_scan_keeps_disagreeing_settings_and_skips_unreadable(proc):
    proc(500, NAV_NS, 1, {**ROS, "ROS_DOMAIN_ID": "7"})
    proc(501, NAV_NS, 2, {**ROS})
    proc(502, NAV_NS, 3, readable=False)
    [found] = ros_processes.scan()
    assert found["processes"] == 2
    assert {"ROS_DISTRO": "jazzy"} in found["settings"]
    assert {"ROS_DISTRO": "jazzy", "ROS_DOMAIN_ID": "7"} in found["settings"]


def test_scan_of_an_empty_proc():
    assert ros_processes.scan() == []  # conftest's empty folder


def test_written_only_when_it_changes(fairy_dirs):
    path = paths.ros_processes_path()
    found = [{"pid": 5, "processes": 1, "settings": [{"ROS_DISTRO": "j"}]}]
    assert ros_processes.write_if_changed(found)
    since = json.loads(path.read_text())["since"]
    assert not ros_processes.write_if_changed(found)
    assert json.loads(path.read_text())["since"] == since
    assert path.stat().st_mode & 0o777 == 0o644
    assert ros_processes.read() == {5: [{"ROS_DISTRO": "j"}]}
    assert ros_processes.write_if_changed([])
    assert ros_processes.read() == {}


def test_a_changed_process_count_alone_is_not_rewritten(fairy_dirs):
    found = [{"pid": 5, "processes": 1, "settings": [{"ROS_DISTRO": "j"}]}]
    assert ros_processes.write_if_changed(found)
    assert not ros_processes.write_if_changed([{**found[0], "processes": 4}])
    assert ros_processes.write_if_changed(
        [{**found[0], "settings": [{"ROS_DISTRO": "k"}]}])


def test_read_without_or_with_a_broken_file(fairy_dirs):
    assert ros_processes.read() == {}
    paths.ros_processes_path().write_text("{not json")
    assert ros_processes.read() == {}


def test_the_watchdog_scans_at_once_then_every_ten_seconds(fairy_dirs):
    from fairy.watchdog.watchdog import Watchdog
    from tests.unit.test_watchdog import (
        FakeClock,
        FakeINotify,
        good_pipeline,
        no_recorders,
    )
    calls = []

    def scan():
        calls.append(1)
        return [{"pid": 9, "processes": 1, "settings": [ROS]}]
    clock = FakeClock()
    dog = Watchdog(inotify=FakeINotify(), clock=clock, pipeline=good_pipeline,
                   harvest_in_thread=False, scan_recorders=no_recorders,
                   scan_ros_processes=scan)
    dog.start()
    dog.step(0)
    assert len(calls) == 1 and ros_processes.read() == {9: [ROS]}
    clock.now += 5
    dog.step(0)
    assert len(calls) == 1
    clock.now += 6
    dog.step(0)
    assert len(calls) == 2


# --- what doctor and setup make of it ------------------------------------------

def _with_pid(entry: dict, pid: int) -> dict:
    return {**entry, "State": {"Pid": pid}}


def test_processes_win_over_the_configuration(docker):  # noqa: F811
    docker.containers = [
        _with_pid(_inspect("navstack", {**ROS, "ROS_DOMAIN_ID": "0",
                                        "ROS_STATIC_PEERS": "10.0.0.2"}), 500),
        # No ROS_DISTRO in its configuration: ROS is sourced by a script.
        _with_pid(_inspect("handmade", {"PATH": "/bin"}), 600),
        _with_pid(_inspect("db", {"PATH": "/bin"}), 700),
    ]
    running = {500: [{**ROS, "ROS_DOMAIN_ID": "7"}],
               600: [{"ROS_DISTRO": "jazzy"}]}
    found = {c["name"]: c for c in docker_info.ros_containers(running=running)}
    assert set(found) == {"navstack", "handmade"}
    nav = found["navstack"]
    assert nav["source"] == "processes" and nav["env"]["ROS_DOMAIN_ID"] == "7"
    assert "ROS_STATIC_PEERS" not in nav["env"]   # the processes don't set it
    assert nav["env"]["PATH"] == ROS["PATH"]       # the rest stays
    assert found["handmade"]["source"] == "processes"


def test_doctor_sees_the_entrypoint_export(fairy_dirs, docker):  # noqa: F811
    _service({})
    docker.containers = [_with_pid(_inspect("navstack", ROS), 500)]
    assert doctor._check_container_env()["status"] == doctor.OK
    ros_processes.write_if_changed(
        [{"pid": 500, "processes": 3,
          "settings": [{**ROS, "ROS_DOMAIN_ID": "7"}]}])
    result = doctor._check_container_env()
    assert result["status"] == doctor.WARN
    assert "navstack (as its ROS processes run): ROS_DOMAIN_ID: 0 → 7" in \
        result["detail"]
    assert "--from-container navstack" in result["hint"]


def test_doctor_when_a_containers_nodes_disagree(fairy_dirs,
                                                docker):  # noqa: F811
    _service({})
    docker.containers = [_with_pid(_inspect("navstack", ROS), 500)]
    ros_processes.write_if_changed([{"pid": 500, "processes": 2, "settings": [
        {"ROS_DISTRO": "jazzy"}, {"ROS_DISTRO": "jazzy",
                                  "ROS_DOMAIN_ID": "7"}]}])
    result = doctor._check_container_env()
    assert result["title"] == \
        "A ROS container's nodes are on different ROS partitions"
    assert "all defaults / ROS_DOMAIN_ID=7" in result["detail"]
    assert "from-container" not in result["hint"]


def test_from_container_uses_the_processes(fairy_dirs, docker,  # noqa: F811
                                           monkeypatch):
    docker.containers = [_with_pid(_inspect("navstack", ROS), 500)]
    ros_processes.write_if_changed(
        [{"pid": 500, "processes": 1,
          "settings": [{**ROS, "ROS_DOMAIN_ID": "7"}]}])
    monkeypatch.delenv("ROS_DOMAIN_ID", raising=False)
    console = Console(file=io.StringIO(), width=200)
    assert setup_cmd._adopt_container_discovery(console, "navstack")
    assert os.environ["ROS_DOMAIN_ID"] == "7"
    assert "navstack (as its ROS processes run): ROS_DOMAIN_ID=7" in \
        console.file.getvalue()


def test_from_container_refuses_when_they_disagree(fairy_dirs,
                                                   docker):  # noqa: F811
    docker.containers = [_with_pid(_inspect("navstack", ROS), 500)]
    ros_processes.write_if_changed([{"pid": 500, "processes": 2, "settings": [
        {"ROS_DISTRO": "jazzy"}, {"ROS_DISTRO": "jazzy",
                                  "ROS_DOMAIN_ID": "7"}]}])
    console = Console(file=io.StringIO(), width=200)
    assert not setup_cmd._adopt_container_discovery(console, "navstack")
    assert "don't all use the same DDS settings" in console.file.getvalue()


def test_no_secrets_leave_the_scan(proc):
    proc(500, NAV_NS, 1, {**ROS, "AWS_SECRET": "s3cr3t", "PATH": "/x"})
    [found] = ros_processes.scan()
    assert set(found["settings"][0]) <= {"ROS_DISTRO",
                                         *ros_env.SESSION_ADOPT_KEYS}


def test_doctor_without_docker_still_compares(fairy_dirs, monkeypatch):
    """An operator outside the docker group: the watchdog's findings, by
    pid; with none, skipped as before."""
    _service({})
    assert doctor._check_container_env()["status"] == doctor.SKIP
    ros_processes.write_if_changed(
        [{"pid": 500, "processes": 1,
          "settings": [{**ROS, "ROS_DOMAIN_ID": "7"}]}])
    result = doctor._check_container_env()
    assert result["status"] == doctor.WARN
    assert "the container whose first process is 500 (as its ROS processes " \
        "run): ROS_DOMAIN_ID: 0 → 7" in result["detail"]
    assert "docker ps" in result["hint"]
    _service({"ROS_DOMAIN_ID": "7"})
    assert doctor._check_container_env()["status"] == doctor.OK


def test_a_stated_default_is_no_disagreement(proc):
    # SUBNET is what an unset ROS_AUTOMATIC_DISCOVERY_RANGE means on Jazzy.
    proc(500, NAV_NS, 1, {**ROS, "ROS_AUTOMATIC_DISCOVERY_RANGE": "SUBNET"})
    proc(501, NAV_NS, 2, {**ROS})
    [found] = ros_processes.scan()
    assert found["settings"] == [{"ROS_DISTRO": "jazzy"}]


def test_doctor_sums_up_many_disagreeing_settings(fairy_dirs,
                                                  docker):  # noqa: F811
    # A benchmark running ten simulations, one ROS domain each.
    _service({})
    docker.containers = [_with_pid(_inspect("bench", ROS), 500)]
    cyclone = {"ROS_DISTRO": "jazzy", "RMW_IMPLEMENTATION":
               "rmw_cyclonedds_cpp"}
    settings = [{**cyclone, "ROS_DOMAIN_ID": str(d),
                 "ROS_AUTOMATIC_DISCOVERY_RANGE": "LOCALHOST"}
                for d in range(150, 160)] + [{**cyclone,
                                              "ROS_DOMAIN_ID": "42"}]
    ros_processes.write_if_changed(
        [{"pid": 500, "processes": 11, "settings": settings}])
    result = doctor._check_container_env()
    assert "bench's ROS processes don't agree: 11 different sets: " \
        "ROS_DOMAIN_ID 42, 150–159; " \
        "RMW_IMPLEMENTATION=rmw_cyclonedds_cpp; " \
        "ROS_AUTOMATIC_DISCOVERY_RANGE LOCALHOST or unset" in result["detail"]
    assert "on purpose" in result["hint"]
