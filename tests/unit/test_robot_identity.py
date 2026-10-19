import pytest

from fairy.harvest import robot_identity
from fairy.harvest.robot_identity import RobotIdentityError


def test_valid_identity(identity_yaml):
    data = robot_identity.harvest()
    assert data["robot"]["name"] == "Heron-02"
    assert data["robot"]["owner_organization"] == "Example Marine Robotics Lab"
    assert data["robot"]["owner_contact"] == "fleet@example.org"
    assert [s["sensor_id"] for s in data["sensors"]] == ["gps0", "sonar0"]
    assert data["sensors"][0]["calibration_ref"] == "gps0_cal"
    assert data["sensors"][1]["calibration_ref"] is None
    assert data["calibrations"][0]["name"] == "gps0_cal"
    assert data["recording"] == {"topics": None, "storage": None}
    assert data["default_license"] is None


def test_missing_file(fairy_dirs):
    with pytest.raises(RobotIdentityError, match="not found"):
        robot_identity.harvest()


def test_missing_required_field(fairy_dirs):
    (fairy_dirs["cfg"] / "robot_identity.yaml").write_text(
        "robot:\n  name: x\n  platform: y\nowner:\n  organization: z\n"
        "  contact_email: a@b.c\n")
    with pytest.raises(RobotIdentityError, match="serial_number"):
        robot_identity.harvest()


def test_duplicate_sensor_id(fairy_dirs, identity_yaml):
    text = identity_yaml.read_text().replace("sensor_id: sonar0",
                                             "sensor_id: gps0")
    identity_yaml.write_text(text)
    with pytest.raises(RobotIdentityError, match="duplicate"):
        robot_identity.harvest()


def test_unknown_calibration_ref(fairy_dirs, identity_yaml):
    text = identity_yaml.read_text().replace("calibration: gps0_cal",
                                             "calibration: nope")
    identity_yaml.write_text(text)
    with pytest.raises(RobotIdentityError, match="unknown calibration"):
        robot_identity.harvest()


def test_invalid_yaml(fairy_dirs):
    (fairy_dirs["cfg"] / "robot_identity.yaml").write_text("{[broken")
    with pytest.raises(RobotIdentityError, match="YAML"):
        robot_identity.harvest()
