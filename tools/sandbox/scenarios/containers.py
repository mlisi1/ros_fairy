"""The robot's stack runs in a container and moves to ROS_DOMAIN_ID 7 there,
while the host shells stay on 0: doctor finds it through the container's
environment, `setup --refresh-env --from-container` fixes it, and doctor
then points at the shell, not the service. Also: a container on its own
Docker network, and an account that may not use Docker."""

import json
import subprocess
import sys
import time
from pathlib import Path

from fairy_sandbox import (
    IDENTITY,
    data_dir,
    ensure_talkers,
    expect,
    finish,
    record_mission,
    setup_robot,
    sh,
    step,
)

IMAGE = "fake-ros:latest"


def doctor(user: str = "ana", shell: str = "") -> dict[str, dict]:
    out = sh(user, f"{shell} ros2 fairy doctor --json", check=False,
             quiet=True).stdout
    return {c["title"]: c for c in json.loads(out)["checks"]}


def find(checks: dict[str, dict], *words: str) -> dict:
    return next(c for t, c in checks.items() if all(w in t for w in words))


def container(name: str, *options: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", name, *options,
                    "-e", "ROS_DISTRO=jazzy", IMAGE,
                    "busybox", "sleep", "100000"],
                   check=True, capture_output=True)


def watchdog_env(key: str) -> str | None:
    pid = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value",
                          "ros-fairy-watchdog"], capture_output=True,
                         text=True).stdout.strip()
    for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
        name, _, value = item.decode().partition("=")
        if name == key:
            return value
    return None


ensure_talkers()
if not IDENTITY.is_file():
    step("setup")
    expect(setup_robot() == 0, "setup")

step("Docker in the sandbox, and a tiny image standing in for the robot's")
for _ in range(30):
    if subprocess.run(["docker", "info"], capture_output=True).returncode == 0:
        break
    time.sleep(1)
root = Path("/root/fake-ros")
(root / "bin").mkdir(parents=True, exist_ok=True)
subprocess.run(["cp", "/bin/busybox", str(root / "bin/busybox")], check=True)
subprocess.run(f"tar -C {root} -c . | docker import - {IMAGE}", shell=True,
               check=True, capture_output=True)
expect(True, "image ready")

step("the robot's container moves to domain 7 (its nodes too)")
container("navstack", "--network", "host", "-e", "ROS_DOMAIN_ID=7")
ensure_talkers(domain="7", restart=True)

step("doctor: the shell check can't see it, the container check does")
checks = doctor()
expect(find(checks, "Background service", "ROS environment")["status"] == "ok",
       "shell vs service: no difference (this shell is still on 0)")
check = find(checks, "ROS container")
expect(check["status"] == "warn", f"container check warns ({check['title']})")
expect("navstack (as its ROS processes run): ROS_DOMAIN_ID: 0 → 7"
       in check["detail"], check["detail"])
expect("--refresh-env --from-container navstack" in check["hint"],
       "names the command")

step("ben may not use Docker: the watchdog's findings, without names")
check = find(doctor("ben"), "container")
expect(check["status"] == "warn" and "the container whose first process is"
       in check["detail"] and "ROS_DOMAIN_ID: 0 → 7" in check["detail"],
       f"{check['title']}: {check['detail']}")

step("--refresh-env from the host shell sees no robot, and says where it is")
result = sh("ana", "ros2 fairy setup --refresh-env", check=False)
expect(result.returncode == 1, f"refused (exit {result.returncode})")
expect("--from-container navstack" in result.stdout, "suggests the container")

step("--refresh-env --from-container navstack")
result = sh("ana", "ros2 fairy setup --refresh-env --from-container navstack",
            check=False)
expect(result.returncode == 0, f"exit 0 (got {result.returncode})")
expect("container navstack (as its ROS processes run): ROS_DOMAIN_ID=7"
       in result.stdout, "uses the container's settings")
expect("ROS_DOMAIN_ID: 0 → 7" in result.stdout, "shows the change")
expect("ROS_DOMAIN_ID=7" in Path("/etc/ros-fairy/watchdog.env").read_text(),
       "watchdog.env re-captured")
expect(watchdog_env("ROS_DOMAIN_ID") == "7", "the watchdog runs on domain 7")

step("doctor now points at the shell, not the service")
checks = doctor()
expect(find(checks, "ROS container")["status"] == "ok", "containers match")
check = find(checks, "shell")
expect(check["title"].startswith("This shell is on a different ROS partition"),
       check["title"])
expect("export ROS_DOMAIN_ID=7" in check["hint"], check["hint"])
expect("refresh-env" not in check["hint"], "doesn't say to refresh")
checks = doctor(shell="export ROS_DOMAIN_ID=7;")
expect(find(checks, "Background service", "ROS environment")["status"] == "ok",
       "with the export, all is well")

step("a mission from a shell on the robot's domain captures its nodes")
record_mission(shell="export ROS_DOMAIN_ID=7;", goal="Robot in a container")
crate = max((data_dir() / "archive").glob("2*"))
harvest = json.loads((crate / "harvest" / "harvest.json").read_text())
nodes = (harvest.get("ros_graph") or {}).get("nodes") or []
expect("/gps_driver" in nodes, f"nodes captured ({nodes})")
names = [c["name"] for c in
         (harvest.get("software") or {}).get("docker_containers") or []]
expect("navstack" in names, f"the container is in the record ({names})")

step("a ROS container on its own Docker network")
container("cam", "--network", "bridge")
check = find(doctor(), "ROS container")
expect(check["status"] == "warn" and "cam is on its own Docker network"
       in check["detail"], check["detail"])
expect("network_mode: host" in check["hint"] or "from-container" in
       check["hint"], check["hint"])

step("back to how it was")
subprocess.run(["docker", "rm", "-f", "navstack", "cam"], capture_output=True)
ensure_talkers(restart=True)
sh("ana", "ros2 fairy setup --refresh-env", check=False, quiet=True)
check = find(doctor(), "Background service", "ROS environment")
expect(check["status"] == "ok", f"back on domain 0 ({check['title']})")
sys.exit(finish())
