import os
import shutil
import sqlite3
import textwrap
from pathlib import Path

import pytest


def _ros_available() -> bool:
    """A sourced ROS 2 environment: ros2 on PATH and $ROS_DISTRO set."""
    return shutil.which("ros2") is not None and bool(os.environ.get("ROS_DISTRO"))


def pytest_collection_modifyitems(config, items):
    """Skip `ros`-marked tests when ROS isn't sourced.

    They are deselected by default (pyproject `addopts = -m "not ros"`); this
    only fires when a user explicitly runs `pytest -m ros` on a box without a
    sourced ROS 2 environment, so they skip with a clear reason instead of
    failing.
    """
    if _ros_available():
        return
    skip = pytest.mark.skip(
        reason="requires a sourced ROS 2 environment (ros2 on PATH + "
               "$ROS_DISTRO); source ROS 2 and run `pytest -m ros`")
    for item in items:
        if "ros" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_host_udev(request, monkeypatch):
    """Keep this machine's sysfs and udevadm out of the unit tests: the
    hardware probe would otherwise read the real USB tree. Tests of the probe
    itself are marked ``real_usb_udev`` and fake sysfs instead."""
    if "real_usb_udev" in request.keywords:
        return
    from ros_fairy.harvest import usb_udev
    monkeypatch.setattr(usb_udev, "harvest", lambda: {
        "usb": None, "udev_rules": None, "rule_files": {}, "trace": [],
        "partial": False})


@pytest.fixture
def fairy_dirs(tmp_path, monkeypatch):
    """Relocate /var/ros-fairy and /etc/ros-fairy into tmp_path."""
    var = tmp_path / "var"
    cfg = tmp_path / "etc"
    (var / "spool" / "bags").mkdir(parents=True)
    (var / "archive").mkdir()
    cfg.mkdir()
    monkeypatch.setenv("ROS_FAIRY_VAR_DIR", str(var))
    monkeypatch.setenv("ROS_FAIRY_CONFIG_DIR", str(cfg))
    return {"var": var, "cfg": cfg}


@pytest.fixture
def identity_yaml(fairy_dirs):
    """A valid robot_identity.yaml with two sensors and one calibration."""
    cal_file = fairy_dirs["cfg"] / "gps0_cal.yaml"
    cal_file.write_text("offset: 0.5\n")
    path = fairy_dirs["cfg"] / "robot_identity.yaml"
    path.write_text(textwrap.dedent(f"""\
        robot:
          name: Heron-02
          platform: Clearpath Heron USV
          serial_number: H02-2031-XK
        owner:
          organization: Example Marine Robotics Lab
          contact_email: fleet@example.org
        sensors:
          - sensor_id: gps0
            type: gps
            make_model: u-blox ZED-F9P
            topic: /fix
            frame_id: gps_link
            calibration: gps0_cal
          - sensor_id: sonar0
            type: sonar
            make_model: BlueRobotics Ping2
            topic: /depth
        calibrations:
          - name: gps0_cal
            source_path: {cal_file}
            format: yaml
        """))
    return path


def make_bag(bag_dir: Path, topics: dict[str, list[float]],
             types: dict[str, str] | None = None,
             storage: str = "sqlite3") -> Path:
    """Create a minimal rosbag2-shaped bag dir with sqlite3 storage.

    topics: topic name -> message timestamps in seconds (epoch).
    """
    bag_dir.mkdir(parents=True, exist_ok=True)
    types = types or {}
    all_ts = [t for stamps in topics.values() for t in stamps]
    start = min(all_ts) if all_ts else 0.0
    end = max(all_ts) if all_ts else 0.0

    db_name = f"{bag_dir.name}_0.db3"
    con = sqlite3.connect(bag_dir / db_name)
    con.execute("CREATE TABLE topics (id INTEGER PRIMARY KEY, name TEXT, "
                "type TEXT, serialization_format TEXT)")
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, "
                "topic_id INTEGER, timestamp INTEGER, data BLOB)")
    for tid, (name, stamps) in enumerate(topics.items(), start=1):
        con.execute("INSERT INTO topics VALUES (?, ?, ?, 'cdr')",
                    (tid, name, types.get(name, "std_msgs/msg/Empty")))
        con.executemany(
            "INSERT INTO messages (topic_id, timestamp, data) VALUES (?, ?, x'')",
            [(tid, int(ts * 1e9)) for ts in stamps])
    con.commit()
    con.close()

    _write_metadata(bag_dir, topics, types, storage, db_name, start, end)
    return bag_dir


def make_mcap_bag(bag_dir: Path, topics: dict[str, list[float]],
                  types: dict[str, str] | None = None) -> Path:
    """Create a rosbag2-shaped bag dir backed by a real ``.mcap`` file.

    Requires the optional ``mcap`` package; tests should ``importorskip`` it.
    topics: topic name -> message timestamps in seconds (epoch).
    """
    from mcap.writer import Writer

    bag_dir.mkdir(parents=True, exist_ok=True)
    types = types or {}
    all_ts = [t for stamps in topics.values() for t in stamps]
    start = min(all_ts) if all_ts else 0.0
    end = max(all_ts) if all_ts else 0.0

    mcap_name = f"{bag_dir.name}_0.mcap"
    with open(bag_dir / mcap_name, "wb") as handle:
        writer = Writer(handle)
        writer.start()
        for name, stamps in topics.items():
            schema_id = writer.register_schema(
                name=types.get(name, "std_msgs/msg/Empty"),
                encoding="ros2msg", data=b"")
            channel_id = writer.register_channel(
                topic=name, message_encoding="cdr", schema_id=schema_id)
            for ts in stamps:
                ns = int(ts * 1e9)
                writer.add_message(channel_id=channel_id, log_time=ns,
                                   data=b"", publish_time=ns)
        writer.finish()

    _write_metadata(bag_dir, topics, types, "mcap", mcap_name, start, end)
    return bag_dir


def _write_metadata(bag_dir: Path, topics: dict[str, list[float]],
                    types: dict[str, str], storage: str, rel_file: str,
                    start: float, end: float) -> None:
    all_ts = [t for stamps in topics.values() for t in stamps]
    topic_entries = "\n".join(
        f"    - topic_metadata:\n"
        f"        name: {name}\n"
        f"        type: {types.get(name, 'std_msgs/msg/Empty')}\n"
        f"        serialization_format: cdr\n"
        f"      message_count: {len(stamps)}"
        for name, stamps in topics.items())
    (bag_dir / "metadata.yaml").write_text(textwrap.dedent(f"""\
        rosbag2_bagfile_information:
          version: 5
          storage_identifier: {storage}
          relative_file_paths:
            - {rel_file}
          duration:
            nanoseconds: {int((end - start) * 1e9)}
          starting_time:
            nanoseconds_since_epoch: {int(start * 1e9)}
          message_count: {len(all_ts)}
          topics_with_message_count:
        """) + topic_entries + "\n")
