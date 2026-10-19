"""ros2 fairy setup's edit flags: change one part of the robot configuration.

``--robot-name``, ``--add-sensor``, ``--storage`` and the rest change only
what they name in ``robot_identity.yaml``, without the wizard: the result is
validated, shown as a before/after diff, confirmed (or ``--yes``) and
written through sudo like the rest of setup, keeping the previous file as
``robot_identity.yaml.bak``. ``--show`` prints the configuration.

Saved missions are unaffected: each keeps the configuration it was
recorded with. The watchdog and mission_record read the file afresh every
time, so nothing needs restarting.
"""

import copy
import difflib
import os
import re
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ros_fairy.subcommands import confirm
from ros_fairy.utils import paths

# Flag (argparse dest) -> where it goes; the robot and owner fields.
_FIELDS = {
    "robot_name": ("robot", "name"),
    "platform": ("robot", "platform"),
    "serial": ("robot", "serial_number"),
    "organization": ("owner", "organization"),
    "contact_email": ("owner", "contact_email"),
}
EDIT_FLAGS = (*_FIELDS, "license", "add_sensor", "set_sensor",
              "remove_sensor", "record_topics", "storage")

# The keys of --add-sensor / --set-sensor, and their names in the file.
_SENSOR_KEYS = {"id": "sensor_id", "type": "type", "model": "make_model",
                "topic": "topic", "frame": "frame_id",
                "calibration": "calibration"}
_ALIASES = {"sensor_id": "id", "make_model": "model", "frame_id": "frame"}
_REQUIRED = ("id", "type", "model", "topic")
_SPDX_RE = re.compile(r"^[A-Za-z0-9.+-]+$")


class EditError(Exception):
    """A flag that can't be applied, in plain words."""


def wanted(args) -> bool:
    return any(getattr(args, flag, None) not in (None, [])
               for flag in EDIT_FLAGS)


def parse_spec(text: str, flag: str) -> dict[str, str]:
    """``id=gps0,type=gps,model="u-blox ZED-F9P"`` -> {key: value}.

    Commas split the pairs except inside double quotes, so a value may hold
    one: ``model="Foo, Inc. X1"``. Keys are the short names (id, type,
    model, topic, frame, calibration) or the file's own.
    """
    pairs, field, quoted = [], "", False
    for ch in text:
        if ch == '"':
            quoted = not quoted
        elif ch == "," and not quoted:
            pairs.append(field)
            field = ""
            continue
        field += ch
    if quoted:
        raise EditError(f"{flag}: a quote isn't closed in {text!r}")
    pairs.append(field)
    spec: dict[str, str] = {}
    for pair in (p.strip() for p in pairs):
        if not pair:
            continue
        key, sep, value = pair.partition("=")
        key = _ALIASES.get(key.strip(), key.strip())
        if not sep or key not in _SENSOR_KEYS:
            raise EditError(
                f"{flag}: {pair!r} isn't KEY=VALUE with KEY one of "
                f"{', '.join(_SENSOR_KEYS)}")
        if key in spec:
            raise EditError(f"{flag}: {key} is given twice")
        spec[key] = value.strip().strip('"').strip()
    return spec


def _calibration(sensor_id: str, path: str, taken) -> dict:
    """A calibration entry for ``sensor_id``, named apart from ``taken``."""
    from ros_fairy.subcommands.setup import calibration_name
    file = Path(path).expanduser()
    if not file.is_file():
        raise EditError(f"calibration file for {sensor_id} not found: {path}")
    # Absolute: the watchdog (root, cwd "/") reads it later.
    return {"name": calibration_name(sensor_id, taken),
            "source_path": str(file.resolve())}


def _section(config: dict, key: str, kind: type[list] | type[dict]):
    """``config[key]``, made an empty list or mapping when missing or empty
    (``sensors:`` with nothing after it); one of another kind is refused."""
    value = config.get(key)
    if value is None:
        value = config[key] = kind()
    if not isinstance(value, kind) or (
            isinstance(value, list)
            and not all(isinstance(v, dict) for v in value)):
        what = "a list of entries" if kind is list else "a group of fields"
        raise EditError(f"`{key}:` in the configuration should be {what} but "
                        f"isn't ({value!r}); fix it by hand in "
                        f"{paths.robot_identity_path()} (with sudo) or run "
                        "`ros2 fairy setup` again")
    return value


def _used_by_others(config: dict, sensor: dict) -> set:
    """Calibration names other sensors refer to."""
    return {s.get("calibration") for s in config.get("sensors") or []
            if s is not sensor} - {None}


def _sensor_value(key: str, value: str, sensor_types: list[str],
                  slug: re.Pattern) -> str:
    if key == "id" and not slug.match(value):
        raise EditError(f"sensor id {value!r}: lowercase letters, digits "
                        "and underscores only")
    if key == "type" and value not in sensor_types:
        raise EditError(f"sensor type {value!r}: one of "
                        f"{', '.join(sensor_types)}")
    if key == "topic":
        return value if value.startswith("/") else "/" + value
    return value


def _set_calibration(config: dict, sensor: dict, path: str | None) -> None:
    """Point ``sensor`` at a calibration file (or at none), dropping an
    entry no sensor uses any more."""
    calibrations = _section(config, "calibrations", list)
    old = sensor.pop("calibration", None)
    if path:
        # Never repoint an entry another sensor still uses.
        entry = _calibration(sensor["sensor_id"], path,
                             _used_by_others(config, sensor))
        calibrations[:] = [c for c in calibrations
                           if c.get("name") != entry["name"]]
        calibrations.append(entry)
        sensor["calibration"] = entry["name"]
    _drop_unused_calibrations(config, also=old)


def _rename_calibration(config: dict, sensor: dict, old_id: str) -> None:
    """After ``old_id`` was renamed: rename the sensor's own calibration entry
    (``<old_id>_cal``) after it too, so a sensor added later with the old id
    doesn't take it over. Kept when another sensor also uses it."""
    from ros_fairy.subcommands.setup import calibration_name
    old = sensor.get("calibration")
    if not old or not re.fullmatch(rf"{re.escape(old_id)}_cal(_\d+)?", old) \
            or old in _used_by_others(config, sensor):
        return
    calibrations = _section(config, "calibrations", list)
    taken = {c.get("name") for c in calibrations} | \
        _used_by_others(config, sensor)
    new = calibration_name(sensor["sensor_id"], taken - {old})
    for entry in calibrations:
        if entry.get("name") == old:
            entry["name"] = new
    sensor["calibration"] = new
    if not calibrations:
        config.pop("calibrations")


def _drop_unused_calibrations(config: dict, also: str | None = None) -> None:
    used = {s.get("calibration") for s in config.get("sensors") or []}
    if also is not None and also not in used:
        config["calibrations"] = [c for c in config.get("calibrations") or []
                                  if c.get("name") != also]
    if not config.get("calibrations"):
        config.pop("calibrations", None)


def _storage_names() -> list[str] | None:
    from ros_fairy.subcommands.mission_record import storage_names
    return storage_names()


def apply(current: dict, args) -> dict:
    """The configuration with the edit flags applied, in this order: robot
    and owner fields, removed sensors, changed sensors, added sensors,
    recording. Raises EditError for a flag that can't be applied."""
    from ros_fairy.subcommands.setup import EMAIL_RE, SENSOR_TYPES, SLUG_RE
    config = copy.deepcopy(current)

    for flag, (section, key) in _FIELDS.items():
        value = getattr(args, flag, None)
        if value is None:
            continue
        value = value.strip()
        if not value:
            raise EditError(f"--{flag.replace('_', '-')} can't be empty")
        if flag == "robot_name" and len(value) > 40:
            raise EditError("--robot-name: 40 characters at most")
        if flag == "contact_email" and not EMAIL_RE.match(value):
            raise EditError(f"--contact-email: {value!r} doesn't look like "
                            "an email address")
        _section(config, section, dict)[key] = value

    license_ = getattr(args, "license", None)
    if license_ is not None:
        owner = _section(config, "owner", dict)
        license_ = license_.strip()
        if not license_:
            owner.pop("default_license", None)
        elif "://" in license_:
            owner["default_license"] = license_
        elif _SPDX_RE.match(license_):
            # An SPDX identifier, as in the RO-Crate: its canonical URL.
            owner["default_license"] = f"https://spdx.org/licenses/{license_}"
        else:
            raise EditError(f"--license: {license_!r} is neither a URL nor "
                            "an SPDX identifier such as CC-BY-4.0")

    sensors = _section(config, "sensors", list)

    def find(sensor_id: str, flag: str) -> dict:
        for sensor in sensors:
            if sensor.get("sensor_id") == sensor_id:
                return sensor
        known = ", ".join(s.get("sensor_id", "?") for s in sensors) or "none"
        raise EditError(f"{flag}: there's no sensor {sensor_id!r} "
                        f"(configured: {known})")

    for sensor_id in getattr(args, "remove_sensor", None) or []:
        sensor = find(sensor_id, "--remove-sensor")
        sensors.remove(sensor)
        _drop_unused_calibrations(config, also=sensor.get("calibration"))

    for sensor_id, text in getattr(args, "set_sensor", None) or []:
        sensor = find(sensor_id, "--set-sensor")
        spec = parse_spec(text, "--set-sensor")
        if not spec:
            raise EditError(f"--set-sensor {sensor_id}: nothing to change")
        for key, value in spec.items():
            if key == "calibration":
                continue
            field = _SENSOR_KEYS[key]
            if not value:
                if key in _REQUIRED:
                    raise EditError(f"--set-sensor {sensor_id}: {key} can't "
                                    "be empty")
                sensor.pop(field, None)
                continue
            value = _sensor_value(key, value, SENSOR_TYPES, SLUG_RE)
            if key == "id" and value != sensor_id and any(
                    s.get("sensor_id") == value for s in sensors):
                raise EditError(f"--set-sensor {sensor_id}: id {value!r} is "
                                "already taken")
            sensor[field] = value
        if sensor.get("sensor_id") != sensor_id:
            _rename_calibration(config, sensor, sensor_id)
        if "calibration" in spec:
            _set_calibration(config, sensor, spec["calibration"] or None)

    for text in getattr(args, "add_sensor", None) or []:
        spec = parse_spec(text, "--add-sensor")
        missing = [k for k in _REQUIRED if not spec.get(k)]
        if missing:
            raise EditError(f"--add-sensor {text!r}: {', '.join(missing)} "
                            "missing (id, type, model and topic are needed)")
        if any(s.get("sensor_id") == spec["id"] for s in sensors):
            raise EditError(f"--add-sensor: there's already a sensor "
                            f"{spec['id']!r} (change it with --set-sensor)")
        sensor = {_SENSOR_KEYS[k]: _sensor_value(k, spec[k], SENSOR_TYPES,
                                                 SLUG_RE)
                  for k in ("id", "type", "model", "topic")}
        if spec.get("frame"):
            sensor["frame_id"] = spec["frame"]
        sensors.append(sensor)
        if spec.get("calibration"):
            _set_calibration(config, sensor, spec["calibration"])
    if not sensors:
        config.pop("sensors")

    topics = getattr(args, "record_topics", None)
    storage = getattr(args, "storage", None)
    if topics is not None or storage is not None:
        recording = _section(config, "recording", dict)
        if topics is not None:
            names = [t.strip() for t in topics.split(",") if t.strip()]
            if names == ["all"]:
                recording.pop("topics", None)
            elif not names or "all" in names:
                raise EditError("--record-topics: topic names separated by "
                                "commas, or `all`")
            else:
                recording["topics"] = [t if t.startswith("/") else "/" + t
                                       for t in names]
        if storage is not None:
            storage = storage.strip()
            if storage in ("", "default"):
                recording.pop("storage", None)
            elif not SLUG_RE.match(storage):
                raise EditError(f"--storage: {storage!r} isn't a storage "
                                "name such as mcap or sqlite3")
            elif (known := _storage_names()) and storage not in known:
                raise EditError(f"--storage: rosbag2 here has no storage "
                                f"{storage!r} (it has {', '.join(known)})")
            else:
                recording["storage"] = storage
        if not recording:
            config.pop("recording")
    return config


def dump(config: dict) -> str:
    return yaml.safe_dump(config, sort_keys=False, allow_unicode=True)


def diff_lines(before: dict, after: dict) -> list[str]:
    """The changed lines of the file, with a little context."""
    return [line.rstrip("\n") for line in difflib.unified_diff(
        dump(before).splitlines(True), dump(after).splitlines(True),
        "before", "after", n=2)][2:]


def print_diff(console: Console, lines: list[str]) -> None:
    for line in lines:
        style = {"+": "green", "-": "red", "@": "dim"}.get(line[:1], "")
        text = line.replace("[", r"\[")
        console.print(f"[{style}]{text}[/{style}]" if style else text,
                      highlight=False)


def show(console: Console) -> int:
    """setup --show: the configuration as the watchdog reads it."""
    from ros_fairy.harvest import robot_identity
    path = paths.robot_identity_path()
    if not path.is_file():
        console.print(f"This robot isn't set up yet ({path} doesn't exist): "
                      "run `ros2 fairy setup`.")
        return 1
    try:
        data = robot_identity.harvest()
    except robot_identity.RobotIdentityError as exc:
        console.print(f"[red]The robot configuration has a mistake: {exc}."
                      "[/red]")
        console.print(f"[dim]Fix it with the setup flags (e.g. --set-sensor),"
                      f" or by hand in {path} (with sudo).[/dim]")
        return 1
    robot = data["robot"]
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Robot", f"{robot['name']} ({robot['platform']}), serial "
                           f"{robot['serial_number']}")
    table.add_row("Owner", f"{robot['owner_organization']} "
                           f"<{robot['owner_contact']}>")
    table.add_row("License", data["default_license"] or "[dim]none[/dim]")
    cal_paths = {c["name"]: c["source_path"] for c in data["calibrations"]}
    for s in data["sensors"]:
        extra = [f"frame {s['frame_id']}"] if s["frame_id"] else []
        if s["calibration_ref"]:
            extra.append(f"calibration {cal_paths[s['calibration_ref']]}")
        table.add_row(f"Sensor {s['sensor_id']}",
                      f"{s['type']}: {s['make_model']} on {s['topic']}"
                      + (f" [dim]({', '.join(extra)})[/dim]" if extra else ""))
    if not data["sensors"]:
        table.add_row("Sensors", "[dim]none[/dim]")
    rec = data["recording"]
    table.add_row("Records", ", ".join(rec["topics"]) if rec["topics"]
                  else "every topic")
    table.add_row("Storage", rec["storage"] or "rosbag2's default")
    console.print(Panel(table, title=str(path), border_style="cyan"))
    console.print("[dim]Change it with flags, e.g. `ros2 fairy setup "
                  "--set-sensor gps0 topic=/gps/fix`; `ros2 fairy setup -h` "
                  "lists them.[/dim]")
    return 0


def _unpublished(config: dict, before: dict) -> list[str]:
    """Topics of added or changed sensors nobody publishes right now, when
    the robot's graph can be seen at all (a robot that is off says
    nothing)."""
    old = {(s.get("sensor_id"), s.get("topic"))
           for s in before.get("sensors") or []}
    new = [s["topic"] for s in config.get("sensors") or []
           if (s.get("sensor_id"), s.get("topic")) not in old]
    if not new:
        return []
    from ros_fairy.subcommands.setup import _live_topics
    live = _live_topics(Console(quiet=True))
    return [t for t in new if t not in live] if live else []


def run(args, console: Console) -> int:
    """The edit flags, start to end. Writing goes through setup's
    privileged half (``{"edit": config}``)."""
    from ros_fairy.harvest import robot_identity
    from ros_fairy.subcommands import setup

    path = paths.robot_identity_path()
    if not path.is_file():
        console.print("[red]This robot isn't set up yet: run `ros2 fairy "
                      "setup` first; the flags change an existing "
                      "configuration.[/red]")
        return 1
    try:
        current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        console.print(f"[red]{path} can't be read ({exc}). Fix it by hand "
                      "or run `ros2 fairy setup` again.[/red]")
        return 1
    if not isinstance(current, dict):
        console.print(f"[red]{path} isn't a configuration (a YAML mapping). "
                      "Run `ros2 fairy setup` again.[/red]")
        return 1
    try:
        config = apply(current, args)
        robot_identity.validate(config)
    except EditError as exc:
        console.print(f"[red]{exc}. Nothing was changed.[/red]")
        return 1
    except robot_identity.RobotIdentityError as exc:
        console.print(f"[red]The configuration would have a mistake: {exc}. "
                      "Nothing was changed.[/red]")
        return 1

    lines = diff_lines(current, config)
    if not lines:
        console.print("Nothing to change: the configuration already says "
                      "that.")
        return 0
    console.print(f"Changes to {path}:")
    print_diff(console, lines)
    for topic in _unpublished(config, current):
        console.print(f"[yellow]{topic} isn't being published right now. "
                      "That's fine if the sensor is off; check the name "
                      "otherwise.[/yellow]")
    if not confirm("Write this?", default=True, console=console,
                   assume_yes=getattr(args, "yes", False)):
        console.print("Nothing was changed.")
        return 0
    payload = {"edit": config}
    if os.geteuid() == 0:
        return 0 if setup._apply(console, payload) else 1
    console.print("[dim]Writing it needs root: you may be asked for your "
                  "password.[/dim]")
    return setup._apply_via_sudo(console, payload)
