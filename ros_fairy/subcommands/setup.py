"""ros2 fairy setup — one-time per-robot configuration wizard.

Engineer-facing: the one place where ROS jargon is acceptable. Idempotent;
re-running shows current values as defaults.

Runs as a normal user — no sudo needed up front. Only the final "commit"
step (writing /etc/ros-fairy, creating the ros-fairy group, installing the
watchdog service) needs root, so that step alone re-execs itself under
``sudo`` and prompts for a password only then. See :func:`_apply_via_sudo`.
"""

import argparse
import grp
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from ros_fairy.subcommands import VerbExtension, _configure_logging, guarded_main
from ros_fairy.utils import operators, paths, ros_env

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SLUG_RE = re.compile(r"^[a-z0-9_]+$")
SENSOR_TYPES = ["gps", "lidar", "camera", "imu", "sonar", "other"]
SERVICE_NAME = "ros-fairy-watchdog.service"
UNIT_DIR = Path("/etc/systemd/system")
SYSTEM_PYTHON = "/usr/bin/python3"
GROUP_NAME = operators.GROUP_NAME
MAX_ATTEMPTS = 3

# The single fix for every "watchdog harvested nothing" failure: run setup
# from a shell where ROS 2 is sourced and the robot is visible. Printed
# wherever that precondition isn't met.
SOURCE_RECIPE = (
    "Source ROS 2 in this shell and try again, e.g.:\n"
    "    source /opt/ros/<distro>/setup.bash\n"
    "    ros2 node list   # should list your robot's nodes\n"
    "    ros2 fairy setup\n"
    "Running setup directly as root instead (`sudo ros-fairy-setup` or "
    "`sudo ros2 fairy setup`)? Point it at your install and it will source "
    "ROS 2 itself: --ros-setup /opt/ros/<distro>/setup.bash")


class SetupAborted(Exception):
    pass


def _ask(console: Console, prompt: str, validate=None, default=None,
         reason: str = "that doesn't look right", **kwargs) -> str:
    for _ in range(MAX_ATTEMPTS):
        answer = Prompt.ask(prompt, console=console,
                            **({"default": default} if default else {}),
                            **kwargs).strip()
        if answer and (validate is None or validate(answer)):
            return answer
        console.print(f"[yellow]Sorry, {reason}.[/yellow]")
    raise SetupAborted(f"Gave up on '{prompt}' after {MAX_ATTEMPTS} attempts.")


def _existing_identity() -> dict:
    path = paths.robot_identity_path()
    if not path.is_file():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return {}


_graph_cache: dict[str, list[str]] = {}


def _ros2_list(what: str) -> list[str]:
    """Visible nodes ("node") or topics ("topic").

    Read through ros-fairy's own one-participant snapshot (the one the
    watchdog uses), not `ros2 node list`/`ros2 topic list`: each of those adds
    a DDS participant and starts a ros2 daemon, and on a robot near
    CycloneDDS's 32-participants-per-host limit they can fail and make setup
    wrongly refuse. Taken once per run.
    """
    if not _graph_cache:
        from ros_fairy.harvest import ros_snapshot
        try:
            snap = ros_snapshot.take(nodes_only=True)
        except ros_snapshot.SnapshotError:
            snap = {}
        _graph_cache["node"] = list(snap.get("nodes") or [])
        _graph_cache["topic"] = [t["name"] for t in snap.get("topics") or []]
    return list(_graph_cache.get(what, []))


def _live_topics(console: Console) -> list[str]:
    return _ros2_list("topic")


def ask_robot(console: Console, current: dict) -> dict:
    robot = current.get("robot") or {}
    owner = current.get("owner") or {}
    answers = {
        "name": _ask(console, "Robot name",
                     validate=lambda s: len(s) <= 40,
                     default=robot.get("name"),
                     reason="40 characters max"),
        "platform": _ask(console, "Platform (make and model)",
                         default=robot.get("platform")),
        "serial_number": _ask(console, "Serial number / asset tag",
                              default=robot.get("serial_number")),
    }
    org = _ask(console, "Owning organization",
               default=owner.get("organization"))
    email = _ask(console, "Contact email",
                 validate=lambda s: bool(EMAIL_RE.match(s)),
                 default=owner.get("contact_email"),
                 reason="that doesn't look like an email address")
    return {"robot": answers,
            "owner": {"organization": org, "contact_email": email,
                      **({"default_license": owner["default_license"]}
                         if owner.get("default_license") else {})}}


def ask_sensors(console: Console, current: dict) -> tuple[list, list]:
    sensors: list[dict] = []
    calibrations: list[dict] = []
    # Idempotent re-run: existing sensors are kept unless explicitly dropped,
    # so declining the add-loop can't silently wipe the configuration.
    existing = list(current.get("sensors") or [])
    if existing:
        names = ", ".join(s.get("sensor_id", "?") for s in existing)
        if Confirm.ask(f"Keep the {len(existing)} sensor(s) already "
                       f"configured ({names})?", default=True,
                       console=console):
            sensors = existing
            kept_refs = {s.get("calibration") for s in sensors}
            calibrations = [c for c in (current.get("calibrations") or [])
                            if c.get("name") in kept_refs]
    live = _live_topics(console)
    if live:
        console.print(f"[dim]Live topics seen: {', '.join(live[:12])}"
                      f"{' …' if len(live) > 12 else ''}[/dim]")
    while Confirm.ask("Add a sensor?", default=not sensors, console=console):
        seen = {s["sensor_id"] for s in sensors}
        sid = _ask(console, "Sensor id (lowercase slug, e.g. gps0)",
                   validate=lambda s, seen=seen: bool(SLUG_RE.match(s))
                   and s not in seen,
                   reason="lowercase letters/digits/underscore, and unique")
        stype = Prompt.ask("Type", choices=SENSOR_TYPES, console=console)
        make = _ask(console, "Make and model")
        topic = _ask(console, "Topic", validate=lambda s: s.startswith("/"),
                     reason="topics start with /")
        if live and topic not in live:
            if not Confirm.ask(f"{topic} isn't being published right now — "
                               f"use it anyway?", default=True,
                               console=console):
                continue
        frame = Prompt.ask("TF frame id (Enter to skip)", default="",
                           console=console).strip() or None
        sensor = {"sensor_id": sid, "type": stype, "make_model": make,
                  "topic": topic}
        if frame:
            sensor["frame_id"] = frame
        cal_path = Prompt.ask("Calibration file path (Enter to skip)",
                              default="", console=console).strip()
        if cal_path:
            for _ in range(MAX_ATTEMPTS - 1):
                if Path(cal_path).is_file():
                    break
                console.print("[yellow]That file doesn't exist.[/yellow]")
                cal_path = Prompt.ask("Calibration file path (Enter to "
                                      "skip)", default="",
                                      console=console).strip()
                if not cal_path:
                    break
            if cal_path and Path(cal_path).is_file():
                cal_name = f"{sid}_cal"
                # Absolute: the watchdog (root, cwd "/") reads it later.
                calibrations.append({
                    "name": cal_name,
                    "source_path": str(Path(cal_path).expanduser().resolve())})
                sensor["calibration"] = cal_name
        sensors.append(sensor)
    return sensors, calibrations


def ask_operators(console: Console) -> list[str]:
    """Other accounts that will record missions on this robot.

    The account running setup is added anyway (it's the one behind sudo);
    accounts already in the group are listed so nobody is asked twice.
    """
    me = operators.invoking_user()
    already = [m for m in operators.members() if m != me]
    if already:
        console.print(f"[dim]Already in the '{GROUP_NAME}' group: "
                      f"{', '.join(already)}[/dim]")
    prompt = ("Other accounts that will record missions (names separated by "
              "spaces, Enter for none)")
    for _ in range(MAX_ATTEMPTS):
        names = [n for n in operators.parse_names(
            Prompt.ask(prompt, default="", console=console))
            if n != me and n not in already]
        unknown = [n for n in names if not operators.account_exists(n)]
        if not unknown:
            return names
        console.print(f"[yellow]Sorry, there's no account named "
                      f"{', '.join(unknown)} on this computer.[/yellow]")
    raise SetupAborted(f"Gave up on '{prompt}' after {MAX_ATTEMPTS} attempts.")


def review(console: Console, config: dict,
           operator_names: list[str] | None = None) -> bool:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Robot", f"{config['robot']['name']} "
                           f"({config['robot']['platform']})")
    table.add_row("Serial", config["robot"]["serial_number"])
    table.add_row("Owner", f"{config['owner']['organization']} "
                           f"<{config['owner']['contact_email']}>")
    for sensor in config.get("sensors", []):
        cal = f", cal: {sensor['calibration']}" if sensor.get(
            "calibration") else ""
        table.add_row(f"Sensor {sensor['sensor_id']}",
                      f"{sensor['type']} — {sensor['make_model']} on "
                      f"{sensor['topic']}{cal}")
    table.add_row("Operators", ", ".join(
        [f"{operators.invoking_user()} (you)", *(operator_names or [])]))
    console.print(Panel(table, title="Configuration review",
                        border_style="cyan"))
    return Confirm.ask("Write this configuration?", default=True,
                       console=console)


def write_identity(config: dict) -> None:
    config_dir = paths.config_dir()
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    path = paths.robot_identity_path()
    path.write_text(yaml.safe_dump(config, sort_keys=False,
                                   allow_unicode=True))
    path.chmod(0o644)


def create_dirs() -> None:
    """Create the ``ros-fairy`` group and the shared directories it owns."""
    try:
        gid = grp.getgrnam(GROUP_NAME).gr_gid
    except KeyError:
        subprocess.run(["groupadd", "--system", GROUP_NAME], check=False)
        try:
            gid = grp.getgrnam(GROUP_NAME).gr_gid
        except KeyError:
            gid = -1
    for directory in (paths.var_dir(), paths.spool_dir(), paths.bags_dir(),
                      paths.archive_dir()):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o2775)
        if gid >= 0:
            try:
                os.chown(directory, -1, gid)
            except PermissionError:
                pass
    # Fix files an earlier root-only run may have left group-unwritable.
    for f in (paths.index_db_path(),):
        if f.exists():
            try:
                f.chmod(0o664)
                if gid >= 0:
                    os.chown(f, -1, gid)
            except OSError:
                pass


def add_operators(console: Console, extra: list[str]) -> None:
    """Put the human behind sudo, and ``extra``, into the ros-fairy group.

    Without it every non-root account is locked out of the spool and the
    index, so mission_start fails on the first real mission. SUDO_USER
    survives `sudo su` (su without `-` keeps the environment) but not a
    login shell; then only ``extra`` is added.
    """
    me = os.environ.get("SUDO_USER", "")
    names = list(dict.fromkeys(
        ([me] if me and me != "root" else []) + list(extra)))
    if not names:
        console.print(f"[yellow]No operator accounts were added to the "
                      f"'{GROUP_NAME}' group, so only root can record "
                      f"missions. Add them with: {operators.ADD_HINT}"
                      "[/yellow]")
        return
    outcome = operators.add(names)
    added = [n for n in names if outcome[n] == operators.ADDED]
    for state, style, text in (
            (operators.ADDED, "green", "Added to the '{g}' group: {n}."),
            (operators.ALREADY, "dim", "Already in the '{g}' group: {n}."),
            (operators.NO_ACCOUNT, "yellow", "No such account, skipped: {n}."),
            (operators.FAILED, "red",
             "Couldn't add {n} (`usermod` failed). Try again later with: "
             "ros2 fairy add_operator {n}")):
        which = [n for n in names if outcome[n] == state]
        if which:
            console.print(f"[{style}]" + text.format(
                g=GROUP_NAME, n=", ".join(which)) + f"[/{style}]")
    if added:
        console.print(f"[dim]{operators.NEXT_LOGIN}[/dim]")


def write_watchdog_env(env: dict[str, str]) -> None:
    """Persist a previously-captured ROS environment for the watchdog service.

    Written as a systemd EnvironmentFile loaded by the unit's
    ``EnvironmentFile=``. Takes the environment rather than capturing it
    itself: it commonly runs as root (see :func:`_apply`), and root's own
    environment is not what should be captured — the caller must capture
    ``ros_env.capture()`` from whichever shell actually has ROS 2 sourced
    (normally the unprivileged operator's) and hand it in. Validation/abort
    of that source is the caller's job (:func:`_check_ros_visible`).

    The service runs as root, so search-path entries an ordinary user can
    write to (``~/.local/bin`` from the operator's PATH) are dropped: root
    would otherwise run whatever that user puts there.
    """
    safe, _ = ros_env.root_safe_env(env)
    ros_env.write_file(paths.watchdog_env_path(), safe)


def _ensure_ros_environment(console: Console, explicit: str | None) -> bool:
    """Make sure ROS 2 is sourced in this process, sourcing it ourselves if not.

    Called after the root check, so this runs as root already — the one
    thing it cannot do is *become* root, which is why ``sudo`` is still
    required up front. See :func:`ros_fairy.utils.ros_env.source_setup_bash`
    for why this step exists at all.
    """
    if shutil.which("ros2") is not None and "ROS_DISTRO" in os.environ:
        return True  # already sourced — e.g. the old `sudo su` workflow

    if explicit:
        setup_bash = Path(explicit)
        if not setup_bash.is_file():
            console.print(f"[red]{setup_bash} doesn't exist.[/red]")
            return False
    else:
        candidates = ros_env.find_setup_bash()
        if len(candidates) > 1:
            names = ", ".join(c.parent.name for c in candidates)
            console.print(
                f"[red]Multiple ROS 2 installs found ({names}). Say which "
                "one with: --ros-setup /opt/ros/<distro>/setup.bash[/red]")
            return False
        if not candidates:
            console.print(
                "[red]Couldn't find a ROS 2 install to source (looked under "
                "/opt/ros/*/setup.bash).\n"
                "Point me at it with --ros-setup <path to setup.bash>, or "
                f"source ROS 2 yourself before running setup.\n{SOURCE_RECIPE}"
                "[/red]")
            return False
        setup_bash = candidates[0]

    console.print(f"[dim]Sourcing {setup_bash} …[/dim]")
    try:
        changed = ros_env.source_setup_bash(setup_bash)
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        console.print(f"[red]Couldn't source {setup_bash}: {exc}[/red]")
        return False
    os.environ.update(changed)
    if shutil.which("ros2") is None:
        console.print(f"[red]Sourced {setup_bash} but still can't find ros2 "
                      "on PATH — is this really a ROS 2 install?[/red]")
        return False
    return True


def _check_ros_visible(console: Console) -> bool:
    """Refuse to install a blind watchdog (issue #29 root cause).

    The service inherits exactly this shell's ROS environment. If ROS_DISTRO is
    unset (env stripped under sudo) or the robot graph isn't visible from here
    (wrong domain/RMW, or software not started), the service would harvest an
    empty graph at every mission — fail now with the one recipe that fixes it.
    """
    if "ROS_DISTRO" not in ros_env.capture():
        console.print(
            "[red]No ROS environment is available to capture for the "
            "background service (ROS_DISTRO is unset). The watchdog would "
            "record no software versions, ROS graph, or robot description.\n"
            f"{SOURCE_RECIPE}[/red]")
        return False
    if not _ros2_list("node"):
        console.print(
            "[red]ROS is sourced but no nodes are visible (`ros2 node list` is "
            "empty), so the watchdog would harvest an empty graph. Start the "
            "robot software and check ROS_DOMAIN_ID / RMW_IMPLEMENTATION match "
            "it, then re-run.\n"
            f"{SOURCE_RECIPE}[/red]")
        return False
    return True


def interpreter_root_safe(python: str) -> bool:
    """Only root can change ``python``: the directory it sits in (and, for a
    symlink such as a venv's ``bin/python``, the one it points into)."""
    return all(ros_env.root_safe_dir(str(Path(p).parent))
               for p in (python, os.path.realpath(python)))


def service_import_location(python: str, env: dict[str, str]) -> str | None:
    """Where ``python`` would import ros_fairy from as the watchdog service,
    or ``None`` if it can't import it at all.

    Reproduces what the unit sees: only the root-safe part of ``env`` (what
    :func:`write_watchdog_env` writes), no user site-packages, and ``/`` as
    the working directory, so this checkout isn't found by accident.
    """
    safe, _ = ros_env.root_safe_env(env)
    safe["PYTHONNOUSERSITE"] = "1"
    try:
        result = subprocess.run(
            [python, "-c", "import os, ros_fairy; "
             "print(os.path.dirname(os.path.realpath(ros_fairy.__file__)))"],
            env=safe, cwd="/", capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _choose_service_python(console: Console,
                           env: dict[str, str]) -> str | None:
    """The interpreter the watchdog unit will run, or ``None`` (and why).

    The one running setup comes first, so a robot whose ROS uses another
    Python than ``/usr/bin/python3`` still works. It must be root-owned,
    though: the service runs as root, so an interpreter in a home-directory
    venv would let that user's files run as root.
    """
    candidates = [sys.executable] if interpreter_root_safe(sys.executable) \
        else []
    if SYSTEM_PYTHON not in candidates and Path(SYSTEM_PYTHON).exists():
        candidates.append(SYSTEM_PYTHON)
    mine = str(Path(__file__).resolve().parents[1])
    for python in candidates:
        found = service_import_location(python, env)
        if found is None:
            continue
        if found != mine:
            console.print(
                f"[yellow]The watchdog service will run the ros_fairy "
                f"installed in {found}, not this one ({mine}). Keep them at "
                "the same version, or `ros2 fairy doctor` will warn that the "
                "watchdog runs different code.[/yellow]")
        return python
    console.print(
        "[red]The watchdog service runs as root and can only load ros_fairy "
        "from directories that only root can change. This copy is in "
        f"{mine}, which the service can't use (a colcon workspace or a venv "
        "in a home directory, for example).\n"
        "Install it system-wide from the checkout with `./install.sh`, then "
        "run `ros2 fairy setup` again.[/red]")
    return None


def install_service(console: Console, env: dict[str, str],
                    python: str = SYSTEM_PYTHON) -> bool:
    if not interpreter_root_safe(python):
        console.print(f"[red]Won't install the watchdog service with {python}:"
                      " it runs as root, and that interpreter isn't in a "
                      "root-owned directory.[/red]")
        return False
    write_watchdog_env(env)
    unit_src = Path(__file__).resolve().parent.parent / "watchdog" / \
        SERVICE_NAME
    unit = unit_src.read_text().replace(
        f"ExecStart={SYSTEM_PYTHON} ", f"ExecStart={python} ")
    (UNIT_DIR / SERVICE_NAME).write_text(unit)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", SERVICE_NAME],
                   check=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        active = subprocess.run(["systemctl", "is-active", SERVICE_NAME],
                                capture_output=True, text=True)
        state = active.stdout.strip()
        if state == "active" and paths.watchdog_state_path().is_file():
            return True
        if state == "failed":
            return False  # crashed on start — no point waiting out the timer
        time.sleep(0.5)
    return False


def _collect(console: Console, operator_names: list[str] | None = None
             ) -> tuple[dict, list[str]] | None:
    """The unprivileged half: the interactive wizard. No root needed.

    Returns the robot configuration and the other operator accounts to add
    (``operator_names`` when given: --operator skips that question).

    Reading /etc/ros-fairy/robot_identity.yaml to offer current values as
    defaults works without root too — :func:`write_identity` leaves it
    world-readable (0644).
    """
    current = _existing_identity()
    if current:
        console.print("[dim]Existing configuration found — current values "
                      "are offered as defaults.[/dim]")
    try:
        config = ask_robot(console, current)
        sensors, calibrations = ask_sensors(console, current)
        if sensors:
            config["sensors"] = sensors
        if calibrations:
            config["calibrations"] = calibrations
        # Sections this wizard doesn't ask about (`recording:` — the topics
        # and storage mission_record uses — or anything hand-added) are kept,
        # not silently dropped by a re-run.
        for key, value in current.items():
            if key not in ("robot", "owner", "sensors", "calibrations"):
                config[key] = value
        if operator_names is None:
            operator_names = ask_operators(console)
        if not review(console, config, operator_names):
            console.print("Nothing was written.")
            return None
    except SetupAborted as exc:
        console.print(f"[red]{exc}[/red]")
        return None
    return config, operator_names


def _apply(console: Console, payload: dict) -> bool:
    """The privileged half: write /etc, create the group, install the service.

    Runs as root — either because the whole process already was (the
    old-style ``sudo ros-fairy-setup``/``sudo ros2 fairy setup``), or because
    :func:`_apply_via_sudo` re-exec'd just this step. Either way, ``payload``
    was built entirely by the unprivileged :func:`_collect` phase — this
    function does no interactive prompting and no ROS 2 access of its own,
    so it needs nothing beyond filesystem/systemd privileges.
    """
    write_identity(payload["config"])
    create_dirs()
    add_operators(console, payload.get("operators", []))
    if install_service(console, payload["env"],
                       payload.get("python", SYSTEM_PYTHON)):
        console.print(Panel("Setup complete. ros-fairy is now watching for "
                            "recordings.", border_style="green"))
        return True
    console.print("[red]The watchdog service didn't come up within 10 "
                  "seconds. Check: journalctl -u ros-fairy-watchdog[/red]")
    return False


def _apply_via_sudo(console: Console, payload: dict) -> int:
    """Hand the collected config to a root re-exec of this same module.

    This is the one place a password prompt happens: sudo asks for it right
    here, at the point privilege is actually needed, instead of up front.
    The payload travels over the child's stdin (not a temp file, and not
    argv, which would leak it to ``ps``); sudo itself prompts on the
    terminal directly, so piping stdin doesn't interfere with that.

    sudo's default ``env_reset`` strips everything from the environment,
    including two things the child needs:

    - ``ROS_FAIRY_CONFIG_DIR``/``ROS_FAIRY_VAR_DIR``, if the caller relocated
      the install (``utils/paths.py``) — without them the privileged child
      would silently write to the *default* /etc, /var locations instead of
      the ones ``_collect`` just read from. ``--preserve-env`` names exactly
      these two, nothing else.
    - the ability to ``import ros_fairy`` at all, for a colcon-workspace
      install where the package lives on ``PYTHONPATH`` (set by `source
      install/setup.bash`) rather than in the interpreter's default
      site-packages. Rather than preserving the *inherited* PYTHONPATH
      (which — per the same trust boundary ``ros_env.SESSION_ADOPT_KEYS``
      documents for session.env — would hand root's import machinery a
      loader path influenced by whatever the unprivileged shell happened to
      have), this computes the one correct directory from this *running,
      already-trusted* module's own resolved location and sets only that.
    """
    sudo = shutil.which("sudo")
    if sudo is None:
        console.print("[red]`sudo` isn't available here. Re-run this whole "
                      "command as root instead (e.g. `su -c 'ros2 fairy "
                      "setup'`).[/red]")
        return 1
    pkg_root = str(Path(__file__).resolve().parents[2])
    child_env = {**os.environ, "PYTHONPATH": pkg_root}
    result = subprocess.run(
        [sudo, "--preserve-env=ROS_FAIRY_CONFIG_DIR,ROS_FAIRY_VAR_DIR,PYTHONPATH",
         sys.executable, "-m", "ros_fairy.subcommands.setup",
         "--apply-from-stdin"],
        input=json.dumps(payload).encode(), env=child_env)
    return result.returncode


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()

    if getattr(args, "apply_from_stdin", False):
        if os.geteuid() != 0:
            console.print("[red]--apply-from-stdin is internal; just run "
                          "`ros2 fairy setup`.[/red]")
            return 1
        try:
            payload = json.loads(sys.stdin.read())
        except json.JSONDecodeError as exc:
            console.print(f"[red]Couldn't read the staged configuration: "
                          f"{exc}[/red]")
            return 1
        return 0 if _apply(console, payload) else 1

    if os.geteuid() == 0:
        # Running as root directly (old-style `sudo ros-fairy-setup` /
        # `sudo ros2 fairy setup`) — ROS may not be sourced in *this* root
        # shell even if it was in the one `sudo` was run from.
        if not _ensure_ros_environment(console, getattr(args, "ros_setup", None)):
            return 1
    if not _check_ros_visible(console):
        return 1
    if shutil.which("docker") is None:
        console.print("[yellow]Docker not found — container snapshots will "
                      "be skipped. That's fine if this robot doesn't use "
                      "containers.[/yellow]")

    env = ros_env.capture()
    python = _choose_service_python(console, env)
    if python is None:
        return 1

    wanted = getattr(args, "operator", None)
    if wanted:
        wanted = list(dict.fromkeys(wanted))
        unknown = [n for n in wanted if not operators.account_exists(n)]
        if unknown:
            console.print(f"[red]There's no account named {', '.join(unknown)}"
                          " on this computer (--operator).[/red]")
            return 1

    collected = _collect(console, wanted)
    if collected is None:
        return 0
    config, operator_names = collected
    payload = {"config": config, "env": env, "python": python,
               "operators": operator_names}

    if os.geteuid() == 0:
        return 0 if _apply(console, payload) else 1

    console.print("[dim]Saving this needs root — it writes /etc/ros-fairy "
                  "and installs the watchdog service. You may be asked for "
                  "your password.[/dim]")
    return _apply_via_sudo(console, payload)


def _add_setup_arguments(parser) -> None:
    parser.add_argument(
        "--debug", action="store_true",
        help="verbose logging to stderr (for engineers)")
    parser.add_argument(
        "--ros-setup", metavar="PATH", default=None,
        help="only used when running setup directly as root: path to the "
             "ROS 2 setup.bash to source (auto-detected from "
             "/opt/ros/*/setup.bash when there's exactly one)")
    parser.add_argument(
        "--operator", action="append", metavar="USER",
        help="another account that will record missions; repeatable. "
             "Skips the question about other operators")
    parser.add_argument(
        "--apply-from-stdin", action="store_true",
        help=argparse.SUPPRESS)  # internal: used by the root re-exec only


class SetupVerb(VerbExtension):
    """One-time robot setup: identity file, directories, watchdog service."""

    def add_arguments(self, parser, cli_name):
        _add_setup_arguments(parser)

    def main(self, *, args):
        return guarded_main(run, args)


def main(argv: list[str] | None = None) -> None:
    """``ros-fairy-setup`` — a standalone entry point, and the root re-exec target.

    Two uses:

    1. This module invoked as ``python3 -m ros_fairy.subcommands.setup
       --apply-from-stdin`` — what :func:`_apply_via_sudo` runs under
       ``sudo`` to commit an already-collected configuration. This is the
       normal path: the recommended top-level command is just
       ``ros2 fairy setup``, no sudo needed up front.
    2. The ``ros-fairy-setup`` console script, for people who prefer running
       the *whole* wizard as root in one shot (old style). Unlike
       ``ros2 fairy setup``, it's a plain console script, not a ros2cli verb,
       so it works even before ROS 2 is sourced/on PATH: ``sudo ros2 fairy
       setup`` can fail before Python ever runs, because sudo's
       ``secure_path`` usually excludes ``/opt/ros/<distro>/bin`` — the shell
       can't resolve ``ros2`` at all unless ROS was sourced before ``sudo``
       stripped the environment. ``ros-fairy-setup`` sidesteps that; see
       ``_ensure_ros_environment``.
    """
    parser = argparse.ArgumentParser(
        prog="ros-fairy-setup", description=SetupVerb.__doc__)
    _add_setup_arguments(parser)
    args = parser.parse_args(argv)
    sys.exit(guarded_main(run, args))


if __name__ == "__main__":
    main()
