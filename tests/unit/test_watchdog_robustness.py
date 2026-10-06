"""Watchdog robustness: FAILURE_CASES W1-W20."""

import errno
import json
import logging
import os
import threading
import time
from pathlib import Path
from unittest import mock

import pytest
from inotify_simple import flags

from ros_fairy.manifest import builder
from ros_fairy.ui import status
from ros_fairy.utils import fsio, paths, ros_env
from ros_fairy.watchdog import recorder_scan
from ros_fairy.watchdog import watchdog as wd_mod
from ros_fairy.watchdog.watchdog import FINALISING, IDLE, RECORDING, Watchdog
from tests.conftest import make_bag
from tests.unit.test_watchdog import (
    T0,
    FakeClock,
    FakeINotify,
    _graph_pipeline,
    _steady,
    good_pipeline,
)

SCAN_S = wd_mod.FOREIGN_SCAN_INTERVAL_S
DEAD_PID = 0x7FFFFFFF


class Scanner:
    """A settable fake of recorder_scan.scan."""

    def __init__(self, found=None):
        self.found = list(found or [])

    def __call__(self):
        return list(self.found)


def _dog(scanner=None, pipeline=good_pipeline, in_thread=False):
    ino, clock = FakeINotify(), FakeClock()
    dog = Watchdog(inotify=ino, clock=clock, pipeline=pipeline,
                   harvest_in_thread=in_thread,
                   scan_recorders=scanner or Scanner())
    dog.start()
    return ino, clock, dog


def _spool_bag(ino, dog, name="rosbag2_test", metadata=False):
    bag = make_bag(paths.bags_dir() / name, {"/fix": _steady(T0, T0 + 60, 10)})
    if not metadata:
        (bag / "metadata.yaml").unlink()
    ino.emit_dir_created(paths.bags_dir(), name)
    dog.step(0)
    return bag


def _me():
    return {"pid": os.getpid(), "start": recorder_scan.proc_start(os.getpid())}


def _capture_log():
    """Records logged by the watchdog. Attached to its own logger: the
    ``ros_fairy`` parent may be a launch LaunchLogger that doesn't pass
    records to added handlers."""
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    wd_mod.log.addHandler(handler)
    return records, lambda: wd_mod.log.removeHandler(handler)


# -- W1: a quiet spool bag with a live recorder ------------------------------

def test_quiet_spool_bag_is_not_finalised_while_its_recorder_runs(fairy_dirs):
    scanner = Scanner()
    ino, clock, dog = _dog(scanner)
    bag = _spool_bag(ino, dog)
    assert dog.state == RECORDING
    scanner.found = [{**_me(), "output_dir": bag.resolve(), "discovery": {}}]
    clock.now += SCAN_S
    dog.step(0)

    # MCAP buffers whole chunks: nothing reaches the file for a long time.
    clock.now += wd_mod.BAG_INACTIVITY_S * 3
    dog.step(0)
    assert dog.state == RECORDING

    scanner.found = []  # the recorder exits without writing metadata
    dog._recorders[bag]["pid"] = DEAD_PID
    clock.now += wd_mod.BAG_INACTIVITY_S + 1
    dog.step(0)
    assert dog.state == IDLE


def test_early_finalised_record_is_reread_at_close(fairy_dirs):
    bag = make_bag(paths.bags_dir() / "rosbag2_q", {"/fix": [T0, T0 + 1]})
    meta = (bag / "metadata.yaml").read_text()
    (bag / "metadata.yaml").unlink()
    for f in bag.glob("*.db3"):
        f.write_bytes(b"")  # nothing flushed yet when it was finalised
    wd_mod.append_bag_record(bag)
    assert builder.load_spool()[0]["bags"][0]["topics"] == []

    (bag / "metadata.yaml").write_text(meta)  # the recorder closed it
    wd_mod.refresh_salvaged_records()
    [rec] = builder.load_spool()[0]["bags"]
    assert [t["name"] for t in rec["topics"]] == ["/fix"]


# -- W2 / W3: failures inside the watchdog ------------------------------------

def test_harvest_exception_is_retried_not_fatal(fairy_dirs):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        return good_pipeline()

    ino, clock, dog = _dog(pipeline=flaky)
    bag = _spool_bag(ino, dog)
    ino.emit_file(bag, "rosbag2_test_0.db3")
    dog.step(0)
    assert dog.state == RECORDING and dog._next_retry is not None

    clock.now += wd_mod.ROS_RETRY_INTERVAL_S + 1
    ino.emit_file(bag, "rosbag2_test_0.db3", flags.MODIFY)
    dog.step(0)
    assert calls["n"] == 2
    assert builder.load_spool()[0]["robot"]["name"] == "Heron-02"


def test_status_file_write_failure_does_not_raise(fairy_dirs, monkeypatch):
    ino, clock, dog = _dog()
    records, done = _capture_log()

    def full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(wd_mod.fsio, "atomic_write_json", full)
    try:
        dog.write_state()
        dog.write_state()  # logged once, not every time
    finally:
        done()
    assert sum("status file" in r.getMessage() for r in records) == 1


def test_failed_atomic_write_leaves_no_temp_file(tmp_path, monkeypatch):
    target = tmp_path / "x.json"

    def boom(fd):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(fsio.os, "fsync", boom)
    with pytest.raises(OSError):
        fsio.atomic_write_json(target, {"a": 1})
    assert list(tmp_path.iterdir()) == []


def test_loop_survives_an_unexpected_error(fairy_dirs):
    ino, clock, dog = _dog()
    calls = {"n": 0}

    def step(timeout_ms=1000):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        dog.stop()
    with mock.patch.object(dog, "step", step), \
            mock.patch.object(wd_mod.time, "sleep"):
        dog.run()
    assert calls["n"] == 2


# -- W4 / W5: harvests and a spool that moves on ------------------------------

def test_late_harvest_does_not_write_into_a_cleared_spool(fairy_dirs):
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(timeout=5)
        return good_pipeline()

    ino, clock, dog = _dog(pipeline=slow, in_thread=True)
    bag = _spool_bag(ino, dog, metadata=True)
    ino.emit_file(bag, "rosbag2_test_0.db3")
    dog.step(0)
    assert started.wait(timeout=5)
    ino.emit_file(bag, "metadata.yaml", flags.CLOSE_WRITE)
    dog.step(0)
    assert dog.state == FINALISING
    clock.now += wd_mod.HARVEST_WAIT_S + 1
    dog.step(0)
    assert dog.state == IDLE  # gave up waiting

    # mission_close saves and clears the spool...
    paths.harvest_json_path().unlink()
    release.set()
    dog._harvest_thread.join(timeout=5)
    # ...and the hung harvest's late result does not bring it back.
    assert not paths.harvest_json_path().exists()


def test_context_restored_when_spool_cleared_during_recording(fairy_dirs):
    ino, clock, dog = _dog()
    bag = _spool_bag(ino, dog, metadata=True)
    ino.emit_file(bag, "rosbag2_test_0.db3")
    dog.step(0)
    assert builder.load_spool()[0]["robot"]["name"] == "Heron-02"
    paths.harvest_json_path().unlink()  # a mission_close cleared the spool

    ino.emit_file(bag, "metadata.yaml", flags.CLOSE_WRITE)
    dog.step(0)
    harvest = builder.load_spool()[0]
    assert harvest["robot"]["name"] == "Heron-02"
    assert harvest["provenance"]["harvest_status"]["robot_identity"] == "ok"
    assert [b["path"] for b in harvest["bags"]] == [str(bag)]


# -- W6: no robot description ---------------------------------------------------

def test_missing_description_is_retried_twice_and_keeps_the_graph(fairy_dirs):
    runs = []

    def no_urdf():
        nodes = ["/first"] if not runs else ["/later"]
        runs.append(nodes)
        doc = _graph_pipeline(nodes)()
        doc["ros_graph"]["robot_description"] = None
        doc["ros_graph"]["tf_static"] = None
        doc["provenance"]["harvest_status"]["ros_descriptions"] = "timeout"
        return doc

    ino, clock, dog = _dog(pipeline=no_urdf)
    bag = _spool_bag(ino, dog)
    ino.emit_file(bag, "rosbag2_test_0.db3")
    dog.step(0)
    for _ in range(6):
        clock.now += wd_mod.ROS_RETRY_INTERVAL_S + 1
        ino.emit_file(bag, "rosbag2_test_0.db3", flags.MODIFY)
        dog.step(0)
    assert len(runs) == 1 + wd_mod.MAX_DESCRIPTION_RETRIES
    assert builder.load_spool()[0]["ros_graph"]["nodes"] == ["/first"]


# -- W7 / W8 / W9: restarts ---------------------------------------------------

def _age(bag, seconds=3600):
    old = time.time() - seconds
    for f in [bag, *bag.iterdir()]:
        os.utime(f, (old, old))


def test_restart_finalises_cut_off_bags_without_a_new_harvest(fairy_dirs):
    fsio.atomic_write_json(paths.harvest_json_path(),
                           _graph_pipeline(["/before_crash"])())
    dead = []
    for name in ("rosbag2_a", "rosbag2_b"):
        bag = make_bag(paths.bags_dir() / name, {"/fix": [T0, T0 + 1]})
        (bag / "metadata.yaml").unlink()
        _age(bag)
        dead.append(bag)
    pipeline = mock.Mock(side_effect=good_pipeline)
    ino, clock, dog = _dog(pipeline=pipeline)

    assert dog.state == IDLE
    pipeline.assert_not_called()
    harvest = builder.load_spool()[0]
    assert sorted(b["path"] for b in harvest["bags"]) == \
        [str(b) for b in dead]
    assert harvest["ros_graph"]["nodes"] == ["/before_crash"]


def test_restart_resumes_only_live_recordings(fairy_dirs):
    dead = make_bag(paths.bags_dir() / "rosbag2_a", {"/fix": [T0, T0 + 1]})
    (dead / "metadata.yaml").unlink()
    _age(dead)
    live = make_bag(paths.bags_dir() / "rosbag2_b", {"/fix": [T0, T0 + 1]})
    (live / "metadata.yaml").unlink()
    _age(live)
    scanner = Scanner([{**_me(), "output_dir": live.resolve(),
                        "discovery": {}}])
    ino, clock, dog = _dog(scanner)
    assert dog.state == RECORDING and dog.active_bag_dir == live
    assert [b["path"] for b in builder.load_spool()[0]["bags"]] == [str(dead)]


def test_foreign_recording_finished_while_down_is_finalised(fairy_dirs,
                                                            tmp_path):
    foreign = make_bag(tmp_path / "ext", {"/fix": [T0, T0 + 1]})
    paths.watchdog_state_path().write_text(json.dumps(
        {"state": "RECORDING", "tracked_foreign": [str(foreign)]}))
    ino, clock, dog = _dog()
    [rec] = builder.load_spool()[0]["bags"]
    assert rec["path"] == str(foreign) and rec["source"] == "detected"


def test_state_file_lists_tracked_foreign_recordings(fairy_dirs, tmp_path):
    foreign = make_bag(tmp_path / "ext", {"/fix": [T0, T0 + 1]})
    (foreign / "metadata.yaml").unlink()
    scanner = Scanner([{**_me(), "output_dir": foreign, "discovery": {}}])
    ino, clock, dog = _dog(scanner)
    clock.now += SCAN_S
    dog.step(0)
    state = json.loads(paths.watchdog_state_path().read_text())
    assert state["tracked_foreign"] == [str(foreign)]


# -- W10: the same folder recorded again ---------------------------------------

def test_re_recording_a_folder_is_noticed(fairy_dirs, tmp_path):
    foreign = make_bag(tmp_path / "test", {"/fix": [T0, T0 + 1]})
    wd_mod.append_bag_record(foreign, source="detected")
    (foreign / "metadata.yaml").unlink()  # recorded again into it
    scanner = Scanner([{**_me(), "output_dir": foreign, "discovery": {}}])
    ino, clock, dog = _dog(scanner)
    clock.now += SCAN_S
    dog.step(0)
    assert dog.state == RECORDING and dog.active_bag_dir == foreign
    assert builder.load_spool()[0]["bags"] == []  # stale record dropped


def test_vanished_queued_recording_is_forgotten(fairy_dirs, tmp_path):
    ino, clock, dog = _dog()
    ghost = tmp_path / "ghost"
    dog._foreign[ghost] = {"pid": DEAD_PID}
    dog.queued_bags.append(ghost)
    bag = _spool_bag(ino, dog, metadata=True)
    ino.emit_file(bag, "rosbag2_test_0.db3")
    dog.step(0)
    ino.emit_file(bag, "metadata.yaml", flags.CLOSE_WRITE)
    dog.step(0)
    assert ghost not in dog._foreign and dog.queued_bags == []


# -- W11 / W12 / W17: recorder processes ---------------------------------------

def _fake_pid(proc, pid, *, stat_state="S", start=1234, fds=()):
    d = proc / str(pid)
    (d / "fd").mkdir(parents=True)
    (d / "stat").write_text(
        f"{pid} (recorder) {stat_state} " + " ".join(["0"] * 18)
        + f" {start} 0 0\n")
    for i, target in enumerate(fds):
        os.symlink(target, d / "fd" / str(i))
    return d


def test_two_recorders_in_one_folder_each_find_their_own_bag(tmp_path,
                                                              monkeypatch):
    cwd = tmp_path / "work"
    older = make_bag(cwd / "rosbag2_2026_10_02-10_00_00", {"/t": [1.0]})
    newer = make_bag(cwd / "rosbag2_2026_10_02-10_00_05", {"/t": [1.0]})
    for b in (older, newer):
        (b / "metadata.yaml").unlink()
    proc = tmp_path / "proc"
    _fake_pid(proc, 10, fds=[next(older.glob("*.db3"))])
    _fake_pid(proc, 11, fds=[next(newer.glob("*.db3"))])
    monkeypatch.setattr(recorder_scan, "PROC", proc)
    argv = ["ros2", "bag", "record", "-a"]
    assert recorder_scan._resolve_output(argv, cwd, pid="10") == older
    assert recorder_scan._resolve_output(argv, cwd, pid="11") == newer


def test_zombie_and_reused_pids_are_not_alive(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    _fake_pid(proc, 20, stat_state="Z")
    _fake_pid(proc, 21, start=999)
    monkeypatch.setattr(recorder_scan, "PROC", proc)
    assert not recorder_scan.pid_alive(20)
    assert recorder_scan.pid_alive(21, start=999)
    assert not recorder_scan.pid_alive(21, start=1)  # pid reused since


def test_stale_state_file_with_a_reused_pid_is_not_a_running_watchdog():
    me = os.getpid()
    start = recorder_scan.proc_start(me)
    assert status.watchdog_alive({"pid": me, "proc_start": start})
    assert not status.watchdog_alive({"pid": me, "proc_start": start + 1})
    assert status.watchdog_alive({"pid": me})  # older state files


def test_recording_shorter_than_a_scan_is_still_captured(fairy_dirs,
                                                         tmp_path):
    out = tmp_path / "quick"
    scanner = Scanner([{**_me(),
                        "output_dir": None, "discovery": {},
                        "active": False, "ns_path": str(out),
                        "mountinfo": None}])
    ino, clock, dog = _dog(scanner)
    clock.now += SCAN_S
    dog.step(0)  # recorder seen, nothing recorded yet
    assert dog.state == IDLE and dog._pending

    make_bag(out, {"/fix": [T0, T0 + 1]})  # it recorded and exited
    scanner.found = []
    with mock.patch.object(recorder_scan, "pid_alive", return_value=False):
        clock.now += SCAN_S
        dog.step(0)
        dog.step(0)
    assert dog.state == IDLE
    [rec] = builder.load_spool()[0]["bags"]
    assert rec["path"] == str(out.resolve()) and rec["source"] == "detected"


def test_scan_reports_a_recorder_before_it_writes(tmp_path, monkeypatch):
    from tests.unit.test_foreign_bags import _fake_proc, _fake_recorder
    proc = _fake_proc(tmp_path)
    out = tmp_path / "not_yet"
    _fake_recorder(proc, 300, ["ros2", "bag", "record", "-o", str(out), "/t"],
                   cwd=str(tmp_path))
    (proc / "self" / "mountinfo").write_text("")
    monkeypatch.setattr(recorder_scan, "PROC", proc)
    monkeypatch.setattr(recorder_scan, "_reported", set())
    [rec] = recorder_scan.scan()
    assert rec["active"] is False and rec["ns_path"] == str(out)
    out.mkdir()
    assert recorder_scan.pending_output(rec) == out.resolve()


# -- W13 / W16 / W18: shared files ------------------------------------------------

def test_concurrent_atomic_writes_do_not_collide(tmp_path):
    target = tmp_path / "state.json"
    errors = []

    def writer(n):
        try:
            for i in range(200):
                fsio.atomic_write_json(target, {"writer": n, "i": i})
        except Exception as exc:  # pragma: no cover - the failure mode
            errors.append(exc)
    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert json.loads(target.read_text())["i"] == 199


def test_unreadable_harvest_is_kept_aside_not_overwritten(fairy_dirs):
    paths.harvest_json_path().write_text("{ truncated")
    bag = make_bag(paths.bags_dir() / "rosbag2_x", {"/fix": [T0, T0 + 1]})
    wd_mod.append_bag_record(bag)
    [aside] = paths.spool_dir().glob("harvest.json.corrupt-*")
    assert aside.read_text() == "{ truncated"
    assert len(builder.load_spool()[0]["bags"]) == 1


def test_session_env_is_replaced_atomically_and_group_writable(tmp_path):
    path = tmp_path / "session.env"
    path.write_text("OLD=1\n")
    path.chmod(0o444)  # left by another operator, not writable for us
    ros_env.write_file(path, {"ROS_DOMAIN_ID": "3"}, mode=0o664)
    assert ros_env.read_file(path) == {"ROS_DOMAIN_ID": "3"}
    assert path.stat().st_mode & 0o777 == 0o664


# -- W15: root-safe search paths -----------------------------------------------

def test_user_writable_search_paths_are_dropped(tmp_path):
    mine = tmp_path / "bin"
    mine.mkdir()
    env, dropped = ros_env.root_safe_env({
        "PATH": f"{mine}:/usr/bin", "PYTHONPATH": str(mine),
        "LD_PRELOAD": "/tmp/x.so", "ROS_DISTRO": "jazzy"})
    assert env["PATH"] == "/usr/bin" and env["PYTHONPATH"] == ""
    assert "LD_PRELOAD" not in env and env["ROS_DISTRO"] == "jazzy"
    assert dropped == [str(mine), str(mine)]


def test_setup_writes_a_root_safe_watchdog_env(fairy_dirs, tmp_path):
    from ros_fairy.subcommands import setup
    mine = tmp_path / "bin"
    mine.mkdir()
    setup.write_watchdog_env({"PATH": f"{mine}:/usr/bin",
                              "ROS_DISTRO": "jazzy"})
    assert ros_env.read_file(paths.watchdog_env_path())["PATH"] == "/usr/bin"


# -- W19 / W20: the event loop -------------------------------------------------

def test_metadata_written_while_recorder_runs_does_not_finalise(fairy_dirs):
    scanner = Scanner()
    ino, clock, dog = _dog(scanner)
    bag = _spool_bag(ino, dog, metadata=True)
    ino.emit_file(bag, "rosbag2_test_0.db3")
    dog.step(0)
    scanner.found = [{**_me(), "output_dir": bag.resolve(), "discovery": {}}]
    clock.now += SCAN_S
    dog.step(0)
    ino.emit_file(bag, "metadata.yaml", flags.CLOSE_WRITE)
    dog.step(0)
    assert dog.state == RECORDING
    dog._recorders[bag]["pid"] = DEAD_PID
    dog.step(0)
    assert dog.state == IDLE


def test_loop_keeps_scanning_while_finalising(fairy_dirs):
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(timeout=5)
        return good_pipeline()

    scanner = mock.Mock(return_value=[])
    ino, clock, dog = _dog(scanner, pipeline=slow, in_thread=True)
    bag = _spool_bag(ino, dog, metadata=True)
    ino.emit_file(bag, "rosbag2_test_0.db3")
    dog.step(0)
    assert started.wait(timeout=5)
    ino.emit_file(bag, "metadata.yaml", flags.CLOSE_WRITE)
    dog.step(0)
    assert dog.state == FINALISING
    calls = scanner.call_count
    clock.now += SCAN_S
    dog.step(0)
    assert scanner.call_count == calls + 1  # not blocked
    release.set()
    dog._harvest_thread.join(timeout=5)
    dog.step(0)
    assert dog.state == IDLE


def test_heartbeat_is_written_when_idle(fairy_dirs):
    ino, clock, dog = _dog()
    before = json.loads(paths.watchdog_state_path().read_text())
    time.sleep(0.01)
    clock.now += wd_mod.HEARTBEAT_S + 1
    dog.step(0)
    after = json.loads(paths.watchdog_state_path().read_text())
    assert after["heartbeat_at"] > before["heartbeat_at"]


def test_event_queue_overflow_rescans_the_spool(fairy_dirs):
    ino, clock, dog = _dog()
    bag = make_bag(paths.bags_dir() / "rosbag2_lost", {"/fix": [T0, T0 + 1]})
    (bag / "metadata.yaml").unlink()
    # its CREATE event was lost in the overflow
    from inotify_simple import Event
    ino.queue.append(Event(-1, flags.Q_OVERFLOW, 0, ""))
    dog.step(0)
    assert dog.state == RECORDING and dog.active_bag_dir == bag


def test_spool_lock_path_is_inside_the_spool(fairy_dirs):
    assert Path(paths.harvest_lock_path()).parent == paths.spool_dir()


def test_cut_off_mcap_is_salvaged(tmp_path):
    """W8: a recorder killed mid-write leaves an MCAP without its summary;
    the complete chunks before the cut still say what was recorded."""
    from mcap.writer import Writer
    bag = tmp_path / "cut"
    bag.mkdir()
    path = bag / "cut_0.mcap"
    with open(path, "wb") as fh:
        w = Writer(fh, chunk_size=512)
        w.start()
        sid = w.register_schema("sensor_msgs/msg/Imu", "ros2msg", b"")
        cid = w.register_channel("/imu/data", "cdr", sid)
        for i in range(200):
            w.add_message(cid, log_time=i, publish_time=i, data=b"x" * 64)
        w.finish()
    data = path.read_bytes()
    path.write_bytes(data[: len(data) * 2 // 3])  # power cut
    storage, topics, count = wd_mod._salvage_topics(bag)
    assert storage == "mcap"
    assert [(t["name"], t["type"]) for t in topics] == \
        [("/imu/data", "sensor_msgs/msg/Imu")]
    assert 0 < count < 200
