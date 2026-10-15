"""Helpers for sandbox scenarios: run commands as a robot account, answer
prompts, check outcomes. Runs inside the container, as root."""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pexpect

IDENTITY = Path("/etc/ros-fairy/robot_identity.yaml")
_failures: list[str] = []


def step(title: str) -> None:
    print(f"\n\033[1;36m== {title}\033[0m", flush=True)


def _login(user: str, command: str) -> list[str]:
    """A login shell as ``user`` running ``command`` in their home.

    sudo, not su: su catches Ctrl-C itself and kills the command instead of
    passing it on. Not ``sudo -i`` either: it re-quotes the command, which
    breaks ``$(...)`` and the like.
    """
    return ["sudo", "-u", user, "-H", "bash", "-lc", f"cd && {command}"]


def sh(user: str, command: str, check: bool = True,
       quiet: bool = False) -> subprocess.CompletedProcess:
    """``command`` in a login shell as ``user`` (ROS sourced, groups fresh)."""
    # No stdin: a question nobody expected fails at once instead of hanging.
    result = subprocess.run(_login(user, f"COLUMNS=160 {command}"),
                            capture_output=True, text=True,
                            stdin=subprocess.DEVNULL)
    if not quiet:
        print(f"$ ({user}) {command}")
        sys.stdout.write(result.stdout)
        sys.stdout.write(result.stderr)
    if check:
        expect(result.returncode == 0,
               f"`{command}` as {user} exited {result.returncode}")
    return result


def interact(user: str, command: str, answers: dict, timeout: int = 90,
             interrupt_after: float | None = None,
             interrupt: str = "ros2 fairy") -> int:
    """Run ``command`` as ``user`` on a terminal, answering prompts.

    ``answers`` maps a regex for a prompt to the reply, or to a list of
    replies used in turn when the prompt repeats. ``interrupt_after``
    interrupts it like Ctrl-C after that many seconds (for mission_record):
    the newest process of ``user`` whose command line has ``interrupt``.
    Returns the exit status.
    """
    print(f"$ ({user}) {command}")
    # No styling codes inside the prompts the patterns have to match.
    login = _login(user, f"NO_COLOR=1 TERM=dumb {command}")
    child = pexpect.spawn(login[0], login[1:], encoding="utf-8",
                          timeout=timeout, dimensions=(60, 220))
    child.logfile_read = sys.stdout
    queues = {re.compile(k): (list(v) if isinstance(v, list) else v)
              for k, v in answers.items()}
    patterns = [*queues, pexpect.EOF, pexpect.TIMEOUT]
    deadline = time.monotonic() + interrupt_after if interrupt_after else None
    while True:
        wait = timeout if deadline is None else \
            max(0.1, deadline - time.monotonic())
        index = child.expect(patterns, timeout=wait)
        if index == len(queues):  # EOF
            break
        if index == len(queues) + 1:  # TIMEOUT
            if deadline is not None:
                # What Ctrl-C delivers: SIGINT to the verb's process. (The
                # keystroke itself would stop at sudo's own terminal.)
                subprocess.run(["pkill", "-INT", "-n", "-u", user, "-f",
                                interrupt])
                deadline = None
                continue
            expect(False, f"`{command}` stalled waiting for input")
            child.terminate(force=True)
            break
        reply = queues[patterns[index]]
        if isinstance(reply, list):
            reply = reply.pop(0) if len(reply) > 1 else reply[0]
        child.sendline(reply)
    child.close()
    return child.exitstatus if child.exitstatus is not None else \
        128 + (child.signalstatus or 0)


def expect(condition: bool, message: str) -> bool:
    if condition:
        print(f"\033[32m  ok\033[0m  {message}")
    else:
        print(f"\033[31mFAIL\033[0m  {message}")
        _failures.append(message)
    return condition


TALKERS = (("gps_driver", "/fix", "sensor_msgs/msg/NavSatFix"),
           ("lidar_driver", "/scan", "sensor_msgs/msg/LaserScan"))


def ensure_talkers(domain: str | None = None, restart: bool = False) -> None:
    """Publishers for the sensors the scripted setup declares, as systemd
    units (fairy-talker-*), so they outlive the scenario that started them.
    ``domain`` puts them (the "robot") on another ROS_DOMAIN_ID than the
    login shells, as a stack running in containers would be.

    Named nodes: `ros2 topic pub`'s default name starts with "_", which
    hides it, and setup refuses a robot with no visible nodes.
    """
    started = False
    for node, topic, kind in TALKERS:
        unit = f"fairy-talker-{node}"
        active = subprocess.run(["systemctl", "is-active", "--quiet", unit]
                                ).returncode == 0
        if active and not restart:
            continue
        subprocess.run(["systemctl", "stop", unit], capture_output=True)
        subprocess.run(["systemctl", "reset-failed", unit],
                       capture_output=True)
        prefix = f"export ROS_DOMAIN_ID={domain}; " if domain else ""
        subprocess.run(
            ["systemd-run", f"--unit={unit}", "--uid=ben", "--quiet",
             "/bin/bash", "-lc",
             f"{prefix}exec ros2 topic pub --node-name {node} -r 10 {topic} "
             f"{kind} '{{}}'"], check=True)
        started = True
    if started:
        time.sleep(4)  # discovery


def data_dir() -> Path:
    """Where the robot keeps its missions (/etc/ros-fairy/paths.yaml, as
    ros_fairy reads it)."""
    paths_file = Path("/etc/ros-fairy/paths.yaml")
    if paths_file.is_file():
        for line in paths_file.read_text().splitlines():
            if line.startswith("var_dir:"):
                return Path(line.split(":", 1)[1].strip().strip("'\""))
    return Path("/var/ros-fairy")


def wait_until_idle(timeout: float = 150) -> None:
    """Until the watchdog has finished with the last recording. The harvest
    can take a minute or more: the container sees the host's USB devices,
    and a sleeping one is slow to answer (lsusb -v alone may hit its 20 s
    limit)."""
    state_file = data_dir() / "watchdog.state"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if json.loads(state_file.read_text()).get("state") == "IDLE":
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    expect(False, f"watchdog still busy after {timeout:.0f} s")


SETUP_ANSWERS = {
    r"Robot name": "sandbox-01",
    r"Platform \(make and model\)": "Docker sandbox",
    r"Serial number": "SB-001",
    r"Owning organization": "University of Pisa",
    r"Contact email": "fleet@example.org",
    r"Keep the \d+ sensor": "y",
    r"Add a sensor\?": ["y", "y", "n"],
    r"Sensor id": ["gps0", "lidar0"],
    r"Type \[": ["gps", "lidar"],
    r"Make and model": ["u-blox ZED-F9P", "Ouster OS1"],
    r"Topic:": ["/fix", "/scan"],
    r"isn't being published": "y",
    r"TF frame id": ["gps_link", "lidar_link"],
    r"Calibration file path": "",
    r"Other accounts that will record missions": "",
    r"Where should recordings and saved missions be kept\?": "",
    r"Write this configuration\?": "y",
}


def setup_robot(extra: dict | None = None) -> int:
    ensure_talkers()
    return interact("ana", "ros2 fairy setup", {**SETUP_ANSWERS,
                                                **(extra or {})})


BRIEFING = {
    r"What's your name\?": "Ana",
    r"what is this mission trying to do\?": "Sandbox smoke test",
    r"Where are you\?": "Docker, lab bench",
    r"environment like\?": "indoor",
    r"Anything else worth noting\?": "",
}


def record_mission(user: str = "ana", goal: str = "Sandbox smoke test",
                   seconds: float = 5, shell: str = "") -> None:
    """Briefing, a short recording, save: one mission, checked. ``shell``
    goes before each command (e.g. ``export ROS_DOMAIN_ID=7;``)."""
    status = interact(user, f"{shell} ros2 fairy mission_start",
                      {**BRIEFING, r"what is this mission trying to do\?": goal,
                       r"replace it\?": "y",
                       r"Include (them|it) in this new mission\?": "n"})
    expect(status == 0, f"mission_start exited {status}")
    status = interact(user, f"{shell} ros2 fairy mission_record", {},
                      interrupt_after=seconds)
    expect(status == 0, f"mission_record exited {status}")
    wait_until_idle()
    status = interact(user, f"{shell} ros2 fairy mission_close --yes", {},
                      timeout=180)
    expect(status == 0, f"mission_close exited {status}")


def group_members(group: str = "ros-fairy") -> set[str]:
    line = subprocess.run(["getent", "group", group], capture_output=True,
                          text=True).stdout.strip()
    return set(filter(None, line.split(":")[-1].split(","))) if line else set()


def finish() -> int:
    if _failures:
        print(f"\n\033[31m{len(_failures)} check(s) failed:\033[0m")
        for message in _failures:
            print(f"  - {message}")
        return 1
    print("\n\033[32mAll checks passed.\033[0m")
    return 0
