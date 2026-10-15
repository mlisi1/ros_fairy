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
    result = subprocess.run(_login(user, f"COLUMNS=160 {command}"),
                            capture_output=True, text=True)
    if not quiet:
        print(f"$ ({user}) {command}")
        sys.stdout.write(result.stdout)
        sys.stdout.write(result.stderr)
    if check:
        expect(result.returncode == 0,
               f"`{command}` as {user} exited {result.returncode}")
    return result


def interact(user: str, command: str, answers: dict, timeout: int = 90,
             interrupt_after: float | None = None) -> int:
    """Run ``command`` as ``user`` on a terminal, answering prompts.

    ``answers`` maps a regex for a prompt to the reply, or to a list of
    replies used in turn when the prompt repeats. ``interrupt_after``
    interrupts it like Ctrl-C after that many seconds (for mission_record).
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
                                "ros2 fairy"])
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


def ensure_talkers() -> None:
    """Publishers for the sensors the scripted setup declares, as systemd
    units (fairy-talker-*), so they outlive the scenario that started them.

    Named nodes: `ros2 topic pub`'s default name starts with "_", which
    hides it, and setup refuses a robot with no visible nodes.
    """
    started = False
    for node, topic, kind in (
            ("gps_driver", "/fix", "sensor_msgs/msg/NavSatFix"),
            ("lidar_driver", "/scan", "sensor_msgs/msg/LaserScan")):
        unit = f"fairy-talker-{node}"
        if subprocess.run(["systemctl", "is-active", "--quiet", unit]
                          ).returncode == 0:
            continue
        subprocess.run(["systemctl", "reset-failed", unit],
                       capture_output=True)
        subprocess.run(
            ["systemd-run", f"--unit={unit}", "--uid=ben", "--quiet",
             "/bin/bash", "-lc",
             f"exec ros2 topic pub --node-name {node} -r 10 {topic} {kind} "
             "'{}'"], check=True)
        started = True
    if started:
        time.sleep(4)  # discovery


def wait_until_idle(timeout: float = 30) -> None:
    """Until the watchdog has finished with the last recording."""
    state_file = Path("/var/ros-fairy/watchdog.state")
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


def finish() -> int:
    if _failures:
        print(f"\n\033[31m{len(_failures)} check(s) failed:\033[0m")
        for message in _failures:
            print(f"  - {message}")
        return 1
    print("\n\033[32mAll checks passed.\033[0m")
    return 0
