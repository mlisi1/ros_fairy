"""Rendering tests for ui/diff.py (the rich table view of ros2 fairy diff).

The --json path is covered in test_subcommands; these exercise the rendered
sections, the added/removed/changed row convention, and the no-differences
case.
"""

import copy
import io

import pytest
from rich.console import Console

from ros_fairy.manifest import builder
from ros_fairy.ui import diff as diff_ui
from tests.unit.test_archive import _spool


def _render(a, b) -> str:
    console = Console(file=io.StringIO(), width=140, force_terminal=False)
    diff_ui.show_diff(a, b, console=console)
    return console.file.getvalue()


def _pair(fairy_dirs, mutate):
    """Two records from the same fixture spool; ``mutate(harvest, context)``
    shapes the second one."""
    h1, c1 = _spool(fairy_dirs)
    a = builder.build(h1, c1)
    h2, c2 = copy.deepcopy(h1), copy.deepcopy(c1)
    mutate(h2, c2)
    return a, builder.build(h2, c2)


def test_identical_missions_show_no_differences(fairy_dirs):
    a, b = _pair(fairy_dirs, lambda h, c: None)
    out = _render(a, b)
    assert "No differences found." in out
    assert "Mission diff" in out


def test_context_changes_rendered(fairy_dirs):
    def mutate(h, c):
        c["intent"]["goal"] = "Chart the harbour instead"
        c["identity"]["operator_name"] = "Marco"

    a, b = _pair(fairy_dirs, mutate)
    out = _render(a, b)
    assert "Mission context" in out
    assert "Chart the harbour instead" in out
    assert "Marco" in out
    # unchanged sections are omitted entirely
    assert "Software" not in out


def test_software_and_graph_changes_rendered(fairy_dirs):
    def mutate(h, c):
        h["software"]["ros_distro"] = "kilted"
        h["ros_graph"]["nodes"] = ["/navsat", "/lidar_driver"]
        h["ros_graph"]["topics"] = [
            {"name": "/scan", "type": "sensor_msgs/msg/LaserScan"}]

    a, b = _pair(fairy_dirs, mutate)
    out = _render(a, b)
    assert "ROS graph" in out
    assert "/lidar_driver" in out       # added node
    assert "/fix" in out                # removed topic
    assert "kilted" in out


def test_all_graph_changes_shown_without_cap(fairy_dirs):
    def mutate(h, c):
        h["ros_graph"]["nodes"] = [f"/extra_{i:02d}" for i in range(30)]

    a, b = _pair(fairy_dirs, mutate)
    out = _render(a, b)
    assert "more change" not in out
    assert "/extra_00" in out
    assert "/extra_29" in out           # would have been beyond the old cap


def test_random_id_nodes_ignored_in_graph_diff(fairy_dirs):
    def mutate(h, c):
        h["ros_graph"]["nodes"] = h["ros_graph"]["nodes"] + [
            "/transform_listener_impl_565e5a3dba30",
            "/visodom/transform_listener_impl_6512734d39b0",
        ]

    a, b = _pair(fairy_dirs, mutate)
    out = _render(a, b)
    # only the noisy, random-id nodes differ — nothing worth reporting
    assert "No differences found." in out
    assert "transform_listener_impl" not in out


def test_recording_changes_rendered(fairy_dirs):
    def mutate(h, c):
        bag = h["bags"][0]
        bag["duration_s"] = (bag["duration_s"] or 0) + 300
        bag["size_bytes"] = bag["size_bytes"] * 2 + 1
        bag["health_warnings"] = [{
            "topic": "/fix", "sensor_id": "gps0", "kind": "gap",
            "start_offset_s": 1.0, "duration_s": 4.0,
            "plain_text": "GPS signal was lost for 4 seconds.",
        }]

    a, b = _pair(fairy_dirs, mutate)
    out = _render(a, b)
    assert "Recordings" in out
    assert "Duration" in out
    assert "Warnings" in out
    # counts only — the warning text itself is too noisy for a diff
    assert "GPS signal was lost" not in out


def test_host_package_changes_rendered(fairy_dirs):
    def mutate(h, c):
        h["software"]["ros_packages"] = ["rclpy", "nav2_core"]

    a, b = _pair(fairy_dirs, mutate)
    out = _render(a, b)
    assert "Software" in out
    assert "nav2_core" in out
    assert "rclpy" not in out           # unchanged, so not shown


def test_docker_package_changes_rendered(fairy_dirs):
    h1, c1 = _spool(fairy_dirs)
    h1["software"]["docker_containers"][0]["ros_packages"] = ["rclpy"]
    a = builder.build(h1, c1)

    h2, c2 = copy.deepcopy(h1), copy.deepcopy(c1)
    h2["software"]["docker_containers"][0]["ros_packages"] = [
        "nav2_bringup", "rclpy"]
    b = builder.build(h2, c2)

    out = _render(a, b)
    assert "Software" in out
    assert "navstack: pkg nav2_bringup" in out
    assert "navstack: pkg rclpy" not in out  # unchanged, so not shown


def test_docker_package_not_captured_does_not_show_fake_removals(fairy_dirs):
    """ros_packages is None (not [] ) when the probe never ran — e.g. an old
    record from before package capture existed. That must not be reported
    as every package having been uninstalled."""
    def mutate(h, c):
        h["software"]["docker_containers"][0]["ros_packages"] = [
            f"pkg_{i}" for i in range(20)]

    a, b = _pair(fairy_dirs, mutate)
    out = _render(b, a)  # b has packages captured, a (rendered as "B") does not
    assert "pkg_0" not in out
    assert "packages captured" in out


def test_parameter_changes_rendered(fairy_dirs):
    h1, c1 = _spool(fairy_dirs)
    h1["ros_graph"]["parameters"] = {
        "/navsat": {"/navsat": {"ros__parameters": {"rate": 5.0}}}}
    a = builder.build(h1, c1)

    h2, c2 = copy.deepcopy(h1), copy.deepcopy(c1)
    h2["ros_graph"]["parameters"] = {
        "/navsat": {"/navsat": {"ros__parameters": {"rate": 10.0}}}}
    b = builder.build(h2, c2)

    out = _render(a, b)
    assert "Parameters" in out
    assert "/navsat: rate" in out
    assert "5.0" in out
    assert "10.0" in out


def test_matching_parameters_say_so_instead_of_vanishing(fairy_dirs):
    """With other changes present, an absent Parameters section looked like
    'not captured' when the parameters had simply matched."""
    def mutate(h, c):
        c["intent"]["goal"] = "Something else"
    params = {"/navsat": {"/navsat": {"ros__parameters": {"rate": 5.0}}},
              "/imu": {"/imu": {"ros__parameters": {"hz": 100}}}}
    h1, c1 = _spool(fairy_dirs)
    h1["ros_graph"]["parameters"] = params
    a = builder.build(h1, c1)
    h2, c2 = copy.deepcopy(h1), copy.deepcopy(c1)
    mutate(h2, c2)
    b = builder.build(h2, c2)
    out = _render(a, b)
    assert "Parameters" in out
    assert "no changes across 2 shared nodes" in out
    assert out.index("Mission context") < out.index("Parameters")
    # identical missions still say so plainly
    assert "No differences found." in _render(a, builder.build(h1, c1))
    # and the machine-readable diff carries no synthetic row
    assert "parameters" not in diff_ui.diff_as_dict(a, b)["changes"]


def test_nested_parameter_diff_shows_only_the_changed_leaf(fairy_dirs):
    """A nav2-style plugin param is one whole nested dict in the raw dump;
    the diff must drill down to the single leaf that changed rather than
    dumping the entire nested structure for both sides."""
    unchanged_scan = {
        "max_obstacle_height": 2.0, "min_obstacle_height": 0.15,
        "raytrace_max_range": 10.0,
    }
    h1, c1 = _spool(fairy_dirs)
    h1["ros_graph"]["parameters"] = {
        "/global_costmap": {"/global_costmap": {"ros__parameters": {
            "obstacle_layer": {"enabled": True, "scan": dict(unchanged_scan)}
        }}}}
    a = builder.build(h1, c1)

    h2, c2 = copy.deepcopy(h1), copy.deepcopy(c1)
    changed_scan = dict(unchanged_scan, raytrace_max_range=10.1)
    h2["ros_graph"]["parameters"] = {
        "/global_costmap": {"/global_costmap": {"ros__parameters": {
            "obstacle_layer": {"enabled": True, "scan": changed_scan}
        }}}}
    b = builder.build(h2, c2)

    out = _render(a, b)
    assert "/global_costmap: obstacle_layer.scan.raytrace_max_range" in out
    assert "10.0" in out
    assert "10.1" in out
    # the untouched leaves must not be dumped alongside the real change
    assert "max_obstacle_height" not in out
    assert "enabled" not in out


def test_urdf_diff_shows_only_changed_line(fairy_dirs):
    urdf_a = ("<robot name='heron'>\n"
             "  <link name='base'/>\n"
             "  <link name='old'/>\n"
             "</robot>")
    urdf_b = ("<robot name='heron'>\n"
             "  <link name='base'/>\n"
             "  <link name='new'/>\n"
             "</robot>")

    h1, c1 = _spool(fairy_dirs)
    h1["ros_graph"]["robot_description"] = urdf_a
    a = builder.build(h1, c1)
    h2, c2 = copy.deepcopy(h1), copy.deepcopy(c1)
    h2["ros_graph"]["robot_description"] = urdf_b
    b = builder.build(h2, c2)

    out = _render(a, b)
    assert "Robot description" in out
    assert "old" in out and "new" in out
    # the unchanged lines must not be dumped alongside the real change
    assert "heron" not in out
    assert "base" not in out


def test_urdf_diff_reads_archived_file_content(fairy_dirs):
    """Once archived, ros_graph.robot_description is rewritten to a
    crate-relative path — the diff must read the actual file, not compare
    two identical path strings."""
    from ros_fairy.archive import assembler, locate

    h1, c1 = _spool(fairy_dirs)
    h1["ros_graph"]["robot_description"] = "<robot><link name='a'/></robot>"
    crate_a = assembler.assemble(builder.build(h1, c1), h1)

    # A fresh _spool() call, not a deepcopy of h1 — assemble() already moved
    # h1's bag out of the spool, so reusing that path would fail the move.
    h2, c2 = _spool(fairy_dirs)
    h2["ros_graph"]["robot_description"] = "<robot><link name='b'/></robot>"
    crate_b = assembler.assemble(builder.build(h2, c2), h2)

    loaded_a = locate.load_record(crate_a)
    loaded_b = locate.load_record(crate_b)
    # confirms the field itself is now just a (identical) path — proving a
    # plain field comparison would have hidden the real change
    assert loaded_a.ros_graph.robot_description == \
        loaded_b.ros_graph.robot_description == "harvest/robot_description.urdf"

    console = Console(file=io.StringIO(), width=140, force_terminal=False)
    diff_ui.show_diff(loaded_a, loaded_b, console=console,
                      crate_a=crate_a, crate_b=crate_b)
    out = console.file.getvalue()
    assert "Robot description" in out
    assert "'a'" in out and "'b'" in out


def test_tf_static_diff_shows_added_removed_and_changed(fairy_dirs):
    h1, c1 = _spool(fairy_dirs)
    h1["ros_graph"]["tf_static"] = [
        {"parent_frame": "base", "child_frame": "gps_link",
         "translation": {"x": 0.1, "y": 0.0, "z": 0.0},
         "rotation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}},
        {"parent_frame": "base", "child_frame": "camera_link",
         "translation": {"x": 0.0, "y": 0.0, "z": 0.3},
         "rotation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}},
    ]
    a = builder.build(h1, c1)

    h2, c2 = copy.deepcopy(h1), copy.deepcopy(c1)
    h2["ros_graph"]["tf_static"] = [
        {"parent_frame": "base", "child_frame": "gps_link",
         "translation": {"x": 0.2, "y": 0.0, "z": 0.0},
         "rotation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}},
        {"parent_frame": "base", "child_frame": "lidar_link",
         "translation": {"x": 0.0, "y": 0.0, "z": 0.5},
         "rotation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}},
    ]
    b = builder.build(h2, c2)

    out = _render(a, b)
    assert "Static transforms" in out
    assert "base → camera_link" in out               # removed
    assert "base → lidar_link" in out                # added
    assert "base → gps_link: translation.x" in out   # changed leaf
    assert "0.1" in out and "0.2" in out


def test_parameter_capture_gap_flagged_not_silently_ignored(fairy_dirs):
    """If a mission's param dump failed entirely for a node the other mission
    did capture, that's a harvest gap — not evidence nothing changed."""
    h1, c1 = _spool(fairy_dirs)
    h1["ros_graph"]["nodes"] = ["/navsat", "/bt_navigator"]
    h1["ros_graph"]["parameters"] = {
        "/bt_navigator": {"/bt_navigator": {"ros__parameters": {"rate": 5.0}}}}
    a = builder.build(h1, c1)

    h1["ros_graph"]["parameters"]["/navsat"] = {
        "/navsat": {"ros__parameters": {"frame_id": "gps_link"}}}
    a = builder.build(h1, c1)

    h2, c2 = copy.deepcopy(h1), copy.deepcopy(c1)
    del h2["ros_graph"]["parameters"]["/bt_navigator"]  # its dump failed
    b = builder.build(h2, c2)

    out = _render(a, b)
    assert "Parameters" in out
    assert "/bt_navigator: parameters captured" in out
    # must not fabricate a "rate" value diff — there's nothing to compare to
    assert "/bt_navigator: rate" not in out


def test_failed_graph_capture_flagged_once_not_as_removed_nodes(fairy_dirs):
    """A mission whose graph harvest saw nothing (DDS discovery failed,
    2026-10-01) must read as "not captured", not as every node removed."""
    def mutate(h, c):
        h["ros_graph"]["nodes"] = []
        h["ros_graph"]["topics"] = []
        h["ros_graph"]["parameters"] = {}
    a, b = _pair(fairy_dirs, mutate)
    out = " ".join(_render(a, b).split())
    assert "ROS graph captured yes no" in out
    assert "parameters captured yes no" in out
    assert "/navsat" not in out
    changes = diff_ui.diff_as_dict(a, b)["changes"]
    assert {"ros_graph", "parameters"} <= set(changes)


def test_graph_missing_in_both_missions_is_said_not_hidden(fairy_dirs):
    """The 2026-10-01 report: two missions with no graph captured rendered
    only the Recordings section, as if the parameters had matched."""
    def empty(h):
        h["ros_graph"]["nodes"] = []
        h["ros_graph"]["topics"] = []
        h["ros_graph"]["parameters"] = {}
    h1, c1 = _spool(fairy_dirs)
    empty(h1)
    a = builder.build(h1, c1)
    b = builder.build(copy.deepcopy(h1), copy.deepcopy(c1))
    out = " ".join(_render(a, b).split())
    assert "No differences found." not in out
    assert out.count("not captured in either mission") == 2
    assert diff_ui.diff_as_dict(a, b)["changes"] == {}  # notes aren't changes


def test_parameter_gap_note_skipped_for_brand_new_nodes(fairy_dirs):
    """A node that's simply new in B never had params in A by definition —
    that's already explained by the ROS graph section, not a capture gap."""
    def mutate(h, c):
        h["ros_graph"]["nodes"] = h["ros_graph"]["nodes"] + ["/new_node"]
        h["ros_graph"]["parameters"] = {
            "/new_node": {"/new_node": {"ros__parameters": {"x": 1}}}}

    a, b = _pair(fairy_dirs, mutate)
    out = _render(a, b)
    assert "/new_node: parameters captured" not in out


def test_tf_static_not_captured_flagged_not_silently_ignored(fairy_dirs):
    h1, c1 = _spool(fairy_dirs)
    h1["ros_graph"]["tf_static"] = [
        {"parent_frame": "base", "child_frame": "gps_link",
         "translation": {"x": 0.1, "y": 0.0, "z": 0.0},
         "rotation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}}]
    a = builder.build(h1, c1)

    h2, c2 = copy.deepcopy(h1), copy.deepcopy(c1)
    h2["ros_graph"]["tf_static"] = None  # /tf_static capture timed out
    b = builder.build(h2, c2)

    out = _render(a, b)
    assert "Static transforms captured" in out
    # must not report mission A's transform as "removed" — we don't actually
    # know whether mission B still had it
    assert "gps_link" not in out


def test_diff_as_dict_only_contains_changed_sections(fairy_dirs):
    def mutate(h, c):
        c["intent"]["goal"] = "Different"

    a, b = _pair(fairy_dirs, mutate)
    data = diff_ui.diff_as_dict(a, b)
    assert set(data["changes"]) == {"mission_context"}
    assert data["mission_a"]["goal"] != data["mission_b"]["goal"]


@pytest.mark.parametrize("missing", [None, []])
def test_missing_host_package_list_is_one_row_not_every_package_removed(
        fairy_dirs, missing):
    """2026-10-01: a failed capture listed every installed package as
    (removed). Old records stored [] for "not captured", new ones None."""
    def mutate(h, c):
        h["software"]["ros_packages"] = missing
    a, b = _pair(fairy_dirs, mutate)
    out = " ".join(_render(a, b).split())
    assert "host packages captured yes no" in out
    assert "host pkg" not in out


def test_ekf_frequency_and_newly_set_parameter_are_diffed(fairy_dirs):
    """The 2026-10-02 validation: one EKF parameter changed between missions,
    plus a sensor slot going from unset (None) to configured."""
    def ekf(freq, pose0):
        return {"/ekf_filter_node_odom": {"/ekf_filter_node_odom": {
            "ros__parameters": {"frequency": freq, "pose0": pose0,
                                "two_d_mode": True}}}}
    h1, c1 = _spool(fairy_dirs)
    h1["ros_graph"]["nodes"] = ["/navsat", "/ekf_filter_node_odom"]
    h1["ros_graph"]["parameters"].update(ekf(30.0, None))
    a = builder.build(h1, c1)
    h2, c2 = copy.deepcopy(h1), copy.deepcopy(c1)
    h2["ros_graph"]["parameters"].update(ekf(30.1, "/gnss_pose"))
    b = builder.build(h2, c2)
    out = " ".join(_render(a, b).split())
    assert "/ekf_filter_node_odom: frequency 30.0 30.1" in out
    assert "/ekf_filter_node_odom: pose0 (not set) /gnss_pose" in out
    assert "two_d_mode" not in out
