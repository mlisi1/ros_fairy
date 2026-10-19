from pathlib import Path

from fairy.utils import paths


def test_defaults(monkeypatch):
    monkeypatch.delenv("ROS_FAIRY_VAR_DIR", raising=False)
    monkeypatch.delenv("ROS_FAIRY_CONFIG_DIR", raising=False)
    assert paths.var_dir() == Path("/var/ros-fairy")
    assert paths.robot_identity_path() == Path("/etc/ros-fairy/robot_identity.yaml")
    assert paths.bags_dir() == Path("/var/ros-fairy/spool/bags")
    assert paths.index_db_path() == Path("/var/ros-fairy/index.db")
    assert paths.watchdog_state_path() == Path("/var/ros-fairy/watchdog.state")


def test_env_override(fairy_dirs):
    assert paths.var_dir() == fairy_dirs["var"]
    assert paths.spool_dir() == fairy_dirs["var"] / "spool"
    assert paths.harvest_json_path() == fairy_dirs["var"] / "spool" / "harvest.json"
    assert paths.mission_context_path() == \
        fairy_dirs["var"] / "spool" / "mission_context.json"
    assert paths.staging_dir() == fairy_dirs["var"] / "archive" / ".staging"
    assert paths.robot_identity_path().parent == fairy_dirs["cfg"]
