import io
from unittest import mock

from rich.console import Console

from ros_fairy.manifest import builder
from ros_fairy.ui import briefing, review, status


def _console():
    return Console(file=io.StringIO(), width=100, force_terminal=False)


def _record():
    harvest = builder.compose_harvest(
        identity={
            "robot": {"name": "Heron-02", "platform": "Heron USV",
                      "serial_number": "H02", "owner_organization": "Lab",
                      "owner_contact": "a@b.c"},
            "sensors": [{"sensor_id": "gps0", "type": "gps",
                         "make_model": "u-blox ZED-F9P", "topic": "/fix",
                         "frame_id": None, "calibration_ref": None}],
            "calibrations": [], "default_license": None},
        system={"hostname": "r1", "kernel": "Linux", "arch": "x86_64",
                "ros_distro": "jazzy", "apt_ros_versions": {}},
        graph={"captured_at": "2026-06-12T14:03:00+00:00", "nodes": [],
               "topics": [{"name": "/fix", "type": "t"}], "ros_packages": [],
               "parameters": {}, "complete": True},
        docker=None, descriptions=None,
        harvest_status={"robot_identity": "ok", "system_info": "ok",
                        "ros_graph": "ok", "ros_descriptions": "timeout",
                        "docker_info": "skipped"})
    harvest["bags"] = [{
        "path": "bags/rosbag2_0", "storage_format": "sqlite3",
        "size_bytes": 3_328_599_041,
        "start_time": "2026-06-12T14:03:00+00:00",
        "end_time": "2026-06-12T14:45:00+00:00",
        "duration_s": 2520.0, "message_count": 100, "topics": [],
        "health_warnings": [{
            "topic": "/fix", "sensor_id": "gps0", "kind": "gap",
            "start_offset_s": 720.0, "duration_s": 243.2,
            "plain_text": "GPS signal was lost for 4 minutes, starting "
                          "12 minutes in."}],
    }]
    context = builder.new_mission_context(
        operator_name="Jane Doe", goal="Survey eelgrass",
        location_name="Marsh Creek")
    return builder.build(harvest, context), harvest


def test_briefing_answers_mapping():
    answers = iter(["Jane Doe", "Survey eelgrass", "Marsh Creek", "", ""])
    with mock.patch.object(briefing.Prompt, "ask",
                           side_effect=lambda *a, **k: next(answers)):
        result = briefing.ask_briefing(console=_console())
    assert result == {"operator_name": "Jane Doe", "goal": "Survey eelgrass",
                      "location_name": "Marsh Creek", "environment": None,
                      "notes": None}


def test_briefing_reasks_required():
    answers = iter(["", "  ", "Jane"])
    with mock.patch.object(briefing.Prompt, "ask",
                           side_effect=lambda *a, **k: next(answers)):
        result = briefing.ask_missing(["operator_name"], console=_console())
    assert result == {"operator_name": "Jane"}


def test_summary_is_plain_language():
    record, harvest = _record()
    console = _console()
    review.show_summary(record, builder.harvest_level_warnings(harvest),
                        console=console)
    out = console.file.getvalue()
    assert "GPS signal was lost for 4 minutes" in out
    assert "Jane Doe" in out
    assert "1 recording, 42 minutes, 3.3 GB" in out
    assert "u-blox ZED-F9P" in out
    # the Sensors list names each sensor by its registered id too, so an
    # operator with two of the same make/model can tell them apart
    assert "u-blox ZED-F9P (gps0)" in out
    # no jargon or raw structures
    assert "/fix" not in out
    assert "243.2" not in out
    assert "{" not in out


def test_summary_deduplicates_identical_warnings_across_bags():
    """A multi-bag mission where the same sensor stayed silent in every bag
    must not repeat the identical warning once per bag (reported 2026-09-11:
    a 2-bag mission showed "Camera produced no data at all" four times for
    two silent cameras — the same fact stated twice each, not four facts)."""
    harvest = builder.compose_harvest(
        identity={
            "robot": {"name": "Heron-02", "platform": "Heron USV",
                      "serial_number": "H02", "owner_organization": "Lab",
                      "owner_contact": "a@b.c"},
            "sensors": [{"sensor_id": "cam0", "type": "camera",
                         "make_model": "Realsense D456", "topic": "/cam",
                         "frame_id": None, "calibration_ref": None}],
            "calibrations": [], "default_license": None},
        system={"hostname": "r1", "kernel": "Linux", "arch": "x86_64",
                "ros_distro": "jazzy", "apt_ros_versions": {}},
        graph={"captured_at": "2026-06-12T14:03:00+00:00", "nodes": [],
               "topics": [], "ros_packages": [], "parameters": {},
               "complete": True},
        docker=None, descriptions=None,
        harvest_status={"robot_identity": "ok", "system_info": "ok",
                        "ros_graph": "ok", "ros_descriptions": "timeout",
                        "docker_info": "skipped"})
    warning = {"topic": "/cam", "sensor_id": "cam0", "kind": "never_published",
              "start_offset_s": None, "duration_s": None,
              "plain_text": "Camera produced no data at all during this "
                            "recording."}
    bag = {"path": "bags/rosbag2_0", "storage_format": "sqlite3",
          "size_bytes": 1000, "start_time": "2026-06-12T14:03:00+00:00",
          "end_time": "2026-06-12T14:04:00+00:00", "duration_s": 60.0,
          "message_count": 0, "topics": [], "health_warnings": [warning]}
    harvest["bags"] = [bag, {**bag, "path": "bags/rosbag2_1"}]
    context = builder.new_mission_context(
        operator_name="Jane Doe", goal="Survey eelgrass",
        location_name="Marsh Creek")
    record = builder.build(harvest, context)
    console = _console()
    review.show_summary(record, builder.harvest_level_warnings(harvest),
                        console=console)
    out = console.file.getvalue()
    assert out.count("Camera produced no data at all") == 1


def test_summary_renders_exact_duplicate_more_assertively_than_possible_one():
    """A content-matched duplicate is a much stronger claim than the fuzzy
    location/time heuristic — distinct styling ('✗', red) and wording, and
    it wins the panel border color."""
    record, harvest = _record()
    console = _console()
    review.show_summary(
        record, builder.harvest_level_warnings(harvest), console=console,
        duplicates=["You already saved a mission 20 minutes ago at "
                   '"Marsh Creek" ("Survey eelgrass").'],
        exact_duplicate='This looks like the same recording as the mission '
                       'you already saved as "Survey eelgrass" at "Marsh '
                       'Creek" — not just a similar one.')
    out = console.file.getvalue()
    assert "Duplicate recording" in out
    assert "✗" in out
    assert "Possible duplicate" in out
    assert "⚠" in out


def test_summary_renders_compressed_transport_as_a_note_not_a_warning():
    """A compressed_transport health_warning is informational: rendered
    with 'ℹ' (not '⚠'), and the sensor still shows as OK in the Sensors
    list — it did publish, just not on the exact declared topic."""
    from ros_fairy.manifest import quality as quality_mod
    harvest = builder.compose_harvest(
        identity={
            "robot": {"name": "Heron-02", "platform": "Heron USV",
                      "serial_number": "H02", "owner_organization": "Lab",
                      "owner_contact": "a@b.c"},
            "sensors": [{"sensor_id": "cam0", "type": "camera",
                         "make_model": "Realsense D456", "topic": "/cam",
                         "frame_id": None, "calibration_ref": None}],
            "calibrations": [], "default_license": None},
        system={"hostname": "r1", "kernel": "Linux", "arch": "x86_64",
                "ros_distro": "jazzy", "apt_ros_versions": {}},
        graph={"captured_at": "2026-06-12T14:03:00+00:00", "nodes": ["/cam_node"],
               "topics": [{"name": "/cam", "type": "t"}], "ros_packages": [],
               "parameters": {}, "complete": True},
        docker=None, descriptions=None,
        harvest_status={"robot_identity": "ok", "system_info": "ok",
                        "ros_graph": "ok", "ros_descriptions": "ok",
                        "docker_info": "skipped"})
    note = {"topic": "/cam/compressed", "sensor_id": "cam0",
           "kind": "compressed_transport",
           "start_offset_s": None, "duration_s": None,
           "plain_text": "Camera was recorded through its compressed "
                         "stream instead of the raw one."}
    harvest["bags"] = [{
        "path": "bags/rosbag2_0", "storage_format": "sqlite3",
        "size_bytes": 1000, "start_time": "2026-06-12T14:03:00+00:00",
        "end_time": "2026-06-12T14:04:00+00:00", "duration_s": 60.0,
        "message_count": 100, "topics": [], "health_warnings": [note],
    }]
    context = builder.new_mission_context(
        operator_name="Jane Doe", goal="Survey eelgrass",
        location_name="Marsh Creek")
    record = builder.build(harvest, context)
    quality = quality_mod.assess(record, harvest)
    assert quality.level == quality_mod.OK

    console = _console()
    review.show_summary(record, builder.harvest_level_warnings(harvest),
                        console=console, quality=quality)
    out = console.file.getvalue()
    assert "ℹ" in out
    assert "compressed stream" in out
    assert "⚠" not in out
    assert "✓ Realsense D456" in out


def _gap_harvest_with_topics(n: int):
    harvest = builder.compose_harvest(
        identity={
            "robot": {"name": "Heron-02", "platform": "Heron USV",
                      "serial_number": "H02", "owner_organization": "Lab",
                      "owner_contact": "a@b.c"},
            "sensors": [], "calibrations": [], "default_license": None},
        system={"hostname": "r1", "kernel": "Linux", "arch": "x86_64",
                "ros_distro": "jazzy", "apt_ros_versions": {}},
        graph={"captured_at": "2026-06-12T14:03:00+00:00", "nodes": [],
               "topics": [], "ros_packages": [], "parameters": {},
               "complete": True},
        docker=None, descriptions=None,
        harvest_status={"robot_identity": "ok", "system_info": "ok",
                        "ros_graph": "ok", "ros_descriptions": "timeout",
                        "docker_info": "skipped"})
    gap_warnings = [{
        "topic": f"/topic{i}", "sensor_id": None, "kind": "gap",
        "start_offset_s": 1.0, "duration_s": 2.0,
        "plain_text": f"One of the recorded data channels (/topic{i}) data "
                      "was lost for 2 seconds, starting 1 second in.",
    } for i in range(n)]
    harvest["bags"] = [{
        "path": "bags/rosbag2_0", "storage_format": "sqlite3",
        "size_bytes": 1000, "start_time": "2026-06-12T14:03:00+00:00",
        "end_time": "2026-06-12T14:04:00+00:00", "duration_s": 60.0,
        "message_count": 100, "topics": [], "health_warnings": gap_warnings,
    }]
    context = builder.new_mission_context(
        operator_name="Jane Doe", goal="Survey eelgrass",
        location_name="Marsh Creek")
    return builder.build(harvest, context), harvest


def test_summary_collapses_many_gap_topics():
    """More than GAP_SUMMARY_THRESHOLD distinct topics with a gap must not
    each get their own line — one flaky network dropping a dozen topics at
    once shouldn't drown the review in a dozen near-identical bullets."""
    record, harvest = _gap_harvest_with_topics(5)
    console = _console()
    review.show_summary(record, builder.harvest_level_warnings(harvest),
                        console=console)
    out = console.file.getvalue()
    assert "5 recorded channels dropped data" in out
    assert "mission record" in out
    assert "/topic0" not in out
    assert "was lost for 2 seconds" not in out


def test_summary_itemises_gap_topics_at_or_below_threshold():
    """At or below the threshold, individual topics still get their own,
    more specific line — collapsing only kicks in once it's actually
    needed."""
    record, harvest = _gap_harvest_with_topics(3)
    console = _console()
    review.show_summary(record, builder.harvest_level_warnings(harvest),
                        console=console)
    out = console.file.getvalue()
    assert "recorded channels dropped data" not in out
    assert out.count("was lost for 2 seconds") == 3


def test_confirm_save_paths():
    with mock.patch.object(review.Confirm, "ask", side_effect=[True]):
        assert review.confirm_save(_console()) == "save"
    with mock.patch.object(review.Confirm, "ask", side_effect=[False, True]):
        assert review.confirm_save(_console()) == "discard"
    with mock.patch.object(review.Confirm, "ask", side_effect=[False, False]):
        assert review.confirm_save(_console()) == "keep"


def test_status_lines(fairy_dirs):
    assert "not running" in status.assistant_line(None)
    dead = {"pid": 99999999, "state": "IDLE"}
    assert "not running" in status.assistant_line(dead)

    import os
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    alive = {"pid": os.getpid(), "state": "IDLE", "heartbeat_at": now,
             "since": now}
    assert "watching" in status.assistant_line(alive)
    alive["state"] = "RECORDING"
    assert status.assistant_line(alive).startswith("recording")
    alive["state"] = "FINALISING"
    assert "wrapping up" in status.assistant_line(alive)
    # a state file with null times (hand-edited, or an older version)
    nulls = dict(alive, state="RECORDING", heartbeat_at=None, since=None)
    assert status.assistant_line(nulls) == "recording"

    lines = status.harvest_lines({"harvest_status": {
        "ros_graph": "failed", "docker_info": "skipped",
        "system_info": "ok", "hardware_devices": "partial"}})
    assert "✗ software versions and settings — will keep trying" in lines
    assert "– container software (not used on this robot)" in lines
    assert "✓ computer details" in lines
    assert "⚠ connected hardware (partial)" in lines


def test_show_status_renders(fairy_dirs):
    console = _console()
    status.show_status(None, None, console=console)
    out = console.file.getvalue()
    assert "not started yet" in out
    assert "ros-fairy status" in out


def test_briefing_asks_again_for_a_too_long_answer():
    import io
    from unittest import mock

    from rich.console import Console

    from ros_fairy.ui import briefing
    console = Console(file=io.StringIO(), width=200)
    with mock.patch.object(briefing.Prompt, "ask",
                           side_effect=["x" * 281, "Map the pier"]):
        assert briefing._ask_one(console, "goal", "Goal?", True, None) == \
            "Map the pier"
    assert "at most 280 characters" in console.file.getvalue()
