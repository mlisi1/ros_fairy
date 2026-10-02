import json

import pytest
from inotify_simple import Event, flags

from ros_fairy.manifest import builder
from ros_fairy.utils import paths
from ros_fairy.watchdog import watchdog as wd_mod
from ros_fairy.watchdog.watchdog import IDLE, RECORDING, Watchdog
from tests.conftest import make_bag

T0 = 1_750_000_000.0


class FakeINotify:
    """Mock inotify event injector (specs testing strategy)."""

    def __init__(self):
        self._next_wd = 1
        self.watches: dict[str, int] = {}
        self.queue: list[Event] = []

    def add_watch(self, path, mask):
        wd = self._next_wd
        self._next_wd += 1
        self.watches[str(path)] = wd
        return wd

    def rm_watch(self, wd):
        for path, known in list(self.watches.items()):
            if known == wd:
                del self.watches[path]

    def read(self, timeout=None):
        events, self.queue = self.queue, []
        return events

    # -- injection helpers -------------------------------------------------
    def emit(self, path, mask, name):
        self.queue.append(Event(self.watches[str(path)], mask, 0, name))

    def emit_dir_created(self, bags_dir, bag_name):
        self.emit(bags_dir, flags.CREATE | flags.ISDIR, bag_name)

    def emit_file(self, bag_dir, name, mask=flags.CREATE):
        self.emit(bag_dir, mask, name)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


GOOD_STATUS = {"robot_identity": "ok", "system_info": "ok", "ros_graph": "ok",
               "ros_descriptions": "ok", "docker_info": "skipped"}
IDENTITY = {
    "robot": {"name": "Heron-02", "platform": "Heron", "serial_number": "H02",
              "owner_organization": "Lab", "owner_contact": "a@b.c"},
    "sensors": [{"sensor_id": "gps0", "type": "gps", "make_model": "F9P",
                 "topic": "/fix", "frame_id": None, "calibration_ref": None}],
    "calibrations": [], "default_license": None,
}


def good_pipeline():
    return builder.compose_harvest(
        identity=IDENTITY,
        system={"hostname": "r1", "kernel": "Linux 6.8", "arch": "x86_64",
                "ros_distro": "jazzy", "apt_ros_versions": {}},
        graph={"captured_at": "2026-06-12T14:03:00+00:00", "nodes": [],
               "topics": [{"name": "/fix", "type": "sensor_msgs/msg/NavSatFix"}],
               "ros_packages": [], "parameters": {}, "complete": True},
        docker=None, descriptions={"robot_description": "<robot/>",
                                   "tf_static": []},
        harvest_status=GOOD_STATUS)


@pytest.fixture
def rig(fairy_dirs):
    ino = FakeINotify()
    clock = FakeClock()
    dog = Watchdog(inotify=ino, clock=clock, pipeline=good_pipeline,
                   harvest_in_thread=False)
    dog.start()
    return ino, clock, dog


def _steady(start, end, hz):
    n = int((end - start) * hz)
    return [start + i / hz for i in range(n + 1)]


def _record_bag(rig, name="rosbag2_test", with_metadata=True):
    ino, clock, dog = rig
    bag = make_bag(paths.bags_dir() / name,
                   {"/fix": _steady(T0, T0 + 60, 10)})
    if not with_metadata:
        (bag / "metadata.yaml").unlink()
    ino.emit_dir_created(paths.bags_dir(), name)
    dog.step(0)
    ino.emit_file(bag, f"{name}_0.db3")
    dog.step(0)
    return bag


def test_full_cycle(rig):
    ino, clock, dog = rig
    assert dog.state == IDLE
    bag = _record_bag(rig)

    assert dog.state == RECORDING
    harvest, _ = builder.load_spool()
    assert harvest["robot"]["name"] == "Heron-02"
    assert harvest["sensors"][0]["detected_at_start"] is True
    assert harvest["bags"] == []
    state = json.loads(paths.watchdog_state_path().read_text())
    assert state["state"] == "RECORDING"
    assert state["active_bag_dir"] == str(bag)

    ino.emit_file(bag, "metadata.yaml", flags.CLOSE_WRITE)
    dog.step(0)

    assert dog.state == IDLE
    harvest, _ = builder.load_spool()
    assert len(harvest["bags"]) == 1
    rec = harvest["bags"][0]
    assert rec["storage_format"] == "sqlite3"
    assert rec["message_count"] == 601
    assert rec["topics"][0]["name"] == "/fix"
    assert rec["health_warnings"] == []
    assert harvest["provenance"]["harvested_at"]
    assert json.loads(paths.watchdog_state_path().read_text())["state"] == "IDLE"


def test_storage_present_before_watch_still_records(rig):
    """Race: the first storage chunk lands before W2 is armed.

    inotify never delivers a CREATE for a file that predates ``add_watch``,
    so a bag whose first ``.db3`` appears in the window between the directory
    showing up and the per-bag watch being armed would be missed entirely —
    no RECORDING, no context harvest. Here only the dir-created event is
    emitted; the storage file already exists and no file event ever fires.
    The watchdog must scan the new directory on arm and still record.
    """
    ino, clock, dog = rig
    name = "rosbag2_race"
    bag = make_bag(paths.bags_dir() / name, {"/fix": _steady(T0, T0 + 60, 10)})
    (bag / "metadata.yaml").unlink()  # recording still in progress

    ino.emit_dir_created(paths.bags_dir(), name)
    dog.step(0)  # no storage-file event is ever emitted

    assert dog.state == RECORDING
    assert dog.active_bag_dir == bag
    harvest, _ = builder.load_spool()
    assert harvest["robot"]["name"] == "Heron-02"
    state = json.loads(paths.watchdog_state_path().read_text())
    assert state["state"] == "RECORDING"
    assert state["active_bag_dir"] == str(bag)


def test_inactivity_finalises_crashed_bag(rig):
    ino, clock, dog = rig
    _record_bag(rig, with_metadata=False)
    assert dog.state == RECORDING

    clock.now += wd_mod.BAG_INACTIVITY_S + 1
    dog.step(0)

    assert dog.state == IDLE
    harvest, _ = builder.load_spool()
    rec = harvest["bags"][0]
    assert rec["storage_format"] == "unknown"
    assert any("unexpectedly" in w["plain_text"]
               for w in rec["health_warnings"])


def test_activity_defers_inactivity_timeout(rig):
    ino, clock, dog = rig
    bag = _record_bag(rig)
    clock.now += wd_mod.BAG_INACTIVITY_S - 5
    ino.emit_file(bag, f"{bag.name}_0.db3", flags.MODIFY)
    dog.step(0)
    clock.now += wd_mod.BAG_INACTIVITY_S - 5
    dog.step(0)
    assert dog.state == RECORDING


def test_ros_down_schedules_retry_and_recovers(fairy_dirs):
    ino, clock = FakeINotify(), FakeClock()
    calls = {"n": 0}

    def flaky_pipeline():
        calls["n"] += 1
        if calls["n"] == 1:
            return builder.compose_harvest(
                identity=IDENTITY, system=None, graph=None, docker=None,
                descriptions=None,
                harvest_status={**GOOD_STATUS, "ros_graph": "failed",
                                "ros_descriptions": "failed"})
        return good_pipeline()

    dog = Watchdog(inotify=ino, clock=clock, pipeline=flaky_pipeline,
                   harvest_in_thread=False)
    dog.start()
    bag = _record_bag((ino, clock, dog))
    harvest, _ = builder.load_spool()
    assert harvest["provenance"]["harvest_status"]["ros_graph"] == "failed"
    assert dog._next_retry is not None

    # rosbag2 keeps writing while time passes, so the bag stays active
    for _ in range(4):
        clock.now += wd_mod.ROS_RETRY_INTERVAL_S / 4 + 1
        ino.emit_file(bag, f"{bag.name}_0.db3", flags.MODIFY)
        dog.step(0)
    assert dog.state == RECORDING
    harvest, _ = builder.load_spool()
    assert harvest["provenance"]["harvest_status"]["ros_graph"] == "ok"
    assert calls["n"] == 2
    assert dog._next_retry is None


def test_recovery_finalises_leftover_bag(fairy_dirs):
    bag = make_bag(paths.bags_dir() / "rosbag2_left",
                   {"/fix": _steady(T0, T0 + 60, 10)})
    ino, clock = FakeINotify(), FakeClock()
    dog = Watchdog(inotify=ino, clock=clock, pipeline=good_pipeline,
                   harvest_in_thread=False)
    dog.start()
    assert dog.state == IDLE
    harvest, _ = builder.load_spool()
    assert len(harvest["bags"]) == 1
    assert harvest["bags"][0]["path"] == str(bag)


def test_unreliable_clock_bag_records_unknown_duration(fairy_dirs):
    """A bag whose clock was broken for most of the run is finalised with an
    unknown duration and no fabricated start/end times or data rates."""
    bad = [float(i) for i in range(1, 31)]      # 30 near-epoch stamps
    good = _steady(T0, T0 + 5, 2)               # 11 real stamps
    bag = make_bag(paths.bags_dir() / "rosbag2_badclock",
                   {"/data": bad + good})

    wd_mod.append_bag_record(bag)

    harvest, _ = builder.load_spool()
    rec = harvest["bags"][0]
    assert rec["duration_s"] is None
    assert rec["start_time"] is None and rec["end_time"] is None
    assert all(t["avg_frequency_hz"] is None for t in rec["topics"])
    assert any(w["kind"] == "unreliable_clock" for w in rec["health_warnings"])
    # the record still validates against the schema (timing fields optional).
    from ros_fairy.manifest.schema import Bag
    Bag.model_validate(rec)


def test_recovery_resumes_recording(fairy_dirs):
    bag = make_bag(paths.bags_dir() / "rosbag2_live",
                   {"/fix": _steady(T0, T0 + 60, 10)})
    (bag / "metadata.yaml").unlink()
    ino, clock = FakeINotify(), FakeClock()
    dog = Watchdog(inotify=ino, clock=clock, pipeline=good_pipeline,
                   harvest_in_thread=False)
    dog.start()
    assert dog.state == RECORDING
    assert dog.active_bag_dir == bag


def test_second_bag_queued_then_recorded(rig):
    ino, clock, dog = rig
    bag_a = _record_bag(rig, "rosbag2_a")
    bag_b = make_bag(paths.bags_dir() / "rosbag2_b",
                     {"/fix": _steady(T0 + 100, T0 + 160, 10)})
    ino.emit_dir_created(paths.bags_dir(), "rosbag2_b")
    dog.step(0)
    ino.emit_file(bag_b, "rosbag2_b_0.db3")
    dog.step(0)
    assert dog.active_bag_dir == bag_a
    assert dog.queued_bags == [bag_b]

    ino.emit_file(bag_a, "metadata.yaml", flags.CLOSE_WRITE)
    dog.step(0)
    assert dog.state == RECORDING
    assert dog.active_bag_dir == bag_b

    ino.emit_file(bag_b, "metadata.yaml", flags.CLOSE_WRITE)
    dog.step(0)
    harvest, _ = builder.load_spool()
    assert [b["path"] for b in harvest["bags"]] == [str(bag_a), str(bag_b)]


def test_short_recording_waits_for_inflight_harvest(fairy_dirs):
    """A recording shorter than the harvest pipeline must not finalise
    against an empty spool (2026-07-03 field bug: a 6.7 s bag was archived
    with the all-modules-failed stub while the real harvest was still
    running in its thread)."""
    import threading

    started = threading.Event()
    release = threading.Event()

    def slow_pipeline():
        started.set()
        assert release.wait(timeout=5)
        return good_pipeline()

    ino = FakeINotify()
    dog = Watchdog(inotify=ino, clock=FakeClock(), pipeline=slow_pipeline,
                   harvest_in_thread=True)
    dog.start()
    bag = make_bag(paths.bags_dir() / "rosbag2_short",
                   {"/fix": _steady(T0, T0 + 5, 10)})
    ino.emit_dir_created(paths.bags_dir(), "rosbag2_short")
    dog.step(0)
    ino.emit_file(bag, "rosbag2_short_0.db3")
    dog.step(0)
    assert started.wait(timeout=5)

    # The bag closes while the harvest thread is still mid-pipeline; let it
    # finish shortly after finalise starts waiting.
    threading.Timer(0.1, release.set).start()
    ino.emit_file(bag, "metadata.yaml", flags.CLOSE_WRITE)
    dog.step(0)

    assert dog.state == IDLE
    harvest, _ = builder.load_spool()
    assert harvest["robot"]["name"] == "Heron-02"
    assert harvest["provenance"]["harvest_status"] == GOOD_STATUS
    assert [b["path"] for b in harvest["bags"]] == [str(bag)]


def test_harvest_rewrite_preserves_bags(rig):
    ino, clock, dog = rig
    bag = _record_bag(rig)
    ino.emit_file(bag, "metadata.yaml", flags.CLOSE_WRITE)
    dog.step(0)
    dog._harvest_once()  # e.g. a retry firing after finalise
    harvest, _ = builder.load_spool()
    assert len(harvest["bags"]) == 1


def test_apply_session_env_adopts_recording_shell(fairy_dirs):
    """The watchdog adopts the recorder's ROS env so its harvest lands on the
    same DDS partition as the session actually recording (issue #29)."""
    from unittest import mock

    from ros_fairy.utils import ros_env
    paths.spool_dir().mkdir(parents=True, exist_ok=True)
    ros_env.write_file(paths.session_env_path(),
                       {"ROS_DOMAIN_ID": "42", "RMW_IMPLEMENTATION": "rmw_x"})
    dog = Watchdog(inotify=FakeINotify(), pipeline=good_pipeline,
                   harvest_in_thread=False)
    with mock.patch.dict(wd_mod.os.environ, {"ROS_DOMAIN_ID": "0"}, clear=False):
        dog._apply_session_env()
        assert wd_mod.os.environ["ROS_DOMAIN_ID"] == "42"
        assert wd_mod.os.environ["RMW_IMPLEMENTATION"] == "rmw_x"


def test_apply_session_env_ignores_loader_paths(fairy_dirs):
    """A group-writable session.env must not inject loader paths into the
    root watchdog process (privilege escalation)."""
    from unittest import mock

    from ros_fairy.utils import ros_env
    paths.spool_dir().mkdir(parents=True, exist_ok=True)
    ros_env.write_file(paths.session_env_path(),
                       {"ROS_DOMAIN_ID": "42", "PYTHONPATH": "/tmp/evil",
                        "LD_LIBRARY_PATH": "/tmp/evil", "PATH": "/tmp/evil"})
    dog = Watchdog(inotify=FakeINotify(), pipeline=good_pipeline,
                   harvest_in_thread=False)
    with mock.patch.dict(wd_mod.os.environ,
                         {"PATH": "/usr/bin", "PYTHONPATH": "/safe"},
                         clear=False):
        dog._apply_session_env()
        assert wd_mod.os.environ["ROS_DOMAIN_ID"] == "42"   # discovery adopted
        assert wd_mod.os.environ["PATH"] == "/usr/bin"      # loader path intact
        assert wd_mod.os.environ["PYTHONPATH"] == "/safe"


def test_apply_session_env_reverts_keys_absent_from_session(fairy_dirs):
    """A key a session doesn't set reverts to the watchdog's baseline, so a
    previous session's value never leaks into a later harvest (#29 review #3)."""
    from unittest import mock

    from ros_fairy.utils import ros_env
    paths.spool_dir().mkdir(parents=True, exist_ok=True)
    ros_env.write_file(paths.session_env_path(), {"ROS_DOMAIN_ID": "42"})
    with mock.patch.dict(wd_mod.os.environ,
                         {"RMW_IMPLEMENTATION": "rmw_base"}, clear=False):
        dog = Watchdog(inotify=FakeINotify(), pipeline=good_pipeline,
                       harvest_in_thread=False)
        wd_mod.os.environ["RMW_IMPLEMENTATION"] = "rmw_leaked"  # stale prior run
        dog._apply_session_env()
        assert wd_mod.os.environ["ROS_DOMAIN_ID"] == "42"
        assert wd_mod.os.environ["RMW_IMPLEMENTATION"] == "rmw_base"


def test_apply_session_env_noop_without_file(fairy_dirs):
    dog = Watchdog(inotify=FakeINotify(), pipeline=good_pipeline,
                   harvest_in_thread=False)
    dog._apply_session_env()  # must not raise when no session.env exists


def test_read_state(fairy_dirs):
    assert wd_mod.read_state() is None
    paths.watchdog_state_path().write_text('{"state": "IDLE"}')
    assert wd_mod.read_state() == {"state": "IDLE"}
    paths.watchdog_state_path().write_text("not json")
    assert wd_mod.read_state() is None


def test_ensure_ros_log_dir_when_home_missing(fairy_dirs, monkeypatch):
    # Under systemd there is no $HOME; rcl logging then fails to expand
    # ~/.ros/log and node creation breaks (param dump, rclpy descriptions).
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("ROS_LOG_DIR", raising=False)
    wd_mod.ensure_ros_log_dir()
    assert wd_mod.os.environ["ROS_LOG_DIR"] == str(paths.ros_log_dir())
    assert paths.ros_log_dir().is_dir()


def test_ensure_ros_log_dir_respects_writable_home(fairy_dirs, monkeypatch,
                                                    tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ROS_LOG_DIR", raising=False)
    wd_mod.ensure_ros_log_dir()
    assert "ROS_LOG_DIR" not in wd_mod.os.environ
    assert (tmp_path / ".ros" / "log").is_dir()


def test_ensure_ros_log_dir_respects_existing_ros_log_dir(fairy_dirs, monkeypatch):
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("ROS_LOG_DIR", "/somewhere/else")
    wd_mod.ensure_ros_log_dir()
    assert wd_mod.os.environ["ROS_LOG_DIR"] == "/somewhere/else"


def test_ensure_ros_log_dir_falls_back_when_home_unwritable(fairy_dirs,
                                                             monkeypatch,
                                                             tmp_path):
    # A $HOME that's *set* but not writable by this process — root writing
    # into an operator's home directory, say — must not be trusted blindly:
    # that silently broke `ros2 param dump` the same way a missing $HOME did.
    # Using a file (not a directory) as the HOME component forces the
    # ~/.ros/log mkdir to fail deterministically, regardless of privilege.
    not_a_dir = tmp_path / "home_is_actually_a_file"
    not_a_dir.write_text("x")
    monkeypatch.setenv("HOME", str(not_a_dir))
    monkeypatch.delenv("ROS_LOG_DIR", raising=False)
    wd_mod.ensure_ros_log_dir()
    assert wd_mod.os.environ["ROS_LOG_DIR"] == str(paths.ros_log_dir())


def test_run_pipeline_marks_ros_graph_partial_when_incomplete(fairy_dirs,
                                                               monkeypatch):
    """ros_graph.harvest() doesn't raise when only the parameter dump timed
    out — it returns complete=False instead. run_pipeline must not report
    that as a plain "ok", or the gap becomes invisible everywhere that reads
    harvest_status (status display, doctor, diff)."""
    from ros_fairy.harvest import (
        docker_info,
        hardware_devices,
        python_env,
        robot_identity,
        ros_graph,
        system_info,
    )

    monkeypatch.setattr(robot_identity, "harvest", lambda: {
        "robot": None, "sensors": [], "calibrations": [],
        "default_license": None})
    monkeypatch.setattr(system_info, "harvest", lambda: {
        "hostname": "r1", "kernel": "Linux", "arch": "x86_64",
        "ros_distro": "jazzy", "apt_ros_versions": {},
        "clock_synchronized": True})
    monkeypatch.setattr(python_env, "harvest", lambda: {
        "python_env": None, "pip_freeze": None, "pip_list_json": None,
        "status": "ok"})
    monkeypatch.setattr(hardware_devices, "harvest", lambda: {
        "devices": [], "lsusb_verbose": None, "dmesg_usb": None,
        "status": "ok"})
    monkeypatch.setattr(ros_graph, "harvest", lambda: {
        "captured_at": None, "nodes": ["/n"], "topics": [],
        "ros_packages": [], "parameters": {}, "complete": False,
        "robot_description": "<robot/>", "tf_static": []})
    monkeypatch.setattr(docker_info, "harvest", lambda: {
        "docker_containers": [], "raw_inspect": [], "available": False})

    doc = wd_mod.run_pipeline()
    assert doc["provenance"]["harvest_status"]["ros_graph"] == "partial"
    # the URDF rides in the same snapshot and lands in its own section
    assert doc["provenance"]["harvest_status"]["ros_descriptions"] == "ok"
    assert doc["ros_graph"]["robot_description"] == "<robot/>"


# -- repeated harvests must not degrade the capture (2026-10-01) ---------------

def _graph_pipeline(nodes, status="ok", packages=("rclpy",)):
    def pipeline():
        captured = status in ("ok", "partial")
        return builder.compose_harvest(
            identity=IDENTITY,
            system={"hostname": "r1", "kernel": "Linux 6.8", "arch": "x86_64",
                    "ros_distro": "jazzy", "apt_ros_versions": {}},
            graph={"captured_at": "2026-10-01T14:30:00+00:00",
                   "nodes": list(nodes),
                   "topics": [{"name": "/fix",
                               "type": "sensor_msgs/msg/NavSatFix"}],
                   "ros_packages": list(packages) if packages else None,
                   "parameters": {n: {n: {"ros__parameters": {"x": 1}}}
                                  for n in nodes},
                   "complete": True} if captured else
                  ({"ros_packages": list(packages)} if packages else None),
            docker=None,
            descriptions={"robot_description": "<robot/>", "tf_static": []}
            if captured else None,
            harvest_status={**GOOD_STATUS, "ros_graph": status,
                            "ros_descriptions": status if captured
                            else "timeout"})
    return pipeline


def test_later_failed_harvest_keeps_the_earlier_graph(fairy_dirs):
    """A retry that ran after the stack stopped overwrote a good capture with
    an empty one, so the mission was archived without its graph."""
    ino, clock = FakeINotify(), FakeClock()
    dog = Watchdog(inotify=ino, clock=clock,
                   pipeline=_graph_pipeline(["/navsat", "/bt_navigator"]),
                   harvest_in_thread=False)
    dog.start()
    _record_bag((ino, clock, dog), with_metadata=False)

    dog.pipeline = _graph_pipeline([], status="failed", packages=None)
    dog._harvest_once()

    harvest, _ = builder.load_spool()
    graph = harvest["ros_graph"]
    assert graph["nodes"] == ["/navsat", "/bt_navigator"]
    assert set(graph["parameters"]) == {"/navsat", "/bt_navigator"}
    assert graph["robot_description"] == "<robot/>"
    status = harvest["provenance"]["harvest_status"]
    assert status["ros_graph"] == "ok" and status["ros_descriptions"] == "ok"
    assert harvest["software"]["ros_packages"] == ["rclpy"]
    assert harvest["sensors"][0]["detected_at_start"] is True


def test_later_better_harvest_still_replaces_a_failed_one(fairy_dirs):
    ino, clock = FakeINotify(), FakeClock()
    dog = Watchdog(inotify=ino, clock=clock,
                   pipeline=_graph_pipeline([], status="failed"),
                   harvest_in_thread=False)
    dog.start()
    _record_bag((ino, clock, dog), with_metadata=False)
    dog.pipeline = _graph_pipeline(["/navsat"])
    dog._harvest_once()
    harvest, _ = builder.load_spool()
    assert harvest["ros_graph"]["nodes"] == ["/navsat"]
    assert harvest["provenance"]["harvest_status"]["ros_graph"] == "ok"


def test_recording_start_supersedes_a_pending_retry(fairy_dirs):
    """A retry armed by an earlier failure fired alongside the new recording's
    own harvest, queueing a second one that ran after the stack stopped."""
    ino, clock = FakeINotify(), FakeClock()
    calls = []

    def counting():
        calls.append(clock.now)
        return good_pipeline()

    dog = Watchdog(inotify=ino, clock=clock, pipeline=counting,
                   harvest_in_thread=False)
    dog.start()
    dog._next_retry = clock.now - 1  # expired while idle
    _record_bag((ino, clock, dog), with_metadata=False)
    dog.step(0)
    assert len(calls) == 1
    assert dog._next_retry is None


def test_run_pipeline_keeps_packages_when_graph_discovery_fails(fairy_dirs,
                                                                 monkeypatch):
    from ros_fairy.harvest import (
        docker_info,
        hardware_devices,
        python_env,
        robot_identity,
        ros_graph,
        system_info,
    )
    monkeypatch.setattr(robot_identity, "harvest", lambda: IDENTITY)
    monkeypatch.setattr(system_info, "harvest", lambda: {
        "hostname": "r1", "kernel": "Linux", "arch": "x86_64",
        "ros_distro": "jazzy", "apt_ros_versions": {}})
    monkeypatch.setattr(python_env, "harvest", lambda: {"status": "ok"})
    monkeypatch.setattr(hardware_devices, "harvest", lambda: {"status": "ok"})
    monkeypatch.setattr(docker_info, "harvest", lambda: {"available": False})

    def no_nodes():
        raise ros_graph.RosGraphError("no ROS nodes visible")

    monkeypatch.setattr(ros_graph, "harvest", no_nodes)
    monkeypatch.setattr(ros_graph, "list_packages", lambda: ["nav2_core",
                                                             "rclpy"])

    doc = wd_mod.run_pipeline()
    assert doc["provenance"]["harvest_status"]["ros_graph"] == "failed"
    # a failed snapshot is a failure with a logged reason, not a "timeout"
    assert doc["provenance"]["harvest_status"]["ros_descriptions"] == "failed"
    assert doc["software"]["ros_packages"] == ["nav2_core", "rclpy"]
    assert doc["ros_graph"]["nodes"] == []

    monkeypatch.setattr(ros_graph, "list_packages", no_nodes)
    doc = wd_mod.run_pipeline()
    assert doc["software"]["ros_packages"] is None  # not captured, not empty


def test_spool_log_handler_mirrors_and_caps(fairy_dirs, monkeypatch):
    import logging
    paths.spool_dir().mkdir(parents=True, exist_ok=True)
    handler = wd_mod.SpoolLogHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger = logging.getLogger("ros_fairy.test_spool_log")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.warning("harvest module ros_graph failed: no ROS nodes visible")
        text = paths.watchdog_log_path().read_text()
        assert "WARNING harvest module ros_graph failed" in text

        paths.watchdog_log_path().unlink()  # spool cleared between missions
        logger.info("next mission")
        assert paths.watchdog_log_path().read_text() == "INFO next mission\n"

        monkeypatch.setattr(wd_mod.SpoolLogHandler, "MAX_BYTES", 2000)
        for i in range(200):
            logger.info("line %03d", i)
        text = paths.watchdog_log_path().read_text()
        assert len(text) <= 2000
        assert text.startswith("[older entries dropped]\n")
        assert text.rstrip().endswith("INFO line 199")
    finally:
        logger.removeHandler(handler)


def test_a_real_capture_is_never_replaced_by_an_older_one(fairy_dirs):
    """2026-10-02: a stale 4-node "ok" capture (a test run's) was kept over
    the operator's live 38-node "partial" one. Only an empty-handed run may
    defer to an earlier capture."""
    ino, clock = FakeINotify(), FakeClock()
    dog = Watchdog(inotify=ino, clock=clock,
                   pipeline=_graph_pipeline(["/talker"]),
                   harvest_in_thread=False)
    dog.start()
    _record_bag((ino, clock, dog), with_metadata=False)
    live = [f"/node_{i}" for i in range(38)]
    dog.pipeline = _graph_pipeline(live, status="partial")
    dog._harvest_once()
    harvest, _ = builder.load_spool()
    assert harvest["ros_graph"]["nodes"] == live
    assert harvest["provenance"]["harvest_status"]["ros_graph"] == "partial"
