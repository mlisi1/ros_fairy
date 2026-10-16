"""A container whose start-up script exports its DDS settings: nothing in
its configuration says ROS or domain 7, so `docker inspect` can't tell.
The watchdog sees its processes' environment and writes the discovery
settings, nothing else, to ros_processes.json; doctor and
`setup --from-container` use them. Also: a container whose nodes disagree,
and one that stops."""

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
    setup_robot,
    sh,
    step,
)

IMAGE = "fake-ros:latest"


def doctor(user: str = "ana") -> dict:
    out = sh(user, "ros2 fairy doctor --json", check=False, quiet=True).stdout
    checks = json.loads(out)["checks"]
    return next(c for c in checks if "container" in c["title"].lower())


def container(name: str, script: str) -> None:
    """A container started by a shell script, as an entrypoint would be;
    its configuration has no ROS variables."""
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", name, "--network",
                    "host", IMAGE, "busybox", "sh", "-c", script],
                   check=True, capture_output=True)


def pid_of(name: str) -> int:
    return int(subprocess.run(["docker", "inspect", "-f", "{{.State.Pid}}",
                               name], capture_output=True, text=True,
                              check=True).stdout)


def seen(pid: int | None = None, timeout: float = 20) -> dict:
    """ros_processes.json once it lists (or, pid None, no longer lists
    anything for) the container."""
    path = data_dir() / "ros_processes.json"
    deadline = time.monotonic() + timeout
    doc: dict = {}
    while time.monotonic() < deadline:
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            doc = {}
        pids = [c["pid"] for c in doc.get("containers", [])]
        if (pid in pids) if pid is not None else not pids:
            return doc
        time.sleep(1)
    return doc


ensure_talkers()
if not IDENTITY.is_file():
    step("setup")
    expect(setup_robot() == 0, "setup")

step("Docker in the sandbox, and a tiny image")
for _ in range(30):
    if subprocess.run(["docker", "info"], capture_output=True).returncode == 0:
        break
    time.sleep(1)
root = Path("/root/fake-ros")
(root / "bin").mkdir(parents=True, exist_ok=True)
subprocess.run(["cp", "/bin/busybox", str(root / "bin/busybox")], check=True)
subprocess.run(f"tar -C {root} -c . | docker import - {IMAGE}", shell=True,
               check=True, capture_output=True)
for stale in ("navstack", "mixed"):
    subprocess.run(["docker", "rm", "-f", stale], capture_output=True)
expect(not seen().get("containers"), "no containers listed to begin with")

step("the stack's entrypoint exports ROS_DOMAIN_ID=7 and starts two nodes")
container("navstack", "export ROS_DISTRO=jazzy ROS_DOMAIN_ID=7 "
          "API_TOKEN=s3cr3t; busybox sleep 100000 & busybox sleep 100000 & "
          "wait")
ensure_talkers(domain="7", restart=True)
inspect = json.loads(subprocess.run(["docker", "inspect", "navstack"],
                                    capture_output=True, text=True).stdout)
env = inspect[0]["Config"]["Env"] or []
expect(not any(e.startswith(("ROS_DISTRO", "ROS_DOMAIN_ID")) for e in env),
       f"docker inspect knows nothing of it ({env})")
doc = seen(pid_of("navstack"))
entry = next((c for c in doc.get("containers", [])
              if c["pid"] == pid_of("navstack")), {})
expect(entry.get("settings") == [{"ROS_DISTRO": "jazzy",
                                  "ROS_DOMAIN_ID": "7"}],
       f"the watchdog saw its nodes' settings: {entry}")
expect(entry.get("processes") == 2, "both nodes, not the script")
text = (data_dir() / "ros_processes.json").read_text()
expect("s3cr3t" not in text and "PATH" not in text,
       "nothing else from their environment")

step("doctor sees it")
check = doctor()
print(f"  {check['title']}: {check['detail']}")
expect(check["status"] == "warn", f"warns ({check['title']})")
expect("navstack (as its ROS processes run): ROS_DOMAIN_ID: 0 → 7"
       in check["detail"], check["detail"])
expect("--from-container navstack" in check["hint"], check["hint"])

step("so does an operator who may not use Docker, without its name")
check = doctor("ben")
print(f"  {check['title']}: {check['detail']}")
expect(check["status"] == "warn", f"warns ({check['title']})")
expect(f"the container whose first process is {pid_of('navstack')} (as its "
       "ROS processes run): ROS_DOMAIN_ID: 0 → 7" in check["detail"],
       check["detail"])
expect("docker ps" in check["hint"], check["hint"])

step("setup --refresh-env --from-container navstack takes those settings")
result = sh("ana", "ros2 fairy setup --refresh-env --from-container "
            "navstack", check=False)
expect(result.returncode == 0, f"exit 0 (got {result.returncode})")
expect("navstack (as its ROS processes run): ROS_DOMAIN_ID=7"
       in result.stdout, "says where they came from")
expect("ROS_DOMAIN_ID=7" in Path("/etc/ros-fairy/watchdog.env").read_text(),
       "watchdog.env has domain 7")
check = doctor()
expect(check["status"] == "ok", f"doctor: {check['title']}")

step("a container whose nodes disagree")
container("mixed", "export ROS_DISTRO=jazzy; busybox sleep 100000 & "
          "ROS_DOMAIN_ID=7 busybox sleep 100000 & wait")
seen(pid_of("mixed"))
check = doctor()
expect(check["title"] ==
       "A ROS container's nodes are on different ROS partitions",
       check["title"])
expect("all defaults / ROS_DOMAIN_ID=7" in check["detail"], check["detail"])
result = sh("ana", "ros2 fairy setup --refresh-env --from-container mixed",
            check=False, quiet=True)
expect(result.returncode == 1 and "don't all use the same" in result.stdout,
       "--from-container refuses it")

step("stopped containers leave the file")
subprocess.run(["docker", "rm", "-f", "navstack", "mixed"],
               capture_output=True)
expect(not seen().get("containers"), "the list empties")

step("back to how it was")
ensure_talkers(restart=True)
sh("ana", "ros2 fairy setup --refresh-env", check=False, quiet=True)
expect("ROS_DOMAIN_ID=7" not in
       Path("/etc/ros-fairy/watchdog.env").read_text(), "back on domain 0")
sys.exit(finish())
