"""Where missions are kept: chosen at setup, moved on the same disk and to
another one, read by every shell and the service, and found by uninstall.
Needs a fresh robot (`sandbox.sh reset && sandbox.sh install`)."""

import json
import subprocess
import sys
from pathlib import Path

from fairy_sandbox import (
    IDENTITY,
    expect,
    finish,
    record_mission,
    setup_robot,
    sh,
    step,
)

PATHS = Path("/etc/ros-fairy/paths.yaml")
DISK2 = Path("/mnt/disk2")


def listed(user: str = "ana") -> list[dict]:
    result = sh(user, "ros2 fairy list --all --json", check=False, quiet=True)
    return json.loads(result.stdout).get("missions", [])


def watchdog_uses(folder: str) -> bool:
    active = subprocess.run(["systemctl", "is-active", "--quiet",
                             "ros-fairy-watchdog"]).returncode == 0
    return active and Path(folder, "watchdog.state").is_file()


if IDENTITY.is_file():
    print("This scenario needs a fresh robot: sandbox.sh reset, then install.")
    sys.exit(2)

step("setup keeps missions in /data/fairy (asked by the wizard)")
status = setup_robot({r"Where should recordings and saved missions be kept\?":
                      ["data/fairy", "/data/fairy"],
                      r"Other accounts that will record missions": "ben"})
expect(status == 0, f"setup exited {status}")
expect(PATHS.is_file() and "var_dir: /data/fairy" in PATHS.read_text(),
       "paths.yaml written")
expect(Path("/data/fairy/spool/bags").is_dir(), "folders created there")
expect(not Path("/var/ros-fairy").exists(), "nothing in /var/ros-fairy")
expect(watchdog_uses("/data/fairy"), "the watchdog runs from /data/fairy")

step("a mission is saved there")
record_mission(goal="Saved on the data disk")
missions = listed()
expect(len(missions) == 1 and missions[0]["archive_path"].startswith(
    "/data/fairy/archive/"), "listed from /data/fairy")

step("setup won't orphan existing missions")
result = sh("ana", "ros2 fairy setup --var-dir /srv/other", check=False)
expect(result.returncode == 1, f"exit 1 (got {result.returncode})")
expect("ros2 fairy move_data /srv/other" in result.stdout,
       "points at move_data")

step("move_data refuses a folder inside the current one")
result = sh("ana", "ros2 fairy move_data /data/fairy/inner --yes",
            check=False)
expect(result.returncode == 1 and "inside one another" in result.stdout,
       "refused")

step("move_data on the same disk, back to the default")
result = sh("ana", "ros2 fairy move_data /var/ros-fairy --yes", check=False)
expect(result.returncode == 0, f"exit 0 (got {result.returncode})")
expect("same disk" in result.stdout, "said it's a rename")
expect(not PATHS.exists(), "paths.yaml removed (default)")
expect(not Path("/data/fairy").exists(), "old folder gone")
missions = listed("ben")
expect(len(missions) == 1 and missions[0]["archive_path"].startswith(
    "/var/ros-fairy/archive/"), "ben sees it at once, at the new place")
expect(watchdog_uses("/var/ros-fairy"), "the watchdog was restarted there")
sh("ana", "ros2 fairy verify 1")

step("move_data to another disk (copy, check, remove the original)")
subprocess.run(["mkdir", "-p", str(DISK2)], check=True)
subprocess.run(["mount", "-t", "tmpfs", "-o", "size=4g", "tmpfs",
                str(DISK2)], check=True)
result = sh("ana", f"ros2 fairy move_data {DISK2}/fairy --yes", check=False)
expect(result.returncode == 0, f"exit 0 (got {result.returncode})")
expect("another disk" in result.stdout, "said it's a copy")
expect(f"var_dir: {DISK2}/fairy" in PATHS.read_text(), "paths.yaml updated")
expect(not Path("/var/ros-fairy").exists(), "original removed")
expect(not any(DISK2.glob(".*moving")), "no work folder left")
group = subprocess.run(["stat", "-c", "%G %a", f"{DISK2}/fairy/spool"],
                       capture_output=True, text=True).stdout.split()
expect(group == ["ros-fairy", "2775"], f"spool keeps its group (got {group})")
sh("ana", "ros2 fairy verify 1")
expect(watchdog_uses(f"{DISK2}/fairy"), "the watchdog runs from there")

step("a new mission goes to the new disk")
record_mission(goal="Recorded after the move")
missions = listed()
expect(len(missions) == 2 and all(m["archive_path"].startswith(
    f"{DISK2}/fairy/") for m in missions), "both listed from the new disk")

step("doctor says where they are")
doc = json.loads(sh("ana", "ros2 fairy doctor --json", check=False,
                    quiet=True).stdout)
titles = [c["title"] for c in doc["checks"]]
expect(f"Missions are kept in {DISK2}/fairy" in titles, "doctor check")

step("a broken paths.yaml is reported plainly")
saved = PATHS.read_text()
PATHS.write_text("var_dir: relative/path\n")
result = sh("ana", "ros2 fairy list", check=False)
expect(result.returncode == 1 and "isn't an absolute path" in
       result.stdout + result.stderr, "plain message")
expect("something went wrong" not in result.stderr, "not 'unexpected'")
PATHS.write_text(saved)

step("uninstall.sh finds the moved archive")
result = subprocess.run(["bash", "-c", "cd /home/ana/ros_fairy && "
                         "./uninstall.sh --yes"],
                        capture_output=True, text=True)
print(result.stdout[-800:])
expect(f"{DISK2}/fairy/archive" in result.stdout, "names the archive")
expect(Path(f"{DISK2}/fairy/archive").is_dir(), "and keeps it")
sys.exit(finish())
