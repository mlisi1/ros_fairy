"""ros2 fairy setup's edit flags and --show."""

import argparse
import io

import pytest
import yaml
from rich.console import Console

from ros_fairy.subcommands import doctor, setup, setup_edit


def _args(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    setup.SetupVerb().add_arguments(parser, "fairy")
    return parser.parse_args(list(argv))


@pytest.fixture
def edit(identity_yaml, monkeypatch):
    """Run setup with flags as root (no sudo), with no ROS graph to see.
    Returns (exit code, output)."""
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(setup_edit.os, "geteuid", lambda: 0)
    monkeypatch.setattr(setup, "_live_topics", lambda console: [])

    def run(*argv: str) -> tuple[int, str]:
        console = Console(file=io.StringIO(), width=200)
        code = setup.run(_args(*argv, "--yes"), console=console)
        return code, console.file.getvalue()
    return run


def _saved(identity_yaml) -> dict:
    return yaml.safe_load(identity_yaml.read_text())


def test_robot_and_owner_fields(edit, identity_yaml):
    before = identity_yaml.read_text()
    code, out = edit("--robot-name", "Heron-03", "--contact-email",
                     "lab@example.org", "--license", "CC-BY-4.0")
    assert code == 0, out
    saved = _saved(identity_yaml)
    assert saved["robot"]["name"] == "Heron-03"
    assert saved["owner"]["contact_email"] == "lab@example.org"
    assert saved["owner"]["default_license"] == \
        "https://spdx.org/licenses/CC-BY-4.0"
    assert saved["robot"]["platform"] == "Clearpath Heron USV"  # untouched
    assert "-  name: Heron-02" in out and "+  name: Heron-03" in out
    backup = identity_yaml.with_name("robot_identity.yaml.bak")
    assert backup.read_text() == before
    assert "saved missions keep" in out
    code, _ = edit("--license", "")
    assert "default_license" not in _saved(identity_yaml)["owner"]


def test_add_change_and_remove_sensors_in_one_call(edit, identity_yaml,
                                                   fairy_dirs):
    cal = fairy_dirs["cfg"] / "lidar1.yaml"
    cal.write_text("k: 1\n")
    code, out = edit(
        "--remove-sensor", "sonar0",
        "--set-sensor", "gps0", "topic=gps/fix,frame=",
        "--add-sensor", f'id=lidar1,type=lidar,model="Ouster, OS2",'
                        f'topic=/points,calibration={cal}')
    assert code == 0, out
    saved = _saved(identity_yaml)
    sensors = {s["sensor_id"]: s for s in saved["sensors"]}
    assert set(sensors) == {"gps0", "lidar1"}
    assert sensors["gps0"]["topic"] == "/gps/fix"
    assert "frame_id" not in sensors["gps0"]
    assert sensors["gps0"]["calibration"] == "gps0_cal"   # kept
    assert sensors["lidar1"]["make_model"] == "Ouster, OS2"
    assert sensors["lidar1"]["calibration"] == "lidar1_cal"
    cals = {c["name"]: c["source_path"] for c in saved["calibrations"]}
    assert cals["lidar1_cal"] == str(cal.resolve())
    assert set(cals) == {"gps0_cal", "lidar1_cal"}


def test_removing_a_sensor_drops_its_calibration(edit, identity_yaml):
    assert edit("--remove-sensor", "gps0")[0] == 0
    saved = _saved(identity_yaml)
    assert "calibrations" not in saved
    assert [s["sensor_id"] for s in saved["sensors"]] == ["sonar0"]
    assert edit("--remove-sensor", "sonar0")[0] == 0
    assert "sensors" not in _saved(identity_yaml)


def test_calibration_can_be_removed_or_replaced(edit, identity_yaml,
                                                fairy_dirs):
    new = fairy_dirs["cfg"] / "new_cal.yaml"
    new.write_text("k: 2\n")
    assert edit("--set-sensor", "gps0", f"calibration={new}")[0] == 0
    saved = _saved(identity_yaml)
    assert saved["calibrations"] == [{"name": "gps0_cal",
                                      "source_path": str(new.resolve())}]
    assert edit("--set-sensor", "gps0", "calibration=")[0] == 0
    saved = _saved(identity_yaml)
    assert "calibrations" not in saved
    assert "calibration" not in saved["sensors"][0]


def test_rename_a_sensor(edit, identity_yaml):
    assert edit("--set-sensor", "sonar0", "id=sonar1")[0] == 0
    assert [s["sensor_id"] for s in _saved(identity_yaml)["sensors"]] == \
        ["gps0", "sonar1"]
    code, out = edit("--set-sensor", "sonar1", "id=gps0")
    assert code == 1 and "already taken" in out


def test_recording_section(edit, identity_yaml):
    assert edit("--record-topics", "/fix,depth", "--storage", "mcap")[0] == 0
    assert _saved(identity_yaml)["recording"] == {
        "topics": ["/fix", "/depth"], "storage": "mcap"}
    assert edit("--record-topics", "all", "--storage", "default")[0] == 0
    assert "recording" not in _saved(identity_yaml)


@pytest.mark.parametrize("argv, message", [
    (("--add-sensor", "id=gps0,type=gps,model=X,topic=/a"), "already a sensor"),
    (("--add-sensor", "id=imu0,type=imu"), "model, topic missing"),
    (("--add-sensor", "id=Imu,type=imu,model=X,topic=/i"), "lowercase"),
    (("--add-sensor", "id=imu0,type=thermometer,model=X,topic=/i"),
     "one of gps"),
    (("--add-sensor", "id=imu0,kind=imu"), "isn't KEY=VALUE"),
    (("--add-sensor", 'id=imu0,model="open'), "quote isn't closed"),
    (("--add-sensor", "id=c0,type=camera,model=X,topic=/c,calibration=/no"),
     "calibration file for c0 not found"),
    (("--set-sensor", "lidar9", "topic=/x"), "no sensor 'lidar9'"),
    (("--set-sensor", "gps0", "topic="), "topic can't be empty"),
    (("--remove-sensor", "nope"), "configured: gps0, sonar0"),
    (("--contact-email", "not-an-address"), "email address"),
    (("--robot-name", "x" * 41), "40 characters"),
    (("--license", "not a license"), "SPDX"),
    (("--record-topics", ","), "or `all`"),
    (("--storage", "my storage"), "storage name"),
])
def test_bad_flags_change_nothing(edit, identity_yaml, argv, message):
    before = identity_yaml.read_text()
    code, out = edit(*argv)
    assert code == 1
    assert message in out and "Nothing was changed" in out
    assert identity_yaml.read_text() == before
    assert not identity_yaml.with_name("robot_identity.yaml.bak").exists()


def test_no_change_writes_nothing(edit, identity_yaml):
    code, out = edit("--robot-name", "Heron-02")
    assert code == 0 and "Nothing to change" in out
    assert not identity_yaml.with_name("robot_identity.yaml.bak").exists()


def test_declining_writes_nothing(identity_yaml, monkeypatch):
    before = identity_yaml.read_text()
    monkeypatch.setattr(setup, "_live_topics", lambda console: [])
    monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **k: False)
    console = Console(file=io.StringIO())
    assert setup.run(_args("--robot-name", "Other"), console=console) == 0
    assert identity_yaml.read_text() == before


def test_an_unpublished_topic_is_a_warning_not_a_refusal(edit, identity_yaml,
                                                        monkeypatch):
    monkeypatch.setattr(setup, "_live_topics", lambda console: ["/fix"])
    code, out = edit("--add-sensor", "id=imu0,type=imu,model=X,topic=/imu")
    assert code == 0
    assert "/imu isn't being published right now" in out
    assert "/fix isn't" not in out     # unchanged sensors aren't checked


def test_edits_can_fix_a_configuration_with_a_mistake(edit, identity_yaml):
    saved = _saved(identity_yaml)
    saved["sensors"][1]["topic"] = ""
    identity_yaml.write_text(yaml.safe_dump(saved))
    assert doctor._check_identity()["title"] == \
        "The robot configuration has a mistake"
    code, out = edit("--robot-name", "Heron-03")
    assert code == 1 and "would have a mistake" in out
    assert edit("--set-sensor", "sonar0", "topic=/depth")[0] == 0
    assert doctor._check_identity()["status"] == doctor.OK


def test_without_a_configuration(fairy_dirs, monkeypatch):
    console = Console(file=io.StringIO())
    assert setup.run(_args("--robot-name", "X"), console=console) == 1
    assert "isn't set up yet" in console.file.getvalue()
    assert doctor._check_identity()["title"] == "Robot is not set up"


def test_show(identity_yaml):
    console = Console(file=io.StringIO(), width=200)
    assert setup.run(_args("--show"), console=console) == 0
    out = console.file.getvalue()
    assert "Heron-02 (Clearpath Heron USV)" in out
    assert "Sensor gps0" in out and "calibration " in out
    assert "every topic" in out and "rosbag2's default" in out


@pytest.mark.parametrize("argv", [
    ("--show", "--robot-name", "X"), ("--robot-name", "X", "--var-dir", "/d"),
    ("--refresh-env", "--remove-sensor", "gps0"), ("--show", "--operator",
                                                   "ana")])
def test_edit_flags_stand_alone(identity_yaml, argv):
    before = identity_yaml.read_text()
    console = Console(file=io.StringIO(), width=200)
    assert setup.run(_args(*argv), console=console) == 1
    assert "can't be combined" in console.file.getvalue()
    assert identity_yaml.read_text() == before


def test_the_privileged_half_writes_only_the_file(identity_yaml, monkeypatch):
    """--apply-from-stdin with an edit: robot_identity.yaml and its backup,
    no service install, no group changes."""
    monkeypatch.setattr(setup, "install_service",
                        lambda *a: pytest.fail("installed the service"))
    monkeypatch.setattr(setup, "add_operators",
                        lambda *a: pytest.fail("changed the group"))
    config = _saved(identity_yaml) | {"robot": {
        "name": "R", "platform": "P", "serial_number": "S"}}
    assert setup._apply(Console(file=io.StringIO()), {"edit": config})
    assert _saved(identity_yaml)["robot"]["name"] == "R"
    assert (identity_yaml.stat().st_mode & 0o777) == 0o644


def test_renaming_a_sensor_renames_its_calibration(edit, identity_yaml,
                                                   fairy_dirs):
    # Otherwise a new gps0 with a calibration would take over gps1's entry.
    assert edit("--set-sensor", "gps0", "id=gps1")[0] == 0
    saved = _saved(identity_yaml)
    assert saved["sensors"][0]["calibration"] == "gps1_cal"
    assert [c["name"] for c in saved["calibrations"]] == ["gps1_cal"]
    new = fairy_dirs["cfg"] / "new.yaml"
    new.write_text("k: 2\n")
    code, out = edit("--add-sensor", "id=gps0,type=gps,model=X,topic=/g,"
                                     f"calibration={new}")
    assert code == 0, out
    cals = {c["name"]: c["source_path"]
            for c in _saved(identity_yaml)["calibrations"]}
    assert cals["gps0_cal"] == str(new.resolve())
    assert cals["gps1_cal"] != str(new.resolve())


def test_a_calibration_name_another_sensor_uses_is_not_taken_over(
        edit, identity_yaml, fairy_dirs):
    # gps1 still points at gps0_cal (renamed by hand, say).
    saved = _saved(identity_yaml)
    saved["sensors"][0]["sensor_id"] = "gps1"
    identity_yaml.write_text(yaml.safe_dump(saved))
    new = fairy_dirs["cfg"] / "new.yaml"
    new.write_text("k: 2\n")
    code, out = edit("--add-sensor", "id=gps0,type=gps,model=X,topic=/g,"
                                     f"calibration={new}")
    assert code == 0, out
    saved = _saved(identity_yaml)
    sensors = {s["sensor_id"]: s for s in saved["sensors"]}
    cals = {c["name"]: c["source_path"] for c in saved["calibrations"]}
    assert sensors["gps1"]["calibration"] == "gps0_cal"
    assert cals["gps0_cal"] != str(new.resolve())
    assert cals[sensors["gps0"]["calibration"]] == str(new.resolve())


@pytest.mark.parametrize("key", ["sensors", "calibrations"])
def test_empty_sections_are_treated_as_empty(edit, identity_yaml, key):
    saved = _saved(identity_yaml)
    saved[key] = None
    if key == "sensors":
        saved.pop("calibrations")
    else:
        saved["sensors"][0].pop("calibration")
    identity_yaml.write_text(yaml.safe_dump(saved))
    code, out = edit("--add-sensor", "id=imu0,type=imu,model=X,topic=/imu")
    assert code == 0, out
    assert "imu0" in [s["sensor_id"] for s in _saved(identity_yaml)["sensors"]]


def test_empty_owner_is_filled_in(edit, identity_yaml):
    saved = _saved(identity_yaml)
    saved["owner"] = None
    identity_yaml.write_text(yaml.safe_dump(saved))
    code, out = edit("--license", "CC-BY-4.0")
    assert code == 1 and "Traceback" not in out   # still needs an organization
    code, out = edit("--organization", "Lab", "--contact-email", "a@b.org")
    assert code == 0, out
    assert _saved(identity_yaml)["owner"] == {"organization": "Lab",
                                              "contact_email": "a@b.org"}


@pytest.mark.parametrize("key, value, argv", [
    ("robot", "Jo", ("--robot-name", "Heron")),
    ("recording", "x", ("--storage", "mcap")),
    ("sensors", "gps0", ("--remove-sensor", "gps0")),
])
def test_a_section_of_the_wrong_kind_is_refused_plainly(
        edit, identity_yaml, key, value, argv):
    saved = _saved(identity_yaml)
    saved[key] = value
    identity_yaml.write_text(yaml.safe_dump(saved))
    before = identity_yaml.read_text()
    code, out = edit(*argv)
    assert code == 1
    assert f"`{key}:` in the configuration should be" in out
    assert "Nothing was changed" in out
    assert identity_yaml.read_text() == before
