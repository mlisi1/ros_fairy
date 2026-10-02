import json
import os

import pytest

from ros_fairy.archive import assembler, index
from ros_fairy.archive.assembler import AssemblyError
from ros_fairy.manifest import builder
from ros_fairy.utils import fsio, paths
from tests.conftest import make_bag

T0 = 1_750_000_000.0


def _steady(start, end, hz):
    n = int((end - start) * hz)
    return [start + i / hz for i in range(n + 1)]


def _spool(fairy_dirs, n_bags=1, with_cal=True):
    """Populate the spool with real bags + harvest + context documents."""
    cal_file = fairy_dirs["cfg"] / "gps0.yaml"
    cal_file.write_text("fx: 1\n")
    identity = {
        "robot": {"name": "Heron-02", "platform": "Clearpath Heron USV",
                  "serial_number": "H02", "owner_organization": "Lab",
                  "owner_contact": "fleet@example.org"},
        "sensors": [{"sensor_id": "gps0", "type": "gps",
                     "make_model": "u-blox ZED-F9P", "topic": "/fix",
                     "frame_id": "gps_link",
                     "calibration_ref": "gps0_cal" if with_cal else None}],
        "calibrations": [{"name": "gps0_cal", "source_path": str(cal_file),
                          "format": "yaml"}] if with_cal else [],
        "default_license": "https://spdx.org/licenses/CC-BY-4.0",
    }
    harvest = builder.compose_harvest(
        identity=identity,
        system={"hostname": "r1", "kernel": "Linux 6.8", "arch": "x86_64",
                "ros_distro": "jazzy", "apt_ros_versions": {}},
        graph={"captured_at": "2026-06-12T14:03:00+00:00",
               "nodes": ["/navsat"],
               "topics": [{"name": "/fix",
                           "type": "sensor_msgs/msg/NavSatFix"}],
               "ros_packages": ["rclpy"], "complete": True,
               "parameters": {"/navsat": {"/navsat": {
                   "ros__parameters": {"frame_id": "gps_link"}}}}},
        docker={"docker_containers": [
            {"name": "navstack", "image": "example/navstack:1.4",
             "digest": "example/navstack@sha256:7be1",
             "compose_project": None, "compose_file": None}],
            "raw_inspect": [{"Name": "/navstack"}], "available": True},
        descriptions={"robot_description": "<robot name='heron'/>",
                      "tf_static": [{"parent_frame": "base", "child_frame":
                                     "gps_link"}]},
        harvest_status={"robot_identity": "ok", "system_info": "ok",
                        "python_env": "ok", "hardware_devices": "partial",
                        "ros_graph": "ok", "ros_descriptions": "ok",
                        "docker_info": "ok"},
        python_env={
            "python_env": {
                "executable": "/opt/venv/bin/python3",
                "version": "3.12.3 (main)",
                "venv_path": "/opt/venv",
                "pip_version": "24.0",
                "packages": [{"name": "rclpy", "version": "5.0.0",
                              "installer": "apt", "editable": False,
                              "location": None}],
                "ros_fairy_editable": False,
                "sys_path": ["/opt/venv/lib/python3.12/site-packages"]},
            "pip_freeze": "rclpy==5.0.0\n",
            "pip_list_json": None},
        hardware_devices={
            "devices": [{"device_class": "usb", "vendor_id": "1546",
                         "product_id": "01a9", "vendor_name": "u-blox AG",
                         "product_name": "ZED-F9P", "serial_number": None,
                         "device_path": None,
                         "bus_path": "Bus 002 Device 003", "driver": None,
                         "source_command": "lsusb",
                         "udev_properties": None}],
            "lsusb_verbose": "Bus 002 Device 003: ID 1546:01a9\n",
            "dmesg_usb": None})
    for i in range(n_bags):
        bag = make_bag(paths.bags_dir() / f"rosbag2_{i}",
                       {"/fix": _steady(T0 + i * 700, T0 + i * 700 + 600, 10)})
        harvest["bags"].append({
            "path": str(bag), "storage_format": "sqlite3",
            "size_bytes": fsio.dir_size_bytes(bag),
            "start_time": "2026-06-12T14:03:00+00:00",
            "end_time": "2026-06-12T14:13:00+00:00",
            "duration_s": 600.0, "message_count": 6001,
            "topics": [{"name": "/fix", "type": "sensor_msgs/msg/NavSatFix",
                        "message_count": 6001, "avg_frequency_hz": 10.0}],
            "health_warnings": []})
    harvest["provenance"]["harvested_at"] = "2026-06-12T14:14:00+00:00"
    context = builder.new_mission_context(
        operator_name="Jane Doe", goal="Survey eelgrass beds",
        location_name="Marsh Creek, north bank", environment="marine")
    fsio.atomic_write_json(paths.harvest_json_path(), harvest)
    fsio.atomic_write_json(paths.mission_context_path(), context)
    return harvest, context


def test_sanitise():
    assert assembler.sanitise("Marsh Creek, north bank") == \
        "marsh-creek-north-bank"
    assert assembler.sanitise("Jane Doe") == "jane-doe"
    assert assembler.sanitise("Forêt d'Orléans!") == "foret-d-orleans"
    assert assembler.sanitise("///") == "unknown"


def test_assemble_full_crate(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    from ros_fairy.utils import ros_env
    ros_env.write_file(paths.session_env_path(), {"ROS_DOMAIN_ID": "7"})
    record = builder.build(harvest, context)
    final = assembler.assemble(record, harvest)

    assert final.parent == paths.archive_dir()
    assert final.name.endswith("_marsh-creek-north-bank_jane-doe")
    # bags moved, not copied
    assert not any(paths.bags_dir().iterdir())
    assert (final / "bags" / "rosbag2_0" / "metadata.yaml").is_file()
    # artifacts
    assert (final / "harvest" / "harvest.json").is_file()
    assert (final / "harvest" / "robot_description.urdf").read_text() == \
        "<robot name='heron'/>"
    assert (final / "harvest" / "tf_static.json").is_file()
    assert (final / "calibrations" / "gps0.yaml").is_file()
    assert (final / "docker" / "containers.json").is_file()
    # python env + hardware raw artifacts extracted (dmesg was None → no file)
    assert (final / "harvest" / "pip_freeze.txt").read_text() == \
        "rclpy==5.0.0\n"
    assert (final / "harvest" / "lsusb_verbose.txt").is_file()
    assert not (final / "harvest" / "dmesg_usb.txt").exists()
    readme = (final / "README.md").read_text()
    assert "Survey eelgrass beds" in readme
    assert "## Connected hardware" in readme
    # manifest updated with crate-relative paths and hashes
    saved = json.loads((final / "mission_record.json").read_text())
    assert saved["bags"][0]["path"] == "bags/rosbag2_0"
    assert saved["ros_graph"]["robot_description"] == \
        "harvest/robot_description.urdf"
    assert saved["calibrations"][0]["archived_path"] == \
        "calibrations/gps0.yaml"
    assert len(saved["calibrations"][0]["sha256"]) == 64
    # spool context cleared (incl. the recording-session env handoff)
    assert not paths.harvest_json_path().exists()
    assert not paths.mission_context_path().exists()
    assert not paths.session_env_path().exists()
    # indexed
    rows, total = index.query()
    assert total == 1
    assert rows[0]["operator"] == "Jane Doe"
    assert rows[0]["archive_path"] == str(final)
    assert rows[0]["bag_count"] == 1


def test_ro_crate_document(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    final = assembler.assemble(record, harvest)
    doc = json.loads((final / "ro-crate-metadata.json").read_text())

    assert doc["@context"][0] == "https://w3id.org/ro/crate/1.1/context"
    assert doc["@context"][1]["sosa"] == "http://www.w3.org/ns/sosa/"
    by_id = {e["@id"]: e for e in doc["@graph"]}

    root = by_id["./"]
    assert root["name"] == "Survey eelgrass beds"
    assert root["license"] == "https://spdx.org/licenses/CC-BY-4.0"
    assert {"@id": "bags/rosbag2_0/"} in root["hasPart"]
    assert {"@id": "mission_record.json"} in root["hasPart"]
    assert "marine" in root["keywords"]

    assert by_id["#operator"]["name"] == "Jane Doe"
    assert by_id["#operator"]["email"] == "fleet@example.org"
    assert by_id["#place"]["name"] == "Marsh Creek, north bank"
    assert by_id["#robot"]["@type"] == ["Thing", "sosa:Platform"]
    assert by_id["#robot"]["sosa:hosts"] == [{"@id": "#sensor-gps0"}]

    sensor = by_id["#sensor-gps0"]
    assert sensor["sosa:isHostedBy"] == {"@id": "#robot"}
    assert sensor["subjectOf"] == {"@id": "calibrations/gps0.yaml"}
    # PropertyValues are hoisted into @id'd entities and referenced
    assert {"@id": "#sensor-gps0-topic"} in sensor["additionalProperty"]
    assert by_id["#sensor-gps0-topic"]["value"] == "/fix"
    assert by_id["#sensor-gps0-topic"]["@type"] == "PropertyValue"

    mission = by_id["#mission"]
    assert mission["agent"] == {"@id": "#operator"}
    assert {"@id": "#robot"} in mission["instrument"]
    assert {"@id": "#ros2"} in mission["instrument"]
    assert {"@id": "#python-runtime"} in mission["instrument"]
    assert {"@id": "#container-navstack"} in mission["instrument"]
    assert mission["startTime"] == "2026-06-12T14:03:00+00:00"

    py = by_id["#python-runtime"]
    assert py["@type"] == "SoftwareApplication"
    assert py["version"] == "3.12.3 (main)"
    py_props = {by_id[ref["@id"]]["name"]: by_id[ref["@id"]]["value"]
                for ref in py["additionalProperty"]}
    assert py_props["executable"] == "/opt/venv/bin/python3"
    assert py_props["venv_path"] == "/opt/venv"

    bag = by_id["bags/rosbag2_0/"]
    assert bag["encodingFormat"] == "application/x-sqlite3"
    first_var = by_id[bag["variableMeasured"][0]["@id"]]
    assert first_var["name"] == "/fix"
    assert first_var["value"] == 6001

    # bag files are File entities with sha256, referenced by the bag's hasPart,
    # and the digests match mission_record.json's bags[].file_sha256
    recorded = record.bags[0].file_sha256
    assert recorded, "expected per-file bag checksums"
    parts = {p["@id"] for p in bag["hasPart"]}
    for rel, digest in recorded.items():
        file_id = f"bags/rosbag2_0/{rel}"
        assert file_id in parts
        assert by_id[file_id]["@type"] == "File"
        assert by_id[file_id]["sha256"] == digest
    assert by_id["bags/rosbag2_0/rosbag2_0_0.db3"]["encodingFormat"] == \
        "application/x-sqlite3"

    # confidence markers share one hoisted entity, referenced by user fields
    assert by_id["#operator"]["additionalProperty"] == {"@id": "#confidence-user"}
    assert by_id["#confidence-user"]["value"] == "user"
    assert by_id["#ros2"]["version"] == "jazzy"
    assert by_id["#container-navstack"]["identifier"] == \
        "example/navstack@sha256:7be1"
    assert by_id["calibrations/gps0.yaml"]["sha256"]
    assert by_id["ro-crate-metadata.json"]["about"] == {"@id": "./"}


def test_readme_flags_identifying_serials():
    from datetime import datetime, timezone

    from ros_fairy.manifest.schema import (
        HardwareDevice,
        Identity,
        Intent,
        MissionRecord,
        Provenance,
        Software,
    )
    rec = MissionRecord(
        identity=Identity(mission_id="m-x",
                          created_at=datetime(2026, 6, 12, tzinfo=timezone.utc),
                          operator_name="Jane"),
        intent=Intent(goal="Survey", location_name="lab"),
        software=Software(ros_fairy_version="0.1.0"),
        hardware_devices=[
            HardwareDevice(source_command="lsusb", device_class="usb",
                           product_name="u-blox ZED-F9P",
                           serial_number="3C123"),
            HardwareDevice(source_command="glob:/dev/video*",
                           device_class="video", device_path="/dev/video0")],
        provenance=Provenance(ros_fairy_version="0.1.0", schema_version="1.0",
                              hostname="h", kernel="Linux", arch="x86_64"))
    md = assembler._render_readme(rec, [])
    assert "## Connected hardware" in md
    assert "u-blox ZED-F9P" in md
    assert "1 of these record a serial number" in md
    assert "Serial numbers can identify" in md

    # no hardware → no section at all
    rec.hardware_devices = []
    assert "Connected hardware" not in assembler._render_readme(rec, [])


def test_archive_name_includes_time_to_seconds(fairy_dirs):
    import re
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    name = assembler.archive_name(record)
    # YYYY-MM-DD_HH-MM-SS_<location>_<operator>
    assert re.match(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_", name)
    assert name.endswith("_marsh-creek-north-bank_jane-doe")


def test_archive_name_collision(fairy_dirs):
    harvest, context = _spool(fairy_dirs, n_bags=2)
    record = builder.build(harvest, context)
    first = assembler.assemble(record, harvest)

    harvest2, context2 = _spool(fairy_dirs)
    context2["identity"]["created_at"] = context["identity"]["created_at"]
    record2 = builder.build(harvest2, context2)
    record2.identity.created_at = record.identity.created_at
    second = assembler.assemble(record2, harvest2)
    assert second.name == first.name + "_2"


def test_failure_before_bag_move_leaves_spool_intact(fairy_dirs, monkeypatch):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)

    def boom(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(assembler.ro_crate, "write", boom)
    with pytest.raises(AssemblyError, match="disk space"):
        assembler.assemble(record, harvest)
    assert (paths.bags_dir() / "rosbag2_0" / "metadata.yaml").is_file()
    assert paths.harvest_json_path().exists()
    assert not any(paths.archive_dir().glob("2026*"))
    assert not list(paths.staging_dir().glob("*"))


def test_bag_move_failure_rolls_back(fairy_dirs, monkeypatch):
    harvest, context = _spool(fairy_dirs, n_bags=2)
    record = builder.build(harvest, context)

    real_move = assembler._move_bag
    calls = {"n": 0}

    def flaky_move(src, dest, progress, *rest):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(5, "Input/output error")
        real_move(src, dest, progress, *rest)

    monkeypatch.setattr(assembler, "_move_bag", flaky_move)
    with pytest.raises(AssemblyError, match="back in the spool"):
        assembler.assemble(record, harvest)
    assert (paths.bags_dir() / "rosbag2_0").is_dir()
    assert (paths.bags_dir() / "rosbag2_1").is_dir()
    assert not list(paths.staging_dir().glob("*"))


def test_resume_legacy_staging_without_plan(fairy_dirs):
    """A complete staging tree left by an older version (no plan file)."""
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    name = assembler.archive_name(record)
    staging = paths.staging_dir() / name
    staging.mkdir(parents=True)
    record.bags = []
    record.provenance.assembled_at = record.identity.created_at
    fsio.atomic_write_json(staging / "mission_record.json",
                           record.model_dump(mode="json"))

    [found] = assembler.pending_saves()
    assert (found.name, found.kind) == (name, assembler.RESUME)
    final = assembler.finish_pending(found)
    assert final.name == name
    rows, total = index.query()
    assert total == 1


def test_commit_flushes_staging_before_rename(fairy_dirs, monkeypatch):
    """A power cut after an unflushed rename left a crate of empty files
    (2026-09-29); everything must be on disk before the rename commits it."""
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    events = []
    real_tree, real_dir = fsio.fsync_tree, fsio.fsync_dir

    def tree(root):
        events.append(("tree", root.parent == paths.staging_dir()))
        real_tree(root)

    def dirsync(path):
        events.append(("dir", path))
        real_dir(path)

    monkeypatch.setattr(fsio, "fsync_tree", tree)
    monkeypatch.setattr(fsio, "fsync_dir", dirsync)
    final = assembler.assemble(record, harvest)
    trees = [e for e in events if e[0] == "tree"]
    assert trees[0] == ("tree", True)  # flushed while still in staging
    assert ("dir", paths.archive_dir()) in events
    assert ("dir", paths.spool_dir()) in events  # spool clearing is durable
    assert (final / "mission_record.json").is_file()
    assert not paths.harvest_json_path().exists()


def test_watchdog_log_archived_with_the_mission(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    paths.watchdog_log_path().write_text(
        "... WARNING harvest module ros_graph failed: no ROS nodes visible\n")
    final = assembler.assemble(builder.build(harvest, context), harvest)
    assert "ros_graph failed" in (final / "harvest" / "watchdog.log").read_text()
    crate = json.loads((final / "ro-crate-metadata.json").read_text())
    assert "harvest/watchdog.log" in json.dumps(crate)
    assert not paths.watchdog_log_path().exists()  # next mission starts clean


def test_find_incomplete_crates(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    final = assembler.assemble(builder.build(harvest, context), harvest)
    assert assembler.find_incomplete_crates() == []
    broken = paths.archive_dir() / "2026-09-29_17-04-31_cut-off"
    (broken / "harvest").mkdir(parents=True)
    assert assembler.find_incomplete_crates() == [broken]
    assert final not in assembler.find_incomplete_crates()


def test_index_filters(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    assembler.assemble(record, harvest)

    rows, total = index.query(operator="jane")
    assert total == 1
    rows, total = index.query(operator="nobody")
    assert total == 0
    rows, total = index.query(location="marsh")
    assert total == 1
    rows, total = index.query(since="2099-01-01")
    assert total == 0
    rows, total = index.query(limit=0)
    assert rows == [] and total == 1


def test_index_quality_filter(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    record.provenance.data_quality = "poor"
    index.insert(record, paths.archive_dir() / "x")

    rows, total = index.query(quality="poor")
    assert total == 1
    rows, total = index.query(quality="ok")
    assert total == 0


def test_index_query_missing_db_is_empty(fairy_dirs):
    # No index yet and no write access wouldn't even create one — a missing
    # file simply means no missions.
    assert index.query() == ([], 0)


def test_exported_mission_ids_missing_db_is_empty(fairy_dirs):
    assert index.exported_mission_ids() == set()


def test_mark_exported_tracks_and_persists(fairy_dirs, tmp_path):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    index.insert(record, paths.archive_dir() / "x")

    assert record.identity.mission_id not in index.exported_mission_ids()
    bundle = tmp_path / "m.zip"
    index.mark_exported(record.identity.mission_id, bundle, "zip", "abc123")
    assert index.exported_mission_ids() == {record.identity.mission_id}

    # Re-marking (e.g. a later --force re-export) just replaces the row.
    index.mark_exported(record.identity.mission_id, bundle, "zip", "def456")
    assert index.exported_mission_ids() == {record.identity.mission_id}


def test_index_query_reads_readonly_db(fairy_dirs):
    # An account that can read but not write the index (not in the ros-fairy
    # group but db/dir readable) must still be able to list missions.
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    index.insert(record, paths.archive_dir() / "x")
    db = paths.index_db_path()
    db.chmod(0o444)
    try:
        rows, total = index.query()
        assert total == 1
    finally:
        db.chmod(0o644)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_index_query_unreadable_raises_plain_error(fairy_dirs):
    # An index this account can't open (e.g. WAL recovery needs directory
    # write access the ros-fairy group has and we don't) must surface as a
    # plain-language IndexUnavailableError, not a traceback.
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    index.insert(record, paths.archive_dir() / "x")
    db = paths.index_db_path()
    db.chmod(0o000)
    try:
        with pytest.raises(index.IndexUnavailableError) as exc:
            index.query()
        assert "ros-fairy' group" in str(exc.value)
    finally:
        db.chmod(0o644)


def test_reindex_rebuilds(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    final = assembler.assemble(record, harvest)
    paths.index_db_path().unlink()
    assert index.reindex() == 1
    rows, total = index.query()
    assert rows[0]["archive_path"] == str(final)


def test_find_similar_flags_mistyped_location(fairy_dirs):
    from ros_fairy.archive import duplicates
    # Save a "Crosslab" mission.
    harvest, context = _spool(fairy_dirs)
    context["intent"]["location_name"] = "Crosslab"
    assembler.assemble(builder.build(harvest, context), harvest)

    # A second mission, same operator/time, location mistyped "Crossloab".
    harvest2, context2 = _spool(fairy_dirs)
    context2["intent"]["location_name"] = "Crossloab"
    context2["identity"]["created_at"] = context["identity"]["created_at"]
    record2 = builder.build(harvest2, context2)

    matches = duplicates.find_similar(record2)
    assert len(matches) == 1
    assert matches[0]["location"] == "Crosslab"
    assert "typo" in duplicates.describe(record2, matches)


def test_duplicate_warning_is_one_line_however_many_matches(fairy_dirs):
    """2026-10-02: seven test missions in a session produced seven near-
    identical "Possible duplicate" warnings; the operator gets one."""
    from datetime import datetime, timedelta
    from ros_fairy.archive import duplicates
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    now = record.identity.created_at
    rows = [{"location": loc, "goal": "val",
             "created_at": (now - timedelta(minutes=m)).isoformat()}
            for loc, m in (("Crosslab", 2), ("Crosslab", 23), ("Crosslab", 51))]
    record.intent.location_name = "Crosslab"
    msg = duplicates.describe(record, rows)
    assert msg.count("You already saved") == 1
    assert "2 minutes ago" in msg and "2 more" in msg
    assert "typo" not in msg  # same place name: nothing to suggest a typo
    assert duplicates.describe(record, []) is None


def test_find_similar_ignores_unrelated(fairy_dirs):
    from ros_fairy.archive import duplicates
    harvest, context = _spool(fairy_dirs)
    context["intent"]["location_name"] = "Crosslab"
    assembler.assemble(builder.build(harvest, context), harvest)

    harvest2, context2 = _spool(fairy_dirs)
    context2["intent"]["location_name"] = "Harbour East Dock"
    context2["identity"]["created_at"] = context["identity"]["created_at"]
    assert duplicates.find_similar(builder.build(harvest2, context2)) == []


def test_find_similar_respects_time_window(fairy_dirs):
    from datetime import timedelta

    from ros_fairy.archive import duplicates
    harvest, context = _spool(fairy_dirs)
    context["intent"]["location_name"] = "Crosslab"
    saved = builder.build(harvest, context)
    assembler.assemble(saved, harvest)

    harvest2, context2 = _spool(fairy_dirs)
    context2["intent"]["location_name"] = "Crosslab"
    record2 = builder.build(harvest2, context2)
    # Same place, but three days later -> well outside the default window.
    record2.identity.created_at = saved.identity.created_at + timedelta(days=3)
    assert duplicates.find_similar(record2) == []


def test_find_similar_does_not_flag_routine_same_day_revisits(fairy_dirs):
    """Reported 2026-09-11: two genuinely separate validation missions at the
    same lab ~20.5h apart were flagged as a possible duplicate. The window
    guards against re-saving the *same outing* twice (a same-sitting
    mistake), not against visiting one site more than once."""
    from datetime import timedelta

    from ros_fairy.archive import duplicates
    harvest, context = _spool(fairy_dirs)
    context["intent"]["location_name"] = "Crosslab"
    saved = builder.build(harvest, context)
    assembler.assemble(saved, harvest)

    harvest2, context2 = _spool(fairy_dirs)
    context2["intent"]["location_name"] = "Crosslab"
    record2 = builder.build(harvest2, context2)
    record2.identity.created_at = saved.identity.created_at + \
        timedelta(hours=20, minutes=33)
    assert duplicates.find_similar(record2) == []


def test_find_similar_still_flags_within_the_new_window(fairy_dirs):
    """The tightened window must still catch the mistake it exists for: an
    accidental same-sitting re-save minutes later."""
    from datetime import timedelta

    from ros_fairy.archive import duplicates
    harvest, context = _spool(fairy_dirs)
    context["intent"]["location_name"] = "Crosslab"
    saved = builder.build(harvest, context)
    assembler.assemble(saved, harvest)

    harvest2, context2 = _spool(fairy_dirs)
    context2["intent"]["location_name"] = "Crosslab"
    record2 = builder.build(harvest2, context2)
    record2.identity.created_at = saved.identity.created_at + \
        timedelta(minutes=20)
    assert len(duplicates.find_similar(record2)) == 1


def test_index_persists_data_quality(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    record.provenance.data_quality = "poor"
    index.insert(record, paths.archive_dir() / "x")
    rows, _ = index.query()
    assert rows[0]["data_quality"] == "poor"


def test_index_migrates_pre_v2_database(fairy_dirs):
    import sqlite3
    # A v1 database has no data_quality column; _connect must add it.
    paths.index_db_path().parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(paths.index_db_path())
    con.execute("CREATE TABLE missions (mission_id TEXT PRIMARY KEY, "
                "created_at TEXT, operator TEXT, location TEXT, goal TEXT, "
                "archive_path TEXT UNIQUE, duration_s REAL, size_bytes INTEGER, "
                "bag_count INTEGER, warning_count INTEGER, robot_name TEXT, "
                "ros_fairy_version TEXT, schema_version TEXT)")
    con.commit()
    con.close()

    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    record.provenance.data_quality = "degraded"
    index.insert(record, paths.archive_dir() / "x")  # must not raise
    rows, _ = index.query()
    assert rows[0]["data_quality"] == "degraded"


def test_index_migrates_pre_v3_database_gets_mission_bags_table(fairy_dirs):
    import sqlite3
    # A v2 database has no mission_bags table; _connect must add it.
    paths.index_db_path().parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(paths.index_db_path())
    con.execute("CREATE TABLE missions (mission_id TEXT PRIMARY KEY, "
                "created_at TEXT, operator TEXT, location TEXT, goal TEXT, "
                "archive_path TEXT UNIQUE, duration_s REAL, size_bytes INTEGER, "
                "bag_count INTEGER, warning_count INTEGER, robot_name TEXT, "
                "ros_fairy_version TEXT, schema_version TEXT, data_quality TEXT)")
    con.commit()
    con.close()

    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    index.insert(record, paths.archive_dir() / "x")  # must not raise


def test_insert_populates_bag_fingerprints(fairy_dirs):
    from ros_fairy.utils.topic_health import bag_fingerprint
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    index.insert(record, paths.archive_dir() / "x")
    match = index.find_bag_duplicate([bag_fingerprint(record.bags[0])])
    assert match is not None
    assert match["mission_id"] == record.identity.mission_id


def test_find_bag_duplicate_excludes_named_mission(fairy_dirs):
    from ros_fairy.utils.topic_health import bag_fingerprint
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    index.insert(record, paths.archive_dir() / "x")
    fp = bag_fingerprint(record.bags[0])
    assert index.find_bag_duplicate(
        [fp], exclude_mission_id=record.identity.mission_id) is None
    assert index.find_bag_duplicate([fp]) is not None


def test_find_bag_duplicate_no_match_for_different_content(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    index.insert(record, paths.archive_dir() / "x")
    assert index.find_bag_duplicate(["not-a-real-fingerprint"]) is None


def test_reindex_repopulates_bag_fingerprints(fairy_dirs):
    from ros_fairy.utils.topic_health import bag_fingerprint
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    final = paths.archive_dir() / "x"
    final.mkdir(parents=True)
    fsio.atomic_write_json(final / "mission_record.json",
                           record.model_dump(mode="json"))
    paths.index_db_path().unlink(missing_ok=True)
    assert index.reindex() == 1
    match = index.find_bag_duplicate([bag_fingerprint(record.bags[0])])
    assert match is not None and match["mission_id"] == \
        record.identity.mission_id


def test_find_exact_duplicate_flags_matching_content(fairy_dirs):
    from ros_fairy.archive import duplicates
    harvest, context = _spool(fairy_dirs)
    saved = builder.build(harvest, context)
    assembler.assemble(saved, harvest)

    # _spool() with the same T0/params reproduces identical bag content —
    # same fingerprint, a different mission_id/timestamp.
    harvest2, context2 = _spool(fairy_dirs)
    record2 = builder.build(harvest2, context2)

    match = duplicates.find_exact_duplicate(record2)
    assert match is not None
    assert match["mission_id"] == saved.identity.mission_id
    msg = duplicates.describe_exact(match)
    assert "same recording" in msg


def test_find_exact_duplicate_no_match_for_different_recording(fairy_dirs):
    from ros_fairy.archive import duplicates
    harvest, context = _spool(fairy_dirs)
    saved = builder.build(harvest, context)
    assembler.assemble(saved, harvest)

    harvest2, context2 = _spool(fairy_dirs)
    # Genuinely different content this time — same-shaped bag, different
    # message count — must not be mistaken for the same recording.
    harvest2["bags"][0]["message_count"] = 42
    harvest2["bags"][0]["topics"][0]["message_count"] = 42
    record2 = builder.build(harvest2, context2)
    assert duplicates.find_exact_duplicate(record2) is None
