"""The robot's ROS containers against the recording assistant: doctor's check
and setup --refresh-env --from-container."""

import io
import json
import os
from types import SimpleNamespace
from unittest import mock

import pytest
from rich.console import Console

from fairy.harvest import docker_info
from fairy.subcommands import doctor
from fairy.subcommands import setup as setup_cmd
from fairy.utils import paths, ros_env


def _inspect(name, env, network="host"):
    return {"Name": f"/{name}",
            "Config": {"Env": [f"{k}={v}" for k, v in env.items()]},
            "HostConfig": {"NetworkMode": network}}


REAL_ROS_CONTAINERS = docker_info.ros_containers


@pytest.fixture
def docker(monkeypatch):
    """A fake `docker` CLI: set .containers, .ps_error."""
    monkeypatch.setattr(docker_info, "ros_containers", REAL_ROS_CONTAINERS)
    state = SimpleNamespace(containers=[], ps_error=None, calls=[])

    def run(cmd, **kwargs):
        state.calls.append(cmd)
        if cmd[1] == "ps":
            if state.ps_error:
                return SimpleNamespace(returncode=1, stdout="",
                                       stderr=state.ps_error)
            return SimpleNamespace(returncode=0, stderr="", stdout="".join(
                f"id{i}\n" for i in range(len(state.containers))))
        return SimpleNamespace(returncode=0, stderr="",
                               stdout=json.dumps(state.containers))
    monkeypatch.setattr(docker_info.subprocess, "run", run)
    return state


ROS = {"ROS_DISTRO": "jazzy", "PATH": "/opt/ros/jazzy/bin"}


def test_ros_containers_keeps_only_ros_ones(docker):
    docker.containers = [
        _inspect("navstack", {**ROS, "ROS_DOMAIN_ID": "7"}),
        _inspect("db", {"PATH": "/usr/bin"}),
        _inspect("cam", ROS, network="bridge"),
    ]
    found = docker_info.ros_containers()
    assert [c["name"] for c in found] == ["cam", "navstack"]
    assert found[1]["env"]["ROS_DOMAIN_ID"] == "7"
    assert found[0]["network"] == "bridge"


@pytest.mark.parametrize("error, says", [
    ("permission denied while trying to connect to the Docker daemon",
     "docker group"),
    ("Cannot connect to the Docker daemon. Is the docker daemon running?",
     "Cannot connect"),
])
def test_ros_containers_says_why_docker_cant_be_asked(docker, error, says):
    docker.ps_error = error
    with pytest.raises(docker_info.DockerUnavailable, match=says):
        docker_info.ros_containers()


def test_ros_containers_without_docker(monkeypatch):
    monkeypatch.setattr(docker_info, "ros_containers", REAL_ROS_CONTAINERS)

    def missing(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(docker_info.subprocess, "run", missing)
    with pytest.raises(docker_info.DockerUnavailable, match="isn't installed"):
        docker_info.ros_containers()


def test_discovery_differences():
    service = {"ROS_DOMAIN_ID": "0", "RMW_IMPLEMENTATION": "rmw_fastrtps_cpp"}
    # Unset means Jazzy's default: domain 0, Fast DDS, SUBNET.
    assert ros_env.discovery_differences(service, {
        "ROS_AUTOMATIC_DISCOVERY_RANGE": "SUBNET"}) == []
    assert ros_env.discovery_differences(service, {
        "ROS_DOMAIN_ID": "7", "ROS_STATIC_PEERS": "10.0.0.2",
        "RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp"}) == [
        "ROS_DOMAIN_ID: 0 → 7",
        "RMW_IMPLEMENTATION: rmw_fastrtps_cpp → rmw_cyclonedds_cpp",
        "ROS_STATIC_PEERS: unset → 10.0.0.2"]


# --- doctor ------------------------------------------------------------------

def _service(env):
    ros_env.write_file(paths.watchdog_env_path(), {**ROS, **env})


def test_doctor_container_matches(fairy_dirs, docker):
    _service({"ROS_DOMAIN_ID": "7"})
    docker.containers = [_inspect("navstack", {**ROS, "ROS_DOMAIN_ID": "7"})]
    result = doctor._check_container_env()
    assert result["status"] == doctor.OK and result["detail"] == "navstack"


def test_doctor_container_on_another_domain(fairy_dirs, docker):
    _service({})
    docker.containers = [_inspect("navstack", {**ROS, "ROS_DOMAIN_ID": "7"})]
    result = doctor._check_container_env()
    assert result["status"] == doctor.WARN
    assert "navstack: ROS_DOMAIN_ID: 0 → 7" in result["detail"]
    assert "--refresh-env --from-container navstack" in result["hint"]


def test_doctor_container_on_its_own_network(fairy_dirs, docker):
    _service({})
    docker.containers = [_inspect("cam", ROS, network="bridge")]
    result = doctor._check_container_env()
    assert result["status"] == doctor.WARN
    assert "own Docker network (bridge)" in result["detail"]
    assert "network_mode: host" in result["hint"]


@pytest.mark.parametrize("containers, error, title", [
    ([], None, "No ROS containers running"),
    ([], "permission denied", "ROS containers not checked"),
])
def test_doctor_container_skips(fairy_dirs, docker, containers, error,
                                title):
    docker.containers, docker.ps_error = containers, error
    result = doctor._check_container_env()
    assert result["status"] == doctor.SKIP and result["title"] == title


# --- setup --from-container ----------------------------------------------------

def _adopt(docker, name, container_env, network="host", shell=None):
    docker.containers = [_inspect("navstack", {**ROS, **container_env},
                                  network)]
    console = Console(file=io.StringIO(), width=200)
    fairy = {k: v for k, v in os.environ.items() if k.startswith("ROS_FAIRY_")}
    with mock.patch.dict(os.environ, {**fairy, **(shell or {})}, clear=True):
        ok = setup_cmd._adopt_container_discovery(console, name)
        env = dict(os.environ)
    return ok, env, console.file.getvalue()


def test_from_container_takes_its_discovery_settings(docker):
    ok, env, out = _adopt(
        docker, "navstack",
        {"ROS_DOMAIN_ID": "7", "RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp"},
        shell={"ROS_DOMAIN_ID": "3", "ROS_STATIC_PEERS": "10.0.0.9",
               "AMENT_PREFIX_PATH": "/opt/ros/jazzy"})
    assert ok
    assert env["ROS_DOMAIN_ID"] == "7"
    assert env["RMW_IMPLEMENTATION"] == "rmw_cyclonedds_cpp"
    assert "ROS_STATIC_PEERS" not in env  # the container uses the default
    assert env["AMENT_PREFIX_PATH"] == "/opt/ros/jazzy"  # still this shell's
    assert "ROS_DOMAIN_ID=7" in out


def test_from_container_warns_about_profiles_and_networks(docker):
    ok, _, out = _adopt(docker, "navstack",
                        {"CYCLONEDDS_URI": "file:///cfg/dds.xml"},
                        network="bridge")
    assert ok
    assert "path is inside the container" in out
    assert "own Docker network" in out


def test_from_container_unknown_name(docker):
    ok, _, out = _adopt(docker, "nav", {})
    assert not ok
    assert "running ROS containers: navstack" in out


def test_from_container_with_refresh_env(fairy_dirs, docker, tmp_path):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    (unit_dir / setup_cmd.SERVICE_NAME).write_text("[Service]\n")
    paths.robot_identity_path().write_text("robot: {}\n")
    docker.containers = [_inspect("navstack", {**ROS, "ROS_DOMAIN_ID": "7"})]
    seen = {}
    with mock.patch.object(setup_cmd, "UNIT_DIR", unit_dir), \
            mock.patch.object(setup_cmd.os, "geteuid", return_value=1000), \
            mock.patch.object(setup_cmd, "_check_ros_visible",
                              side_effect=lambda c: seen.setdefault(
                                  "domain", os.environ.get("ROS_DOMAIN_ID"))
                              or True), \
            mock.patch.object(setup_cmd, "_choose_service_python",
                              return_value="/usr/bin/python3"), \
            mock.patch.object(setup_cmd, "_refresh_env",
                              return_value=0) as refresh, \
            mock.patch.dict(os.environ, {}):
        assert setup_cmd.run(SimpleNamespace(from_container="navstack",
                                             refresh_env=True),
                             console=Console(file=io.StringIO())) == 0
    assert seen["domain"] == "7"  # ROS was checked on the container's domain
    assert refresh.call_args.args[1]["ROS_DOMAIN_ID"] == "7"


def test_doctor_blames_the_shell_when_the_service_matches_the_robot(
        fairy_dirs, docker):
    # After --from-container the service is right; a host shell still on
    # the old domain must not be told to refresh (that would undo it).
    _service({"ROS_DOMAIN_ID": "7"})
    docker.containers = [_inspect("navstack", {**ROS, "ROS_DOMAIN_ID": "7"})]
    fairy = {k: v for k, v in os.environ.items() if k.startswith("ROS_FAIRY_")}
    with mock.patch.dict(os.environ, {**fairy, **ROS}, clear=True):
        result = doctor._check_service_env()
    assert result["status"] == doctor.WARN
    assert result["title"].startswith("This shell is on a different")
    assert "navstack" in result["detail"]
    assert "export ROS_DOMAIN_ID=7" in result["hint"]
    assert "refresh-env" not in result["hint"]


def test_doctor_still_says_refresh_when_no_container_matches(fairy_dirs,
                                                             docker):
    _service({"ROS_DOMAIN_ID": "7"})
    docker.containers = [_inspect("navstack", {**ROS, "ROS_DOMAIN_ID": "3"})]
    fairy = {k: v for k, v in os.environ.items() if k.startswith("ROS_FAIRY_")}
    with mock.patch.dict(os.environ, {**fairy, **ROS}, clear=True):
        result = doctor._check_service_env()
    assert "--refresh-env" in result["hint"]


def test_setup_without_nodes_suggests_the_container(docker):
    docker.containers = [_inspect("navstack", {**ROS, "ROS_DOMAIN_ID": "7"})]
    console = Console(file=io.StringIO(), width=200)
    with mock.patch.object(setup_cmd, "_ros2_list", return_value=[]), \
            mock.patch.dict(os.environ, ROS):
        assert setup_cmd._check_ros_visible(console) is False
    assert "--from-container navstack" in console.file.getvalue()


def test_setup_without_nodes_names_no_container_when_several(docker):
    docker.containers = [_inspect("navstack", {**ROS, "ROS_DOMAIN_ID": "7"}),
                         _inspect("bench", {**ROS, "ROS_DOMAIN_ID": "9"})]
    console = Console(file=io.StringIO(), width=200)
    with mock.patch.object(setup_cmd, "_ros2_list", return_value=[]), \
            mock.patch.dict(os.environ, ROS):
        assert setup_cmd._check_ros_visible(console) is False
    out = console.file.getvalue()
    assert "--from-container <name>" in out and "bench, navstack" in out
    assert "source /opt/ros" not in out
