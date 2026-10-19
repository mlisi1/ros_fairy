"""Tests for utils/ros_env — the ROS-environment capture/serialise helpers."""

import pytest

from fairy.utils import ros_env


def test_capture_excludes_ros_fairys_own_override_vars():
    """ROS_FAIRY_CONFIG_DIR/VAR_DIR start with "ROS_" like real ROS vars do,
    but must never be captured into the watchdog's persisted environment —
    they'd silently redirect the service's spool/archive/index paths."""
    env = {"ROS_DISTRO": "jazzy", "ROS_DOMAIN_ID": "7",
           "ROS_FAIRY_CONFIG_DIR": "/tmp/evil/etc",
           "ROS_FAIRY_VAR_DIR": "/tmp/evil/var"}
    captured = ros_env.capture(env)
    assert captured == {"ROS_DISTRO": "jazzy", "ROS_DOMAIN_ID": "7"}


def test_capture_keeps_only_ros_variables():
    env = {"ROS_DISTRO": "jazzy", "AMENT_PREFIX_PATH": "/opt/ros/jazzy",
           "RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp", "ROS_DOMAIN_ID": "7",
           "PATH": "/opt/ros/jazzy/bin", "LD_LIBRARY_PATH": "/opt/ros/jazzy/lib",
           "EDITOR": "vim", "HOME": "/root"}
    captured = ros_env.capture(env)
    assert captured["ROS_DISTRO"] == "jazzy"
    assert captured["AMENT_PREFIX_PATH"] == "/opt/ros/jazzy"
    assert "PATH" in captured and "LD_LIBRARY_PATH" in captured
    assert "EDITOR" not in captured and "HOME" not in captured


def test_serialize_round_trips_through_parse():
    env = {"ROS_DISTRO": "jazzy", "ROS_DOMAIN_ID": "7",
           "PATH": "/opt/ros/jazzy/bin:/usr/bin"}
    assert ros_env.parse(ros_env.serialize(env)) == env


def test_serialize_is_sorted_and_newline_terminated():
    text = ros_env.serialize({"ROS_DOMAIN_ID": "7", "AMENT_PREFIX_PATH": "/x"})
    assert text == "AMENT_PREFIX_PATH=/x\nROS_DOMAIN_ID=7\n"


def test_serialize_empty_is_empty_string():
    assert ros_env.serialize({}) == ""


def test_parse_ignores_blanks_and_comments():
    env = ros_env.parse("# a comment\n\nROS_DISTRO=jazzy\n  ROS_DOMAIN_ID=3 \n")
    assert env == {"ROS_DISTRO": "jazzy", "ROS_DOMAIN_ID": "3"}


def test_parse_keeps_equals_in_value():
    assert ros_env.parse("CYCLONEDDS_URI=file:///x?a=b")["CYCLONEDDS_URI"] == \
        "file:///x?a=b"


def test_safe_session_env_keeps_only_discovery_keys():
    """The root watchdog must never adopt loader paths from the group-writable
    session.env — that would be a local privilege-escalation vector."""
    env = {"ROS_DOMAIN_ID": "7", "RMW_IMPLEMENTATION": "rmw_x",
           "ROS_LOCALHOST_ONLY": "1",
           "PATH": "/tmp/evil", "LD_LIBRARY_PATH": "/tmp/evil",
           "PYTHONPATH": "/tmp/evil", "AMENT_PREFIX_PATH": "/tmp/evil",
           "ROS_DISTRO": "jazzy"}
    safe = ros_env.safe_session_env(env)
    assert safe == {"ROS_DOMAIN_ID": "7", "RMW_IMPLEMENTATION": "rmw_x",
                    "ROS_LOCALHOST_ONLY": "1"}
    for dangerous in ("PATH", "LD_LIBRARY_PATH", "PYTHONPATH",
                      "AMENT_PREFIX_PATH"):
        assert dangerous not in safe


def test_read_file_missing_returns_empty(tmp_path):
    assert ros_env.read_file(tmp_path / "nope.env") == {}


def test_write_then_read_file(tmp_path):
    path = tmp_path / "sub" / "session.env"
    ros_env.write_file(path, {"ROS_DISTRO": "jazzy"})
    assert path.is_file()
    assert ros_env.read_file(path) == {"ROS_DISTRO": "jazzy"}


# -- find_setup_bash / source_setup_bash (self-sourcing for `setup`) ---------

def test_find_setup_bash_lists_distros_sorted(tmp_path):
    (tmp_path / "jazzy").mkdir()
    (tmp_path / "jazzy" / "setup.bash").write_text("")
    (tmp_path / "humble").mkdir()
    (tmp_path / "humble" / "setup.bash").write_text("")
    (tmp_path / "not_a_distro").mkdir()  # no setup.bash inside — ignored
    found = ros_env.find_setup_bash(tmp_path)
    assert [p.parent.name for p in found] == ["humble", "jazzy"]


def test_find_setup_bash_missing_root_returns_empty(tmp_path):
    assert ros_env.find_setup_bash(tmp_path / "does-not-exist") == []


def test_source_setup_bash_returns_only_changed_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("ROS_FAIRY_TEST_UNCHANGED", "same")
    script = tmp_path / "setup.bash"
    script.write_text(
        "export ROS_FAIRY_TEST_NEW=hello\n"
        "export ROS_FAIRY_TEST_UNCHANGED=same\n")
    changed = ros_env.source_setup_bash(script)
    assert changed.get("ROS_FAIRY_TEST_NEW") == "hello"
    assert "ROS_FAIRY_TEST_UNCHANGED" not in changed


def test_source_setup_bash_raises_on_failure(tmp_path):
    script = tmp_path / "broken.bash"
    script.write_text("echo bad >&2\nexit 1\n")
    with pytest.raises(RuntimeError, match="bad"):
        ros_env.source_setup_bash(script)
