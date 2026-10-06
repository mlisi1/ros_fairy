"""udev rules and USB port management (harvest/usb_udev.py), on a faked
sysfs and dpkg database."""

import hashlib
import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from ros_fairy.harvest import usb_udev
from ros_fairy.manifest import builder
from ros_fairy.manifest.schema import UdevRules, UsbState

JO_RULE = 'ACTION=="add", SUBSYSTEM=="usb", ATTR{power/control}="on"\n'
DEFAULT_RULE = 'SUBSYSTEM=="tty", GROUP="dialout"\n'


def _w(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def fake_rules(tmp_path, monkeypatch):
    etc, lib = tmp_path / "etc/udev/rules.d", tmp_path / "usr/lib/udev/rules.d"
    _w(etc / "99-jo-usb.rules", JO_RULE)
    _w(etc / "60-serial.rules", "# local replacement\n")    # overrides
    os.symlink("/dev/null", etc / "70-noisy.rules")          # masks
    _w(lib / "50-default.rules", DEFAULT_RULE)               # default
    _w(lib / "60-serial.rules", DEFAULT_RULE)
    _w(lib / "70-noisy.rules", DEFAULT_RULE)
    _w(lib / "80-edited.rules", "edited by hand\n")          # modified
    _w(lib / "90-dropped-in.rules", "nobody owns me\n")      # unpackaged
    dpkg = tmp_path / "dpkg"
    md5 = hashlib.md5(DEFAULT_RULE.encode()).hexdigest()
    _w(dpkg / "udev.md5sums",
       f"{md5}  lib/udev/rules.d/50-default.rules\n"      # old /lib spelling
       f"{md5}  usr/lib/udev/rules.d/60-serial.rules\n"
       f"{md5}  usr/lib/udev/rules.d/70-noisy.rules\n"
       f"{md5}  usr/lib/udev/rules.d/80-edited.rules\n")
    _w(dpkg / "udev.list", "")  # not an md5sums list: ignored
    (tmp_path / "var/lib/dpkg").mkdir(parents=True)
    dpkg.rename(tmp_path / "var/lib/dpkg/info")
    monkeypatch.setattr(usb_udev, "ROOT", tmp_path)
    monkeypatch.setattr(usb_udev, "RULE_DIRS", ("/etc/udev/rules.d",
                                                "/usr/lib/udev/rules.d"))
    return tmp_path


def test_only_non_default_rules_are_archived(fake_rules):
    custom, default, contents = usb_udev.rules_inventory()
    by_name = {Path(r["path"]).name: r for r in custom}
    assert set(by_name) == {"99-jo-usb.rules", "60-serial.rules",
                            "70-noisy.rules", "80-edited.rules",
                            "90-dropped-in.rules"}
    assert by_name["99-jo-usb.rules"]["reason"] == "local"
    assert by_name["80-edited.rules"]["reason"] == "modified"
    assert by_name["80-edited.rules"]["package"] == "udev"
    assert by_name["90-dropped-in.rules"]["reason"] == "unpackaged"
    assert by_name["60-serial.rules"]["overrides"] == \
        "/usr/lib/udev/rules.d/60-serial.rules"
    assert by_name["70-noisy.rules"]["masks"] == "70-noisy.rules"
    # contents of the robot's own rules are kept; a mask has none
    assert contents[by_name["99-jo-usb.rules"]["path"]] == JO_RULE
    assert by_name["70-noisy.rules"]["path"] not in contents
    # unmodified package files are only listed
    assert sorted(Path(d["path"]).name for d in default) == \
        ["50-default.rules", "60-serial.rules", "70-noisy.rules"]
    UdevRules.model_validate({"custom": custom, "default": default})


# -- a faked sysfs USB tree ------------------------------------------------------

@pytest.fixture
def fake_sysfs(tmp_path, monkeypatch):
    devices = tmp_path / "sys/devices/pci0000:00/0000:00:14.0"
    hub = devices / "usb1"
    for k, v in {"busnum": "1", "devnum": "1", "devpath": "0",
                 "idVendor": "1d6b", "idProduct": "0002", "speed": "480",
                 "product": "xHCI Host Controller"}.items():
        _w(hub / k, v + "\n")
    imu = hub / "1-3"
    for k, v in {"busnum": "1", "devnum": "2", "devpath": "3",
                 "idVendor": "2639", "idProduct": "0301", "serial": "DB9MF8VK",
                 "product": "MTi USB Converter", "speed": "12"}.items():
        _w(imu / k, v + "\n")
    _w(imu / "power/control", "on\n")
    _w(imu / "power/runtime_status", "active\n")
    intf = imu / "1-3:1.0"
    _w(intf / "bInterfaceClass", "ff\n")
    drivers = tmp_path / "sys/bus/usb/drivers"
    (drivers / "ftdi_sio").mkdir(parents=True)
    os.symlink(drivers / "ftdi_sio", intf / "driver")
    _w(intf / "ttyUSB0/latency_timer", "1\n")
    _w(intf / "ttyUSB0/uevent", "")
    bt = hub / "1-14"
    for k, v in {"devpath": "14", "idVendor": "8087", "idProduct": "0029"}.items():
        _w(bt / k, v + "\n")
    _w(bt / "power/control", "auto\n")  # the driver re-enabled autosuspend
    port = hub / "1-0:1.0/usb1-port3"
    _w(port / "connect_type", "hotplug\n")
    _w(port / "power/control", "auto\n")
    os.symlink(imu, port / "device")
    for d in (hub, imu, intf, bt):
        _w(d / "uevent", "")
    bus = tmp_path / "sys/bus/usb/devices"
    bus.mkdir(parents=True)
    for d in (hub, imu, intf, bt):
        os.symlink(d, bus / d.name)
    core = _w(tmp_path / "sys/module/usbcore/parameters/autosuspend", "2\n")
    _w(core.parent / "usbfs_memory_mb", "1000\n")
    monkeypatch.setattr(usb_udev, "USB_DEVICES", bus)
    monkeypatch.setattr(usb_udev, "USBCORE_PARAMS", core.parent)
    return {"hub": hub, "imu": imu, "bt": bt}


def test_usb_state_reads_devices_ports_and_usbcore(fake_sysfs):
    state = usb_udev.usb_state()
    devs = {d["sysfs_name"]: d for d in state["devices"]}
    assert set(devs) == {"usb1", "1-3", "1-14"}
    imu = devs["1-3"]
    assert imu["serial"] == "DB9MF8VK" and imu["port_path"] == "3"
    assert imu["power"]["control"] == "on"
    assert imu["interfaces"] == [{"name": "1-3:1.0", "interface_class": "ff",
                                  "driver": "ftdi_sio"}]
    assert imu["serial_ports"] == [{"tty": "ttyUSB0", "latency_timer_ms": "1"}]
    [port] = state["ports"]
    assert port["port"] == "usb1-port3" and port["connected"] == "1-3"
    assert port["connect_type"] == "hotplug"
    assert state["usbcore"] == {"autosuspend": "2", "usbfs_memory_mb": "1000"}
    UsbState.model_validate(state)


def _trace_for(fake_sysfs):
    imu, bt = fake_sysfs["imu"], fake_sysfs["bt"]
    rule = "/etc/udev/rules.d/99-jo-usb.rules"
    return {
        str(imu): f"1-3: {rule}:14 ATTR '{imu}/power/control' writing 'on'\n"
                  "1-3: /usr/lib/udev/rules.d/50-default.rules:4 GROUP 20\n",
        str(bt): f"1-14: {rule}:14 ATTR '{bt}/power/control' writing 'on'\n"
                 f"1-14: {rule}:14 Failed to write ATTR{{x}}, ignoring\n",
        str(imu / "1-3:1.0/ttyUSB0"):
            f"ttyUSB0: {rule}:7 ATTR '{imu}/1-3:1.0/ttyUSB0/latency_timer' "
            "writing '1'\n",
    }


def test_applied_rules_are_read_back(fake_sysfs, monkeypatch):
    traces = _trace_for(fake_sysfs)
    rule = "/etc/udev/rules.d/99-jo-usb.rules"

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            cmd, 0, traces.get(cmd[-1], ""), "")
    monkeypatch.setattr(usb_udev, "_have", lambda c: True)
    monkeypatch.setattr(usb_udev.subprocess, "run", fake_run)
    applied, trace, complete = usb_udev.rules_applied(
        ["usb1", "1-3", "1-14"], {rule})
    assert complete
    imu = {(e["node"], e["rule"]): e for e in applied["1-3"]}
    assert imu[("1-3", f"{rule}:14")]["in_effect"] is True
    assert imu[("ttyUSB0", f"{rule}:7")]["actual"] == "1"
    [bt] = applied["1-14"]  # the replay's own failed write is not a rule
    assert bt["in_effect"] is False and bt["actual"] == "auto"
    # default rules are in the trace, not in the record
    assert any("50-default.rules:4" in line for line in trace)
    assert all("50-default" not in e["rule"]
               for entries in applied.values() for e in entries)


def test_replay_never_runs_as_root(monkeypatch):
    monkeypatch.setattr(usb_udev.os, "geteuid", lambda: 0)
    cmd = usb_udev._trace_cmd("/sys/devices/x")
    assert cmd[0] == "setpriv" and "--reuid=65534" in cmd
    assert cmd[-4:] == ["udevadm", "test", "--action=add", "/sys/devices/x"]
    monkeypatch.setattr(usb_udev.os, "geteuid", lambda: 1000)
    assert usb_udev._trace_cmd("/x")[0] == "udevadm"


def test_no_replay_as_root_without_setpriv(monkeypatch):
    monkeypatch.setattr(usb_udev.os, "geteuid", lambda: 0)
    monkeypatch.setattr(usb_udev, "_have", lambda c: c == "udevadm")
    run = mock.Mock()
    monkeypatch.setattr(usb_udev.subprocess, "run", run)
    assert usb_udev.rules_applied(["1-3"], set()) == ({}, [], False)
    run.assert_not_called()


@pytest.mark.real_usb_udev
def test_harvest_puts_it_together(fake_rules, fake_sysfs, monkeypatch):
    traces = _trace_for(fake_sysfs)
    monkeypatch.setattr(usb_udev, "_have", lambda c: True)
    monkeypatch.setattr(usb_udev.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(usb_udev.subprocess, "run", lambda cmd, **kw:
                        subprocess.CompletedProcess(
                            cmd, 0, traces.get(cmd[-1], ""), ""))
    rule = "/etc/udev/rules.d/99-jo-usb.rules"
    out = usb_udev.harvest()
    assert out["partial"] is False
    devs = {d["sysfs_name"]: d for d in out["usb"]["devices"]}
    assert [e["in_effect"] for e in devs["1-14"]["udev_rules_applied"]] == \
        [False]
    assert rule in out["rule_files"]

    warnings = builder._udev_not_in_effect(out["usb"])
    assert warnings == [
        "The robot's udev rule 99-jo-usb.rules:14 should set power/control "
        "to 'on', but USB device 8087:0029 has 'auto' — that device setting "
        "isn't in effect during this recording."]


# -- the record, the crate and the diff -------------------------------------------

def _usb_doc(control="on", rule_sha="a" * 64, in_effect=True):
    return {
        "usb": {"usbcore": {"autosuspend": "2"}, "driver_parameters": {},
                "ports": [],
                "devices": [{"sysfs_name": "1-3", "port_path": "3",
                             "vendor_id": "2639", "product_id": "0301",
                             "serial": "DB9MF8VK",
                             "product": "MTi USB Converter",
                             "power": {"control": control},
                             "serial_ports": [{"tty": "ttyUSB0",
                                               "latency_timer_ms": "1"}],
                             "udev_rules_applied": [{
                                 "node": "1-3", "rule": "/etc/r.rules:14",
                                 "action": "ATTR", "attribute": "/x/power/"
                                 "control", "expected": "on",
                                 "actual": control,
                                 "in_effect": in_effect}]}]},
        "udev_rules": {"custom": [{"path": "/etc/udev/rules.d/99-jo.rules",
                                   "reason": "local", "sha256": rule_sha,
                                   "size_bytes": 10}],
                       "default": []},
    }


def test_crate_archives_the_rules_and_trace(fairy_dirs):
    from ros_fairy.archive import assembler
    from tests.unit.test_archive import _spool
    harvest, context = _spool(fairy_dirs)
    harvest.update(_usb_doc())
    harvest["raw_hardware"]["udev_rule_files"] = {
        "/etc/udev/rules.d/99-jo.rules": JO_RULE}
    harvest["raw_hardware"]["udev_trace"] = ["1-3: /etc/r.rules:14 ATTR"]
    record = builder.build(harvest, context)
    final = assembler.assemble(record, harvest)
    rule = final / "harvest/udev_rules/etc/udev/rules.d/99-jo.rules"
    assert rule.read_text() == JO_RULE
    assert record.udev_rules.custom[0].archived_path == \
        "harvest/udev_rules/etc/udev/rules.d/99-jo.rules"
    assert "ATTR" in (final / "harvest/udev_trace.txt").read_text()
    assert "udev rule file(s)" in (final / "README.md").read_text()


def test_diff_shows_rule_and_port_changes(fairy_dirs):
    from ros_fairy.ui import diff
    from tests.unit.test_archive import _spool
    harvest, context = _spool(fairy_dirs)
    a = builder.build({**harvest, **_usb_doc()}, context)
    b = builder.build({**harvest, **_usb_doc(control="auto", rule_sha="b" * 64,
                                             in_effect=False)}, context)
    rows = diff._diff_usb_udev(a, b)
    labels = [r[0] for r in rows]
    assert "udev rule /etc/udev/rules.d/99-jo.rules" in labels
    assert ("USB MTi USB Converter (DB9MF8VK): power/control", "on",
            "auto") in rows
    assert any(r[0].endswith("rules not in effect") for r in rows)

    old = a.model_copy(deep=True)
    old.usb, old.udev_rules = None, None   # a crate from before this change
    rows = diff._diff_usb_udev(old, b)
    assert ("USB details captured", "no", "yes") in rows
    assert not any(r[0].startswith("USB MTi") for r in rows)
