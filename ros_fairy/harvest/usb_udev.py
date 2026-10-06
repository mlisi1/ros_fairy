"""udev rules and USB port management: what the robot's own configuration
does to its devices.

Three things, all read-only:

- **Non-default udev rules.** A rule file is *default* when a Debian package
  ships it and it is unmodified (its md5 still matches dpkg's). Everything
  else — ``/etc/udev/rules.d``, ``/run/udev/rules.d``, files no package owns,
  package files edited locally — is the robot's own configuration and is
  archived in full. Default files are only listed (name, package).
- **USB management.** For every USB device and hub port, the sysfs settings
  that decide how it behaves: power management (autosuspend, runtime
  status), USB3 link power management, port connect type, quirks, speed,
  drivers, and the FTDI ``latency_timer`` of USB serial adapters; plus the
  global ``usbcore`` parameters (autosuspend, usbfs memory).
- **Which rules applied.** ``udevadm test`` replays the rules for each USB
  device and its children and names every matching ``file:line``. Each
  attribute a non-default rule writes is then read back, so a rule that is
  installed but not in effect (the device was plugged in before it existed,
  or the write failed) is visible.

``udevadm test`` executes ``PROGRAM=`` helpers and, with privileges, really
writes ``ATTR`` values (systemd 255). It is therefore run as ``nobody``
whenever this process is root: replaying the rules must never change the
hardware.
"""

import hashlib
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("ros_fairy.harvest.usb_udev")

# Filesystem root the rules and dpkg's database are read under (tests point
# it at a fabricated tree); recorded paths are always the real "/..." ones.
ROOT = Path("/")
RULE_DIRS = ("/etc/udev/rules.d", "/run/udev/rules.d", "/usr/lib/udev/rules.d",
             "/lib/udev/rules.d", "/usr/local/lib/udev/rules.d")
# Directories whose files override same-named files in later ones (udev's
# own precedence: /etc, then /run, then the library directories).
LOCAL_DIRS = ("/etc/udev/rules.d", "/run/udev/rules.d")
USB_DEVICES = Path("/sys/bus/usb/devices")
USBCORE_PARAMS = Path("/sys/module/usbcore/parameters")
DPKG_INFO = "/var/lib/dpkg/info"

TRACE_BUDGET_S = 15.0
TRACE_TIMEOUT_S = 5.0
MAX_TRACED = 300
MAX_RULE_BYTES = 256 * 1024

_DEVICE_ATTRS = ("busnum", "devnum", "devpath", "idVendor", "idProduct",
                 "manufacturer", "product", "serial", "speed", "version",
                 "bMaxPower", "removable", "authorized", "avoid_reset_quirk",
                 "quirks", "ltm_capable", "bDeviceClass", "maxchild")
_POWER_ATTRS = ("control", "autosuspend_delay_ms", "runtime_status",
                "persist", "wakeup")
_PORT_ATTRS = ("connect_type", "disable", "usb3_lpm_permit", "state",
               "over_current_count", "quirks", "location")
_USBCORE = ("autosuspend", "usbfs_memory_mb", "quirks", "authorized_default",
            "old_scheme_first", "use_both_schemes", "initial_descriptor_timeout")
# Line of `udevadm test` output naming the rule that acted:
#   "ttyUSB0: /etc/udev/rules.d/99-jo-usb.rules:7 ATTR '...' writing '1'"
_TRACE_LINE = re.compile(r"^(?P<dev>[^:\s]+): (?P<file>/\S+\.rules):(?P<line>\d+)"
                         r" (?P<action>.*)$")
_ATTR_WRITE = re.compile(r"^ATTR '(?P<path>[^']+)' writing '(?P<value>[^']*)'")
_USB_NAME = re.compile(r"^(usb\d+|\d+-[\d.]+)$")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return None


# -- rules inventory ----------------------------------------------------------

def _owner_key(path: str) -> str:
    """/usr/lib/x and /lib/x are one file on a merged-/usr system, and
    packages still name the old path: compare without the /usr."""
    return path[len("/usr"):] if path.startswith("/usr/lib/") else path


def _under_root(path: str) -> Path:
    return ROOT / path.lstrip("/")


def _dpkg_owners() -> dict[str, tuple[str, str]]:
    """owner key -> (package, md5) for every rule file dpkg installed, read
    from dpkg's own md5sums lists (no subprocess per file)."""
    owners: dict[str, tuple[str, str]] = {}
    try:
        lists = list(_under_root(DPKG_INFO).glob("*.md5sums"))
    except OSError:
        return owners
    for md5s in lists:
        package = md5s.name[:-len(".md5sums")].split(":")[0]
        try:
            text = md5s.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            md5, _, rel = line.partition("  ")
            if "udev/rules.d/" not in rel or not rel.endswith(".rules"):
                continue
            owners[_owner_key("/" + rel.lstrip("/"))] = (package, md5)
    return owners


def rules_inventory() -> tuple[list[dict], list[dict], dict[str, str]]:
    """(non-default rules, default rules, path -> content of non-default).

    A name in /etc or /run overrides the library file of the same name;
    an override that is empty or links to /dev/null masks it.
    """
    owners = _dpkg_owners()
    seen_real: set[str] = set()
    by_name: dict[str, list[str]] = {}
    custom: list[dict] = []
    default: list[dict] = []
    contents: dict[str, str] = {}
    for directory in RULE_DIRS:
        d = _under_root(directory)
        try:
            files = sorted(f for f in d.iterdir() if f.name.endswith(".rules"))
        except OSError:
            continue
        for f in files:
            try:
                real = str(f.resolve())
            except OSError:
                real = str(f)
            if real in seen_real and directory not in LOCAL_DIRS:
                continue  # /lib is /usr/lib on a merged system
            seen_real.add(real)
            path = f"{directory}/{f.name}"
            by_name.setdefault(f.name, []).append(path)
            masked = real == "/dev/null"
            try:
                data = b"" if masked else f.read_bytes()
            except OSError as exc:
                log.debug("can't read %s: %s", f, exc)
                continue
            owner = owners.get(_owner_key(path))
            md5 = hashlib.md5(data).hexdigest()
            if owner and owner[1] == md5 and directory not in LOCAL_DIRS:
                default.append({"path": path, "package": owner[0]})
                continue
            if directory in LOCAL_DIRS:
                why = "local"
            elif owner:
                why = "modified"
            else:
                why = "unpackaged"
            entry = {"path": path, "reason": why,
                     "package": owner[0] if owner else None,
                     "sha256": hashlib.sha256(data).hexdigest(),
                     "size_bytes": len(data),
                     "masks": None, "overrides": None}
            if masked or (directory in LOCAL_DIRS and not data.strip()):
                entry["masks"] = f.name
            custom.append(entry)
            if not masked and len(data) <= MAX_RULE_BYTES:
                contents[path] = data.decode("utf-8", "replace")
    # Which library file each local one overrides (or masks).
    for entry in custom:
        name = Path(entry["path"]).name
        others = [p for p in by_name.get(name, []) if p != entry["path"]]
        lib = [p for p in others if not p.startswith(LOCAL_DIRS)]
        if entry["path"].startswith(LOCAL_DIRS) and lib:
            entry["overrides"] = lib[0]
        elif entry["masks"] and not lib:
            entry["masks"] = None
    if not owners:
        log.info("dpkg's file lists are unreadable: every udev rule is "
                 "treated as the robot's own")
    return custom, default, contents


# -- USB management -------------------------------------------------------------

def _attrs(d: Path, names: tuple[str, ...]) -> dict[str, str]:
    out = {}
    for name in names:
        value = _read(d / name)
        if value is not None:
            out[name] = value
    return out


def _driver(d: Path) -> str | None:
    try:
        return Path(os.readlink(d / "driver")).name
    except OSError:
        return None


def _usb_serial_children(d: Path) -> list[dict]:
    """USB serial adapters below a USB device, with their FTDI latency."""
    out = []
    for latency in sorted(d.glob("*:*/ttyUSB*/latency_timer")) + \
            sorted(d.glob("*:*/ttyACM*/latency_timer")):
        out.append({"tty": latency.parent.name,
                    "latency_timer_ms": _read(latency)})
    for tty in sorted(d.glob("*:*/tty/ttyACM*")):
        if not any(o["tty"] == tty.name for o in out):
            out.append({"tty": tty.name, "latency_timer_ms": None})
    return out


def usb_state() -> dict[str, Any] | None:
    """Every USB device and hub port, and the usbcore parameters. None when
    sysfs has no USB bus at all."""
    try:
        names = sorted(p.name for p in USB_DEVICES.iterdir()
                       if _USB_NAME.match(p.name))
    except OSError:
        return None
    devices, ports = [], []
    for name in names:
        d = USB_DEVICES / name
        attrs = _attrs(d, _DEVICE_ATTRS)
        power = _attrs(d / "power", _POWER_ATTRS)
        interfaces = []
        for intf in sorted(d.glob(f"{name}:*")):
            interfaces.append({
                "name": intf.name,
                "interface_class": _read(intf / "bInterfaceClass"),
                "driver": _driver(intf)})
        devices.append({
            "sysfs_name": name,
            "port_path": attrs.get("devpath") if not name.startswith("usb")
            else None,
            "bus": attrs.get("busnum"), "device": attrs.get("devnum"),
            "vendor_id": attrs.get("idVendor"),
            "product_id": attrs.get("idProduct"),
            "manufacturer": attrs.get("manufacturer"),
            "product": attrs.get("product"),
            "serial": attrs.get("serial"),
            "speed_mbps": attrs.get("speed"),
            "usb_version": attrs.get("version"),
            "max_power": attrs.get("bMaxPower"),
            "removable": attrs.get("removable"),
            "authorized": attrs.get("authorized"),
            "quirks": attrs.get("quirks"),
            "avoid_reset_quirk": attrs.get("avoid_reset_quirk"),
            "driver": _driver(d),
            "power": power,
            "interfaces": interfaces,
            "serial_ports": _usb_serial_children(d),
        })
        # Hub ports: <hub>/<hub interface>/<port>, e.g.
        # usb1/1-0:1.0/usb1-port3, 1-4/1-4:1.0/1-4-port2
        for port in sorted(d.glob("*:1.0/*-port*")):
            info = _attrs(port, _PORT_ATTRS)
            child = None
            try:
                child = Path(os.readlink(port / "device")).name
            except OSError:
                pass
            peer = None
            try:
                peer = Path(os.readlink(port / "peer")).name
            except OSError:
                pass
            ports.append({"port": port.name, "hub": name,
                          "connected": child, "peer": peer,
                          "power_control": _read(port / "power" / "control"),
                          **info})
    drivers = sorted({i["driver"] for dev in devices
                      for i in dev["interfaces"] if i["driver"]}
                     | {"usbcore"})
    return {"usbcore": _attrs(USBCORE_PARAMS, _USBCORE),
            "driver_parameters": _driver_parameters(drivers),
            "devices": devices, "ports": ports}


def _driver_parameters(drivers: list[str]) -> dict[str, dict[str, str]]:
    """Module parameters of every driver bound to a USB interface: they can
    override what udev set (btusb's enable_autosuspend turns autosuspend
    back on after the device's udev rules ran)."""
    out: dict[str, dict[str, str]] = {}
    for driver in drivers:
        module = driver
        try:
            module = Path(os.readlink(Path("/sys/bus/usb/drivers") / driver
                                      / "module")).name
        except OSError:
            pass
        params = Path("/sys/module") / module / "parameters"
        try:
            names = sorted(p.name for p in params.iterdir())
        except OSError:
            continue
        values = _attrs(params, tuple(names))
        if values:
            out[module] = values
    return out


# -- which rules applied ------------------------------------------------------------

def _trace_cmd(syspath: str) -> list[str]:
    cmd = ["udevadm", "test", "--action=add", syspath]
    if os.geteuid() == 0:
        # Unprivileged: the replay may run PROGRAM helpers and would write
        # ATTR values as root.
        cmd = ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups",
               "--no-new-privs", *cmd]
    return cmd


def _traced_nodes(usb_names: list[str]) -> list[Path]:
    """Every device node udev handles under each USB device (the device,
    its interfaces, and their tty/video/input/... children)."""
    nodes: list[Path] = []
    seen: set[str] = set()
    for name in usb_names:
        try:
            root = (USB_DEVICES / name).resolve()
        except OSError:
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # Don't descend into other USB devices (they are traced
            # themselves) or into power/ and similar attribute folders.
            dirnames[:] = [n for n in dirnames
                           if not _USB_NAME.match(n) and n not in
                           ("power", "subsystem", "driver", "firmware_node",
                            "port", "peer", "device")]
            if "uevent" in filenames and dirpath not in seen:
                seen.add(dirpath)
                nodes.append(Path(dirpath))
    return nodes


def rules_applied(usb_names: list[str], custom_paths: set[str]
                  ) -> tuple[dict[str, list[dict]], list[str], bool]:
    """(USB device -> non-default rules that acted on it or its children,
    every trace line, whether every node was traced in time).

    Each ATTR a non-default rule writes is read back: ``in_effect`` False
    means the device does not have the value the rule sets.
    """
    if not _have("udevadm") or (os.geteuid() == 0 and not _have("setpriv")):
        return {}, [], False
    nodes = _traced_nodes(usb_names)
    complete = len(nodes) <= MAX_TRACED
    nodes = nodes[:MAX_TRACED]
    end = time.monotonic() + TRACE_BUDGET_S
    applied: dict[str, list[dict]] = {}
    trace: list[str] = []
    usb_root = {}
    for name in usb_names:
        try:
            usb_root[str((USB_DEVICES / name).resolve())] = name
        except OSError:
            pass
    for node in nodes:
        if time.monotonic() >= end:
            complete = False
            break
        owner = None
        for root, name in usb_root.items():
            if str(node) == root or str(node).startswith(root + "/"):
                if owner is None or len(root) > len(owner[0]):
                    owner = (root, name)
        try:
            result = subprocess.run(
                _trace_cmd(str(node)), capture_output=True, text=True,
                timeout=min(TRACE_TIMEOUT_S, max(0.5, end - time.monotonic())),
                encoding="utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired):
            complete = False
            continue
        for line in (result.stdout + result.stderr).splitlines():
            m = _TRACE_LINE.match(line.strip())
            if not m or m["action"].startswith("Failed to write"):
                continue  # the unprivileged replay's own refused write
            trace.append(line.strip())
            if m["file"] not in custom_paths or owner is None:
                continue
            entry = {"node": m["dev"], "rule": f"{m['file']}:{m['line']}",
                     "action": m["action"][:300]}
            w = _ATTR_WRITE.match(m["action"])
            if w:
                current = _read(Path(w["path"]))
                entry["attribute"] = w["path"]
                entry["expected"] = w["value"]
                entry["actual"] = current
                entry["in_effect"] = current == w["value"]
            applied.setdefault(owner[1], []).append(entry)
    return applied, trace, complete


def _have(cmd: str) -> bool:
    from shutil import which
    return which(cmd) is not None


def harvest() -> dict[str, Any]:
    """Never raises. Keys:

    - ``udev_rules``: {"custom": [...], "default": [...]} or None
    - ``usb``: usb_state() with ``udev_rules_applied`` per device, or None
    - ``rule_files``: path -> content of each non-default rule (archived)
    - ``trace``: every ``udevadm test`` match line (archived)
    - ``partial``: something could not be read in full
    """
    out: dict[str, Any] = {"udev_rules": None, "usb": None, "rule_files": {},
                           "trace": [], "partial": False}
    custom_paths: set[str] = set()
    try:
        custom, default, contents = rules_inventory()
        out["udev_rules"] = {"custom": custom, "default": default}
        out["rule_files"] = contents
        custom_paths = {r["path"] for r in custom}
    except Exception as exc:
        log.warning("reading the udev rules failed: %s", exc)
        out["partial"] = True
    try:
        usb = usb_state()
    except Exception as exc:
        log.warning("reading the USB devices failed: %s", exc)
        usb, out["partial"] = None, True
    if usb is not None:
        try:
            applied, trace, complete = rules_applied(
                [d["sysfs_name"] for d in usb["devices"]], custom_paths)
            out["trace"] = trace
            if not complete:
                out["partial"] = True
        except Exception as exc:
            log.warning("replaying the udev rules failed: %s", exc)
            applied, out["partial"] = {}, True
        for dev in usb["devices"]:
            dev["udev_rules_applied"] = applied.get(dev["sysfs_name"], [])
        missing = [e for entries in applied.values() for e in entries
                   if e.get("in_effect") is False]
        for e in missing:
            log.warning("udev rule %s should set %s to %r, but it is %r",
                        e["rule"], e["attribute"], e["expected"], e["actual"])
        out["usb"] = usb
    return out
