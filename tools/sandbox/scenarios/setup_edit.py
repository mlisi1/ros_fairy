"""`ros2 fairy setup`'s edit flags: change one part of the configuration
through sudo, without the wizard. A mission recorded before a sensor was
added still verifies; the next one has the sensor, and `diff` shows it.
A mistake is refused before anything is written; doctor names one made by
hand."""

import json
import sys

import yaml
from fairy_sandbox import (
    IDENTITY,
    ensure_talkers,
    expect,
    finish,
    record_mission,
    setup_robot,
    sh,
    step,
)


def saved() -> dict:
    return yaml.safe_load(IDENTITY.read_text())


IDENTITY_TITLES = ("Robot identity configured", "Robot is not set up",
                   "The robot configuration has a mistake")


def identity_check() -> dict:
    checks = json.loads(sh("ana", "ros2 fairy doctor --json", check=False,
                           quiet=True).stdout)["checks"]
    return next(c for c in checks if c["title"] in IDENTITY_TITLES)


def setup(flags: str, user: str = "ana"):
    return sh(user, f"ros2 fairy setup {flags} --yes", check=False,
              quiet=True)


ensure_talkers()
if not IDENTITY.is_file():
    step("setup")
    expect(setup_robot() == 0, "setup")
sh("ana", "ros2 fairy mission_abort --yes", check=False, quiet=True)
original = IDENTITY.read_text()

step("--show prints the configuration")
result = sh("ana", "ros2 fairy setup --show", quiet=True)
print(result.stdout)
expect("Sensor gps0" in result.stdout and "Sensor lidar0" in result.stdout,
       "both sensors shown")

step("a mission with the two sensors")
record_mission(goal="Before the IMU", seconds=4)

step("--add-sensor through sudo: a diff, a warning, a backup")
result = setup("--add-sensor id=imu0,type=imu,model=\"Xsens MTi-630\","
               "topic=/imu,frame=imu_link")
print(result.stdout)
expect(result.returncode == 0, f"exit 0 (got {result.returncode})")
expect(any(line.startswith("+") and "sensor_id: imu0" in line
           for line in result.stdout.splitlines()), "the diff shows it")
expect("/imu isn't being published right now" in result.stdout,
       "warns that /imu is silent (the talkers publish /fix and /scan)")
expect([s["sensor_id"] for s in saved()["sensors"]] ==
       ["gps0", "lidar0", "imu0"], "saved")
backup = IDENTITY.with_name("robot_identity.yaml.bak")
expect(backup.read_text() == original, "the previous file is the backup")
expect(oct(IDENTITY.stat().st_mode & 0o777) == "0o644", "still 0644")
expect(identity_check()["status"] == "ok", "doctor is happy")

step("the mission from before still verifies")
result = sh("ana", "ros2 fairy verify 1 --json", check=False, quiet=True)
expect(result.returncode == 0, f"exit {result.returncode}")

step("the next mission has the IMU, and diff shows it was added")
record_mission(goal="With the IMU", seconds=4)
record = json.loads(sh("ana", "ros2 fairy list --json --path", quiet=True)
                    .stdout)["missions"][0]
mission = json.loads(open(f"{record['archive_path']}/mission_record.json")
                     .read())
expect("imu0" in [s["sensor_id"] for s in mission["sensors"]],
       "the new mission lists imu0")
diff = json.loads(sh("ana", "ros2 fairy diff --json", check=False,
                     quiet=True).stdout)
sensors = diff.get("changes", {}).get("sensors")
expect(bool(sensors) and "Xsens" in json.dumps(sensors),
       f"diff: sensors changed {sensors}")
result = sh("ana", "ros2 fairy verify 2 --json", check=False, quiet=True)
expect(result.returncode == 0, "the first mission still verifies")

step("--record-topics and --storage are what mission_record uses")
result = setup("--record-topics /fix --storage mcap")
expect(result.returncode == 0, f"exit {result.returncode}")
expect(saved()["recording"] == {"topics": ["/fix"], "storage": "mcap"},
       f"saved: {saved().get('recording')}")

step("a mistake is refused, and nothing is written")
before = IDENTITY.read_text()
result = setup("--set-sensor imu0 type=thermometer")
expect(result.returncode == 1 and "one of gps" in result.stdout,
       f"refused: {result.stdout.strip()}")
expect(IDENTITY.read_text() == before, "file unchanged")

step("an operator without sudo can't write it")
result = setup("--robot-name Other", user="ben")
expect(result.returncode != 0, f"refused (exit {result.returncode})")
expect(IDENTITY.read_text() == before, "file unchanged")

step("a mistake made by hand: doctor names it, a flag fixes it")
broken = saved()
broken["sensors"][2]["topic"] = ""
sh("root", f"cat > {IDENTITY} <<'EOF'\n{yaml.safe_dump(broken)}EOF",
   quiet=True)
check = identity_check()
expect(check["title"] == "The robot configuration has a mistake",
       f"doctor: {check['title']}")
result = setup("--set-sensor imu0 topic=/imu")
expect(result.returncode == 0, "fixed with --set-sensor")

step("back as it was")
result = setup("--remove-sensor imu0 --record-topics all --storage default")
expect(result.returncode == 0, f"exit {result.returncode}")
expect(saved() == yaml.safe_load(original), "the original configuration")
sys.exit(finish())
