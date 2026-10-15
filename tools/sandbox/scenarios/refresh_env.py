"""The robot's ROS environment changes after setup (here ROS_DOMAIN_ID 0 →
7, for every login, sensors included): doctor says the recording assistant
is out of date and names `setup --refresh-env`, which fixes it in one step,
open mission included. Runs on a set-up robot or sets it up first."""

import json
import subprocess
import sys
from pathlib import Path

from fairy_sandbox import (
    BRIEFING,
    IDENTITY,
    data_dir,
    ensure_talkers,
    expect,
    finish,
    interact,
    setup_robot,
    sh,
    step,
    wait_until_idle,
)

PROFILE = Path("/etc/profile.d/zz-robot-domain.sh")


def service_check() -> dict:
    out = sh("ana", "ros2 fairy doctor --json", check=False, quiet=True).stdout
    return next(c for c in json.loads(out)["checks"]
                if "Background service" in c["title"]
                and "ROS" in c["title"] and "reach" not in c["title"]
                and "load" not in c["title"])


def watchdog_env(key: str) -> str | None:
    pid = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value",
                          "ros-fairy-watchdog"], capture_output=True,
                         text=True).stdout.strip()
    environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    for item in environ:
        name, _, value = item.decode().partition("=")
        if name == key:
            return value
    return None


def set_domain(domain: str | None) -> None:
    """The robot's environment, for every login, sensors restarted on it."""
    if domain is None:
        PROFILE.unlink(missing_ok=True)
    else:
        PROFILE.write_text(f"export ROS_DOMAIN_ID={domain}\n")
    subprocess.run("systemctl restart 'fairy-talker-*'", shell=True,
                   check=True)


ensure_talkers()
if not IDENTITY.is_file():
    step("setup")
    expect(setup_robot() == 0, "setup")

step("before the change: doctor is happy")
expect(service_check()["status"] == "ok", "service environment matches")

step("a mission is open when the robot's domain changes to 7")
status = interact("ana", "ros2 fairy mission_start",
                  {**BRIEFING, r"replace it\?": "y",
                   r"Include (them|it) in this new mission\?": "n"})
expect(status == 0, f"mission_start exited {status}")
session = data_dir() / "spool" / "session.env"
expect(session.is_file(), "mission_start wrote the mission's ROS settings")
set_domain("7")

step("doctor says the recording assistant is out of date")
check = service_check()
expect(check["status"] == "warn", f"warns ({check['title']})")
expect("ROS_DOMAIN_ID: 0 → 7" in check["detail"], check["detail"])
expect("ros2 fairy setup --refresh-env" in check["hint"], "names the command")

step("setup --refresh-env fixes it in one step")
result = sh("ana", "ros2 fairy setup --refresh-env", check=False)
expect(result.returncode == 0, f"exit 0 (got {result.returncode})")
expect("ROS_DOMAIN_ID: 0 → 7" in result.stdout, "shows the change")
expect("open mission's ROS settings were updated" in result.stdout,
       "updates the open mission")
expect("ROS_DOMAIN_ID=7" in Path("/etc/ros-fairy/watchdog.env").read_text(),
       "watchdog.env re-captured")
expect("ROS_DOMAIN_ID=7" in session.read_text(), "session.env updated")
expect(watchdog_env("ROS_DOMAIN_ID") == "7",
       "the running watchdog has the new domain")
expect(service_check()["status"] == "ok", "doctor is happy again")

step("a second run has nothing to do")
result = sh("ana", "ros2 fairy setup --refresh-env", check=False)
expect("already up to date" in result.stdout, "already up to date")

step("the open mission records and saves on the new domain")
status = interact("ana", "ros2 fairy mission_record", {}, interrupt_after=5)
expect(status == 0, f"mission_record exited {status}")
wait_until_idle()
status = interact("ana", "ros2 fairy mission_close --yes", {}, timeout=180)
expect(status == 0, f"mission_close exited {status}")
crate = max((data_dir() / "archive").glob("2*"))
graph = json.loads((crate / "harvest" / "harvest.json").read_text())
nodes = (graph.get("ros_graph") or {}).get("nodes") or []
expect("/gps_driver" in nodes, f"the robot's nodes were captured ({nodes})")

step("put the robot back on domain 0")
set_domain(None)
sh("ana", "ros2 fairy setup --refresh-env", check=False, quiet=True)
expect(service_check()["status"] == "ok", "back to normal")
sys.exit(finish())
