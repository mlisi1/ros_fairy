"""Take the documentation's terminal screenshots on the sandbox robot.

Runs inside the sandbox container, as root (like the scenarios); started by
take.sh next to it. It installs a virtual X server and terminator, sets the
robot up through the real wizard, records a few missions, and photographs
each command in a terminator window as ana, typing like a person would
(xdotool). The PNGs land in OUT; take.sh copies them to
docs/assets/screenshots/.

Order matters: each shot leaves the robot in the state the next one needs.
"""

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scenarios"))
from fairy_sandbox import (  # noqa: E402
    IDENTITY,
    ensure_talkers,
    expect,
    finish,
    interact,
    sh,
    step,
    wait_until_idle,
)

HERE = Path(__file__).resolve().parent
OUT = Path(os.environ.get("SHOTS_OUT", "/var/tmp/fairy-screenshots"))
WORK = Path("/var/tmp/fairy-shots-work")  # not /tmp: ana's X files live here
DISPLAY = ":42"
USER = "ana"
COLS = 100
BACKGROUND = "#26303D"
PAD = 18  # pixels of background around the text
PROMPT = "ana@robot:~$ "
PACKAGES = ["xvfb", "terminator", "xdotool", "imagemagick", "pngquant",
            "fonts-jetbrains-mono", "dbus-x11"]

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)"
                  r"|\x1b[()][0-9A-B]|\r")


def run(*command: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=True, text=True,
                          capture_output=True, **kwargs)


def x(*command: str) -> str:
    """An X client (xdotool, import) on the virtual display."""
    return run(*command, env={**os.environ, "DISPLAY": DISPLAY}).stdout


# --------------------------------------------------------------------------
# The desktop: Xvfb and a terminator window per shot

def install_tools() -> None:
    missing = [p for p in PACKAGES if subprocess.run(
        ["dpkg", "-s", p], capture_output=True).returncode != 0]
    if missing:
        step(f"installing {' '.join(missing)}")
        run("apt-get", "update", "-qq")
        run("apt-get", "install", "-y", "-qq", "--no-install-recommends",
            *missing, env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})


def start_display() -> None:
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    shutil.chown(WORK, USER, USER)
    # terminator rewrites its config file, so it gets its own copy.
    config = WORK / "terminator.config"
    shutil.copy(HERE / "terminator.config", config)
    shutil.chown(config, USER, USER)
    # A plain prompt, ROS sourced, and a terminal Rich draws in colour for.
    rc = WORK / "bashrc"
    rc.write_text(
        "[ -f /etc/bash.bashrc ] && . /etc/bash.bashrc\n"
        "[ -f ~/.bashrc ] && . ~/.bashrc\n"
        "export TERM=xterm-256color COLORTERM=truecolor\n"
        "unset NO_COLOR\n"
        f"PS1='{PROMPT}'\n"
        "cd\n")
    if subprocess.run(["pgrep", "-x", "Xvfb"],
                      capture_output=True).returncode != 0:
        subprocess.Popen(["sudo", "-u", USER, "Xvfb", DISPLAY, "-screen", "0",
                          "2400x2400x24", "-nolisten", "tcp"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)


class Terminal:
    """A terminator window running bash as ana, its output logged by
    script(1) so we can wait for what's on screen."""

    def __init__(self, rows: int = 60, cols: int = COLS):
        self.log = WORK / "typescript"
        self.log.unlink(missing_ok=True)
        self.seen = 0  # how much of the log the waits have consumed
        shell = f"script -qf -c 'bash --rcfile {WORK}/bashrc -i' {self.log}"
        self.proc = subprocess.Popen(
            ["sudo", "-u", USER, "-H", "env", f"DISPLAY={DISPLAY}",
             "NO_AT_BRIDGE=1", "terminator", "-u", "-g",
             str(WORK / "terminator.config"), "-x", "bash", "-c", shell],
            cwd=f"/home/{USER}", stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 20
        while True:
            found = subprocess.run(
                ["xdotool", "search", "--onlyvisible", "--class",
                 "terminator"], capture_output=True, text=True,
                env={**os.environ, "DISPLAY": DISPLAY}).stdout.split()
            if found:
                self.window = found[-1]
                break
            if time.monotonic() > deadline:
                raise RuntimeError("terminator didn't open a window")
            time.sleep(0.2)
        # Cells, not pixels: terminator sets size hints (geometry_hinting).
        x("xdotool", "windowsize", "--usehints", self.window, str(cols),
          str(rows))
        x("xdotool", "windowfocus", "--sync", self.window)
        x("xdotool", "mousemove", "--window", self.window, "5", "5")
        self.wait_for(re.escape(PROMPT))

    def text(self) -> str:
        try:
            return ANSI.sub("", self.log.read_bytes().decode("utf-8",
                                                             "replace"))
        except FileNotFoundError:
            return ""

    def wait_for(self, pattern: str, timeout: float = 60) -> re.Match:
        regex = re.compile(pattern)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = regex.search(self.text(), self.seen)
            if match:
                self.seen = match.end()
                return match
            time.sleep(0.2)
        tail = self.text()[self.seen:][-800:]
        raise RuntimeError(f"timed out waiting for {pattern!r}; screen "
                           f"ends with:\n{tail}")

    def type(self, text: str, enter: bool = True) -> None:
        time.sleep(0.4)  # a person reads the question first
        if text:
            x("xdotool", "type", "--delay", "35", "--", text)
        if enter:
            x("xdotool", "key", "Return")

    def command(self, command: str) -> None:
        self.type(command)
        self.wait_for(re.escape(command))

    def finished(self, timeout: float = 120) -> None:
        """Until the shell prompts again."""
        self.wait_for(re.escape(PROMPT), timeout)

    def converse(self, answers: dict, until: str, timeout: float = 120
                 ) -> None:
        """Answer prompts (regex -> reply, or a list used in turn) until
        ``until`` shows; that one is left unanswered."""
        queues = {k: (list(v) if isinstance(v, list) else v)
                  for k, v in answers.items()}
        combined = re.compile("|".join(f"(?P<a{i}>{k})" for i, k in
                                       enumerate([until, *queues])))
        keys = list(queues)
        deadline = time.monotonic() + timeout
        while True:
            match = combined.search(self.text(), self.seen)
            if not match:
                if time.monotonic() > deadline:
                    raise RuntimeError("stalled; screen ends with:\n"
                                       + self.text()[-800:])
                time.sleep(0.2)
                continue
            self.seen = match.end()
            index = int(match.lastgroup[1:])
            if index == 0:
                return
            reply = queues[keys[index - 1]]
            if isinstance(reply, list):
                reply = reply.pop(0) if len(reply) > 1 else reply[0]
            self.type(reply)

    def shot(self, name: str, settle: float = 1.0) -> Path:
        """The window as a PNG: cut below the last line of text, padded,
        reduced to a palette."""
        time.sleep(settle)
        raw = WORK / f"{name}-raw.png"
        x("import", "-window", self.window, str(raw))
        # The bounding box of everything that isn't background.
        box = run("convert", str(raw), "-fuzz", "8%", "-trim",
                  "-format", "%h %Y", "info:").stdout.split()
        height, top = int(box[0]), int(box[1].lstrip("+"))
        width = int(run("identify", "-format", "%w", str(raw)).stdout)
        bottom = top + height + 4  # the ibeam cursor's descent
        OUT.mkdir(parents=True, exist_ok=True)
        out = OUT / f"{name}.png"
        run("convert", str(raw), "-crop", f"{width}x{bottom}+0+0", "+repage",
            "-bordercolor", BACKGROUND, "-border", str(PAD), "-strip",
            str(raw))
        run("pngquant", "--force", "--quality", "70-95", "--speed", "1",
            "--strip", "--output", str(out), str(raw))
        print(f"  shot  {out} ({out.stat().st_size // 1024} KiB)")
        return out

    def close(self) -> None:
        subprocess.run(["pkill", "-u", USER, "-f", "bin/terminator"])
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        time.sleep(0.5)


def shoot(name: str, command: str, rows: int = 60, cols: int = COLS,
          answers: dict | None = None,
          until: str | None = None, then: list[str] | None = None,
          timeout: float = 180) -> None:
    """Type ``command``; answer prompts until ``until`` (or until the
    prompt returns); shoot; then type the ``then`` replies, if any, and let
    the command finish."""
    step(f"screenshot: {name}")
    term = Terminal(rows, cols)
    try:
        term.command(command)
        if until:
            term.converse(answers or {}, until, timeout)
        else:
            term.converse(answers or {}, re.escape(PROMPT), timeout)
        term.shot(name)
        if until and then is not None:
            for reply in then:
                term.type(reply)
            term.finished(timeout)
    finally:
        term.close()


# --------------------------------------------------------------------------
# The robot

ROBOT = {
    r"Robot name": "scout-02",
    r"Platform \(make and model\)": "Clearpath Husky A200",
    r"Serial number": "A200-0571",
    r"Owning organization": "University of Pisa",
    r"Contact email": "robots@example.org",
    r"Add a sensor\?": ["y", "y", "n"],
    r"Sensor id": ["gps0", "lidar0"],
    r"Type \[": ["gps", "lidar"],
    r"Make and model": ["u-blox ZED-F9P", "Ouster OS1-64"],
    r"Topic:": ["/fix", "/scan"],
    r"TF frame id": ["gps_link", "lidar_link"],
    r"Calibration file path": "",
    r"Other accounts that will record missions": "ben cleo",
    r"Where should recordings and saved missions be kept\?": "",
}

IMU = ("id=imu0,type=imu,model=\"Xsens MTi-630\",topic=/imu,"
       "frame=imu_link")


def robot_node(name: str, command: str) -> None:
    """A node of the "robot" as a systemd unit, like the GPS and lidar
    talkers fairy_sandbox starts (fairy-talker-*)."""
    unit = f"fairy-talker-{name}"
    if subprocess.run(["systemctl", "is-active", "--quiet", unit]
                      ).returncode == 0:
        return
    subprocess.run(["systemctl", "reset-failed", unit], capture_output=True)
    run("systemd-run", f"--unit={unit}", "--uid=ben", "--quiet",
        "/bin/bash", "-lc", f"exec {command}")
    time.sleep(4)  # discovery


def base_controller() -> None:
    """A node with parameters (the talkers have none), for diff to show."""
    robot_node("base_controller", f"python3 {HERE}/base_controller.py")


def imu_talker() -> None:
    """A publisher for the IMU added between two missions."""
    robot_node("imu_driver", "ros2 topic pub --node-name imu_driver -r 50 "
               "/imu sensor_msgs/msg/Imu '{}'")


def briefing(operator: str, goal: str, location: str, environment: str = "",
             notes: str = "") -> dict:
    return {
        r"What's your name\?": operator,
        r"what is this mission trying to do\?": goal,
        r"Where are you\?": location,
        r"environment like\?": environment,
        r"Anything else worth noting\?": notes,
        r"replace it\?": "y",
        r"Include (them|it) in this new mission\?": "n",
    }


def mission(user: str, seconds: float = 6, **answers) -> None:
    """A whole mission without the camera: briefing, recording, save."""
    status = interact(user, "ros2 fairy mission_start", briefing(**answers))
    expect(status == 0, f"mission_start exited {status}")
    record(user, seconds)
    status = interact(user, "ros2 fairy mission_close --yes", {},
                      timeout=240)
    expect(status == 0, f"mission_close exited {status}")


def record(user: str, seconds: float) -> None:
    status = interact(user, "ros2 fairy mission_record", {},
                      interrupt_after=seconds)
    expect(status == 0, f"mission_record exited {status}")
    wait_until_idle(240)


def main() -> int:
    install_tools()
    start_display()
    ensure_talkers()
    base_controller()
    if IDENTITY.is_file():
        print("This robot is already set up: run `tools/sandbox/sandbox.sh "
              "reset && tools/sandbox/sandbox.sh install` first.")
        return 1

    # setup: the whole wizard, typed; the shot is its review, before
    # writing. Earlier questions scroll off the top.
    shoot("setup", "ros2 fairy setup", rows=46, answers=ROBOT,
          until=r"Write this configuration\?", then=["y"])
    expect(IDENTITY.is_file(), "setup wrote the configuration")
    wait_until_idle()

    shoot("doctor", "ros2 fairy doctor")

    step("missions to list")
    mission("ana", operator="Ana", goal="Test the new lidar mount",
            location="Lab bay 2", environment="indoor",
            notes="Lidar raised 4 cm on the new bracket")

    # mission_start, mission_status and mission_close on one mission.
    shoot("mission_start", "ros2 fairy mission_start",
          answers=briefing("Ana", "Map the greenhouse rows", "Greenhouse B",
                           "indoor", "Rows 1-6 only"))
    recorder = threading.Thread(target=record, args=("ana", 30))
    recorder.start()
    time.sleep(14)  # a recording with some size, and a capture done
    shoot("mission_status", "ros2 fairy mission_status")
    recorder.join()
    shoot("mission_close", "ros2 fairy mission_close",
          answers={r"Anything else worth noting\?":
                   "Cart blocked row 5 briefly"},
          until=r"Save this mission\?", then=["y"], timeout=300)

    mission("cleo", operator="Cleo", goal="Calibrate the wheel odometry",
            location="Car park, east side", environment="outdoor")
    mission("ben", operator="Ben", goal="Survey the north pier",
            location="North pier", environment="marine",
            notes="Low tide, light swell")

    step("an IMU is fitted and the base slowed down: diff shows both")
    imu_talker()
    sh(USER, f"ros2 fairy setup --add-sensor {IMU} --yes")
    sh(USER, "ros2 param set /base_controller max_speed 0.6")
    mission("ben", operator="Ben", goal="Survey the north pier with the IMU",
            location="North pier", environment="marine")

    # The table is laid out for 120 columns; at 100 it squeezes the goals.
    shoot("list", "ros2 fairy list", cols=120)
    shoot("diff", "ros2 fairy diff", rows=80)
    shoot("verify", "ros2 fairy verify")

    step("a mission to abort")
    status = interact("ana", "ros2 fairy mission_start", briefing(
        "Ana", "Check the bumper switches", "Lab bay 2", "indoor"))
    expect(status == 0, f"mission_start exited {status}")
    record("ana", 5)
    shoot("mission_abort", "ros2 fairy mission_abort",
          # The question wraps: \s+ wherever a line may break.
          answers={r"Abort the mission [^?]*\?\s+It\s+will\s+not\s+be\s+"
                   r"saved\.": "y"},
          until=r"and abort\?", then=["n"])
    sh(USER, "ros2 fairy mission_abort --yes")
    return finish()


if __name__ == "__main__":
    sys.exit(main())
