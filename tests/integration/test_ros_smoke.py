"""Opt-in smoke tests against a live, sourced ROS 2 environment.

These validate the parts mocked tests cannot:
  - the `ros2 fairy` verb is actually discovered by ros2cli (entry_points);
  - the single-participant ROS snapshot sees a real running node, its
    parameters and a latched `/robot_description`, even with every
    CycloneDDS participant slot already taken;
  - the full record -> harvest -> archive -> verify pipeline runs against a
    real `ros2 bag record` output (Jazzy's default MCAP storage).

They are DESELECTED by default (pyproject `addopts = -m "not ros"`). Run them on
a robot / dev box with ROS sourced:

    pip install -e '.[test]'
    source /opt/ros/<distro>/setup.bash
    pytest -m ros -v

The graph and lifecycle tests need
`demo_nodes_cpp` (talker) and skip if it is not installed.
"""

import os
import signal
import subprocess
import time
from contextlib import contextmanager

import pytest

from ros_fairy.archive import assembler
from ros_fairy.harvest import ros_graph
from ros_fairy.manifest import builder
from ros_fairy.subcommands import verify
from ros_fairy.utils import fsio, paths
from ros_fairy.watchdog import recorder_scan, watchdog
from ros_fairy.watchdog.watchdog import IDLE, RECORDING, Watchdog

pytestmark = pytest.mark.ros


@pytest.fixture(autouse=True)
def _invisible_to_the_real_watchdog(monkeypatch):
    """Every process these tests start carries the opt-out, so a robot's own
    watchdog never files the test recordings into its real spool (it did,
    twice, on 2026-10-01/02). The scanner under test looks for a different
    variable, so it still sees them."""
    monkeypatch.setenv(recorder_scan.IGNORE_ENV, "1")
    monkeypatch.setattr(recorder_scan, "IGNORE_ENV",
                        "ROS_FAIRY_SMOKE_TEST_NEVER_SET")


@contextmanager
def _background(cmd: list[str]):
    """Run a ROS process in the background; stop it cleanly with SIGINT.

    rosbag2 needs SIGINT (not SIGTERM) to flush metadata.yaml on stop, so all
    background processes are stopped that way.
    """
    # Own process group, signalled as a whole: `ros2 run` is a wrapper whose
    # node child otherwise survives it, orphaned, still holding a DDS
    # participant slot on the robot (found 8 left behind on 2026-10-01).
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        yield proc
    finally:
        _signal_group(proc, signal.SIGINT)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            _signal_group(proc, signal.SIGKILL)
            proc.wait()


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


def _wait_for_node(substr: str, timeout: float = 15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if any(substr in n for n in ros_graph.list_nodes()):
                return True
        except ros_graph.RosGraphError:
            pass
        time.sleep(0.5)
    return False


@pytest.fixture
def talker():
    """A running demo talker, or skip if demo_nodes_cpp isn't installed."""
    try:
        with _background(["ros2", "run", "demo_nodes_cpp", "talker"]) as proc:
            if not _wait_for_node("talker"):
                pytest.skip("demo_nodes_cpp talker did not come up "
                            "(package not installed?)")
            yield proc
    except FileNotFoundError:
        pytest.skip("ros2 not on PATH")


def test_fair_verb_is_discoverable():
    """ros2cli must find the `fairy` command and its verbs (entry_points)."""
    out = subprocess.run(["ros2", "fairy", "--help"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    text = out.stdout + out.stderr
    for verb in ("mission_start", "mission_close", "list", "diff", "verify"):
        assert verb in text, f"verb '{verb}' missing from `ros2 fairy --help`"


def test_ros_graph_harvest_sees_live_node(talker):
    graph = ros_graph.harvest()
    assert any("talker" in n for n in graph["nodes"]), graph["nodes"]
    chatter = next((t for t in graph["topics"] if t["name"] == "/chatter"),
                   None)
    assert chatter is not None, "no /chatter topic in the live graph"
    assert "String" in chatter["type"]
    assert graph["ros_packages"], "expected a non-empty package list"
    # parameters come from the same single participant as the listing
    talker_node = next(n for n in graph["nodes"] if "talker" in n)
    assert "use_sim_time" in \
        graph["parameters"][talker_node][talker_node]["ros__parameters"]


def test_ros_graph_harvest_ignores_participant_slot_limit(talker):
    """With CycloneDDS's participant slots exhausted by the robot, the
    snapshot must still join the graph (2026-10-01). Skips on other RMWs."""
    if os.environ.get("RMW_IMPLEMENTATION") != "rmw_cyclonedds_cpp":
        pytest.skip("participant slots are a CycloneDDS concept")
    prefix = subprocess.run(["ros2", "pkg", "prefix", "demo_nodes_cpp"],
                            capture_output=True, text=True).stdout.strip()
    listener = f"{prefix}/lib/demo_nodes_cpp/listener"
    fillers = []
    try:
        # the binary itself, not `ros2 run` (whose wrapper may outlive a
        # signal and leave the node holding its slot)
        for i in range(40):
            fillers.append(subprocess.Popen(
                [listener, "--ros-args", "-r", f"__node:=fairy_smoke_fill_{i}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(8)
        assert any(p.poll() is not None for p in fillers), \
            "the fillers never hit the participant limit"
        graph = ros_graph.harvest()
        assert sum("fairy_smoke_fill_" in n for n in graph["nodes"]) >= 20
    finally:
        for p in fillers:
            p.terminate()
        deadline = time.monotonic() + 10
        for p in fillers:
            try:
                p.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()


def test_ros_descriptions_captures_latched_urdf():
    """Publish a latched /robot_description; confirm the snapshot reads it."""
    rclpy = pytest.importorskip("rclpy")
    from rclpy.qos import (
        DurabilityPolicy,
        HistoryPolicy,
        QoSProfile,
        ReliabilityPolicy,
    )
    from std_msgs.msg import String

    urdf = "<robot name='smoke'><link name='base'/></robot>"
    latched = QoSProfile(depth=1, history=HistoryPolicy.KEEP_LAST,
                         reliability=ReliabilityPolicy.RELIABLE,
                         durability=DurabilityPolicy.TRANSIENT_LOCAL)

    rclpy.init()
    try:
        node = rclpy.create_node("ros_fairy_smoke_urdf_pub")
        pub = node.create_publisher(String, "/robot_description", latched)
        pub.publish(String(data=urdf))
        # let the latched sample go out on the wire
        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            rclpy.spin_once(node, timeout_sec=0.1)
        # the snapshot is a separate process with a late-joining subscriber
        captured = ros_graph.harvest()["robot_description"]
        # On a bare graph our latched publisher is the only source, so we read
        # it back verbatim. On a real robot a transient-local
        # /robot_description publisher already exists and the late-joining sub
        # may latch *that* sample instead — which is fine; the point of this
        # smoke test is that the latched-read path yields a well-formed URDF.
        assert captured is not None, "harvest captured no /robot_description"
        assert captured == urdf or ("<robot" in captured
                                    and "</robot>" in captured)
    finally:
        rclpy.shutdown()


_IDENTITY_YAML = """\
robot:
  name: SmokeBot
  platform: Test Rig
  serial_number: SMOKE-1
owner:
  organization: Lab
  contact_email: smoke@example.org
sensors:
  - sensor_id: chat0
    type: other
    make_model: Demo Talker
    topic: /chatter
"""


def test_full_record_harvest_archive_verify(talker, fairy_dirs):
    """End-to-end on real ROS: record a bag, run the real harvest pipeline,
    assemble the crate, and verify it."""
    (fairy_dirs["cfg"] / "robot_identity.yaml").write_text(_IDENTITY_YAML)

    bag_dir = paths.bags_dir() / "smoke_bag"
    with _background(["ros2", "bag", "record", "-o", str(bag_dir), "/chatter"]):
        time.sleep(5)  # capture a few seconds of /chatter
    assert (bag_dir / "metadata.yaml").is_file(), "rosbag2 wrote no metadata"

    # Real harvest pipeline (ros_graph, system, python_env, hardware, docker,
    # descriptions) + finalise the real bag, exactly as the watchdog would.
    fsio.atomic_write_json(paths.harvest_json_path(), watchdog.run_pipeline())
    watchdog.append_bag_record(bag_dir)

    harvest, _ = builder.load_spool()
    assert harvest["robot"]["name"] == "SmokeBot"
    assert any("talker" in n for n in harvest["ros_graph"]["nodes"])

    context = builder.new_mission_context(
        operator_name="Smoke Tester", goal="Live ROS smoke test",
        location_name="CI Lab")
    fsio.atomic_write_json(paths.mission_context_path(), context)

    record = builder.build(harvest, context)
    crate = assembler.assemble(record, harvest)

    checks = verify.verify_archive(crate)
    failures = [c for c in checks if c["status"] == verify.FAIL]
    assert not failures, failures
    # the bag came from real `ros2 bag record`, so it carries per-file hashes
    assert record.bags[0].file_sha256


def _wait_for_scan(bag_dir, timeout: float = 20) -> dict | None:
    """Poll recorder_scan until it reports a recorder writing to bag_dir."""
    want = bag_dir.resolve()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for rec in recorder_scan.scan():
            if rec["output_dir"] == want:
                return rec
        time.sleep(0.5)
    return None


def test_recorder_scan_finds_live_recording(talker, tmp_path):
    """The /proc scan locates a real `ros2 bag record` running outside the spool."""
    bag_dir = tmp_path / "ext_run"
    with _background(["ros2", "bag", "record", "-o", str(bag_dir), "/chatter"]):
        found = _wait_for_scan(bag_dir)
    assert found is not None, "recorder_scan did not detect the live recording"
    assert found["output_dir"] == bag_dir.resolve()
    assert found["pid"] > 0


def test_watchdog_poller_detects_and_finalises_foreign(talker, tmp_path,
                                                       fairy_dirs):
    """End-to-end live: the watchdog's poller adopts a recording started outside
    mission_record, harvests it, and finalises it as a `detected` bag in place."""
    (fairy_dirs["cfg"] / "robot_identity.yaml").write_text(_IDENTITY_YAML)
    bag_dir = (tmp_path / "ext_live").resolve()

    dog = Watchdog(harvest_in_thread=False)  # real inotify, /proc scan, clock
    dog.start()
    try:
        with _background(["ros2", "bag", "record",
                          "-o", str(bag_dir), "/chatter"]):
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline and dog.state != RECORDING:
                dog.step(timeout_ms=200)
            assert dog.state == RECORDING, "poller never detected the recording"
            assert dog.active_bag_dir == bag_dir
        # recorder stopped (SIGINT) -> metadata.yaml flushed; let it finalise.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and dog.state != IDLE:
            dog.step(timeout_ms=200)
    finally:
        dog.ino.close()

    assert dog.state == IDLE
    harvest, _ = builder.load_spool()
    assert harvest["bags"][0]["source"] == "detected"
    assert harvest["bags"][0]["path"] == str(bag_dir)
    assert bag_dir.is_dir()  # referenced in place, never moved into the spool
