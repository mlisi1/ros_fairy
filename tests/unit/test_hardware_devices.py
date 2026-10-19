"""Unit tests for fairy.harvest.hardware_devices.

No real USB devices, cameras, or serial ports required. All external commands
and filesystem globs are monkeypatched.
"""

import subprocess
from unittest.mock import MagicMock

import fairy.harvest.hardware_devices as hd

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

LSUSB_OUTPUT = """\
Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub
Bus 002 Device 003: ID 1546:01a9 u-blox AG ZED-F9P
Bus 002 Device 004: ID 046d:c52b Logitech, Inc. Unifying Receiver
"""

LSUSB_VERBOSE_SNIPPET = """\
Bus 002 Device 003: ID 1546:01a9 u-blox AG ZED-F9P
  iSerial                 3 3C1234567890
Bus 002 Device 004: ID 046d:c52b Logitech, Inc. Unifying Receiver
  iSerial                 3 0
"""

LSPCI_OUTPUT = """\
00:00.0 "Host bridge" "Intel Corporation" "Alder Lake" -r01 "ASUSTeK" "Device 1234"
00:1f.3 "Audio device" "Intel Corporation" "Tiger Lake HD Audio" -r11 "" ""
"""

V4L2_OUTPUT = """\
USB 2.0 Camera (usb-0000:00:14.0-5):
\t/dev/video0
\t/dev/video1

"""

UDEVADM_OUTPUT = """\
DEVNAME=/dev/ttyUSB0
DEVTYPE=usb_device
SUBSYSTEM=usb
ID_VENDOR=u-blox_AG
ID_VENDOR_ID=1546
ID_MODEL=u-blox_GNSS_receiver
ID_MODEL_ID=01a9
ID_SERIAL_SHORT=3C1234567890
ID_DRIVER=cdc_acm
ID_PATH=pci-0000:00:14.0-usb-0:1:1.0
SOME_SECRET_KEY=shouldnotappear
"""

DMESG_OUTPUT = """\
[Mon Jun 10 12:00:00 2026] usb 2-1: new full-speed USB device number 3 using xhci_hcd
[Mon Jun 10 12:00:01 2026] usb 2-1: New USB device found, idVendor=1546, idProduct=01a9
[Mon Jun 10 12:00:01 2026] cdc_acm 2-1:1.0: ttyACM0: USB ACM device
[Mon Jun 10 12:00:02 2026] eth0: renamed from veth1234
[Mon Jun 10 12:00:03 2026] EXT4-fs (sda1): mounted filesystem
[Mon Jun 10 12:00:04 2026] video4linux2: V4L2 device registered as /dev/video0
"""


def _completed(stdout: str = "", returncode: int = 0,
               stderr: str = "") -> MagicMock:
    r = MagicMock()
    r.stdout = stdout
    r.returncode = returncode
    r.stderr = stderr
    return r


def _no_which(name):
    return None


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_lsusb_parse_basic(monkeypatch):
    monkeypatch.setattr(hd.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(
        hd.subprocess, "run",
        lambda cmd, **kw: _completed(LSUSB_OUTPUT)
        if cmd[0] == "lsusb" and len(cmd) == 1
        else _completed(returncode=1))
    # Also stub glob and Path so no real /dev access
    monkeypatch.setattr(hd, "glob", lambda p: [])
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)

    result = hd.harvest()
    usb = [d for d in result["devices"] if d["device_class"] == "usb"]
    assert len(usb) == 3
    assert usb[1]["vendor_id"] == "1546"
    assert usb[1]["product_id"] == "01a9"
    assert usb[1]["product_name"] == "u-blox AG ZED-F9P"
    assert usb[1]["bus_path"] == "Bus 002 Device 003"


def test_lsusb_unavailable(monkeypatch):
    monkeypatch.setattr(hd.shutil, "which", _no_which)
    monkeypatch.setattr(hd, "glob", lambda p: [])
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)
    result = hd.harvest()
    assert result["devices"] == []
    assert result["status"] == "skipped"


def test_lsusb_verbose_stored(monkeypatch):
    monkeypatch.setattr(hd.shutil, "which", lambda n: "/usr/bin/" + n)

    def fake_run(cmd, **kw):
        if cmd == ["lsusb"]:
            return _completed(LSUSB_OUTPUT)
        if cmd == ["lsusb", "-v"]:
            return _completed(LSUSB_VERBOSE_SNIPPET)
        return _completed(returncode=1)

    monkeypatch.setattr(hd.subprocess, "run", fake_run)
    monkeypatch.setattr(hd, "glob", lambda p: [])
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)

    result = hd.harvest()
    assert result["lsusb_verbose"] == LSUSB_VERBOSE_SNIPPET
    # Serial number should be populated from verbose output
    gps = next(d for d in result["devices"]
               if d.get("vendor_id") == "1546")
    assert gps["serial_number"] == "3C1234567890"
    # iSerial=0 means no serial, should stay None
    logi = next(d for d in result["devices"]
                if d.get("vendor_id") == "046d")
    assert logi["serial_number"] is None


def test_lsusb_verbose_permission_denied(monkeypatch):
    monkeypatch.setattr(hd.shutil, "which", lambda n: "/usr/bin/" + n)

    def fake_run(cmd, **kw):
        if cmd == ["lsusb"]:
            return _completed(LSUSB_OUTPUT)
        if cmd == ["lsusb", "-v"]:
            return _completed(returncode=1, stderr="Permission denied")
        return _completed(returncode=1)

    monkeypatch.setattr(hd.subprocess, "run", fake_run)
    monkeypatch.setattr(hd, "glob", lambda p: [])
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)

    result = hd.harvest()
    assert result["lsusb_verbose"] is None
    assert result["status"] == "partial"
    for d in result["devices"]:
        assert d["serial_number"] is None


def test_lsusb_verbose_timeout(monkeypatch):
    monkeypatch.setattr(hd.shutil, "which", lambda n: "/usr/bin/" + n)

    def fake_run(cmd, **kw):
        if cmd == ["lsusb"]:
            return _completed(LSUSB_OUTPUT)
        if cmd == ["lsusb", "-v"]:
            raise subprocess.TimeoutExpired(cmd, 20)
        return _completed(returncode=1)

    monkeypatch.setattr(hd.subprocess, "run", fake_run)
    monkeypatch.setattr(hd, "glob", lambda p: [])
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)

    result = hd.harvest()
    assert result["lsusb_verbose"] is None
    # USB devices from basic lsusb should still be present
    assert any(d["device_class"] == "usb" for d in result["devices"])


def test_lspci_parse(monkeypatch):
    monkeypatch.setattr(hd.shutil, "which", lambda n: "/usr/bin/" + n)

    def fake_run(cmd, **kw):
        if cmd[:2] == ["lspci", "-mm"]:
            return _completed(LSPCI_OUTPUT)
        return _completed(returncode=1)

    monkeypatch.setattr(hd.subprocess, "run", fake_run)
    monkeypatch.setattr(hd, "glob", lambda p: [])
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)

    result = hd.harvest()
    pci = [d for d in result["devices"] if d["device_class"] == "pci"]
    assert len(pci) == 2
    assert pci[0]["vendor_name"] == "Intel Corporation"
    assert pci[0]["product_name"] == "Alder Lake"
    assert pci[0]["source_command"] == "lspci"


def test_lspci_unavailable(monkeypatch):
    def which_no_lspci(name):
        return None if name == "lspci" else f"/usr/bin/{name}"

    monkeypatch.setattr(hd.shutil, "which", which_no_lspci)
    monkeypatch.setattr(hd.subprocess, "run",
                        lambda cmd, **kw: _completed(returncode=1))
    monkeypatch.setattr(hd, "glob", lambda p: [])
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)

    result = hd.harvest()
    assert not any(d["device_class"] == "pci" for d in result["devices"])


def test_video_device_glob(monkeypatch):
    monkeypatch.setattr(hd.shutil, "which", _no_which)

    def fake_glob(pattern):
        if "video" in pattern:
            return ["/dev/video0", "/dev/video1"]
        return []

    monkeypatch.setattr(hd, "glob", fake_glob)
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)

    result = hd.harvest()
    video = [d for d in result["devices"] if d["device_class"] == "video"]
    assert len(video) == 2
    assert video[0]["device_path"] == "/dev/video0"
    assert video[0]["source_command"] == "glob:/dev/video*"


def test_serial_device_glob(monkeypatch):
    monkeypatch.setattr(hd.shutil, "which", _no_which)

    def fake_glob(pattern):
        if "ttyUSB" in pattern:
            return ["/dev/ttyUSB0"]
        return []

    monkeypatch.setattr(hd, "glob", fake_glob)
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)

    result = hd.harvest()
    serial = [d for d in result["devices"] if d["device_class"] == "serial"]
    assert len(serial) == 1
    assert serial[0]["device_path"] == "/dev/ttyUSB0"


def test_udevadm_enrichment(monkeypatch):
    monkeypatch.setattr(hd.shutil, "which", lambda n: "/usr/bin/" + n)

    def fake_glob(pattern):
        if "ttyUSB" in pattern:
            return ["/dev/ttyUSB0"]
        return []

    monkeypatch.setattr(hd, "glob", fake_glob)
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)

    def fake_run(cmd, **kw):
        if cmd[0] == "udevadm":
            return _completed(UDEVADM_OUTPUT)
        if cmd[0] == "lsusb" and len(cmd) == 1:
            return _completed("")
        return _completed(returncode=1)

    monkeypatch.setattr(hd.subprocess, "run", fake_run)

    result = hd.harvest()
    tty = next(d for d in result["devices"]
               if d.get("device_path") == "/dev/ttyUSB0")
    assert tty["driver"] == "cdc_acm"
    assert tty["vendor_name"] == "u-blox_AG"
    assert tty["serial_number"] == "3C1234567890"


def test_udev_driver_prefers_id_usb_driver(monkeypatch):
    """A USB serial adapter's node only names its driver in ID_USB_DRIVER."""
    monkeypatch.setattr(hd.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(hd, "glob", lambda p: ["/dev/ttyUSB0"]
                        if "ttyUSB" in p else [])
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)
    props = "DEVNAME=/dev/ttyUSB0\nSUBSYSTEM=tty\nDRIVER=usb\n" \
            "ID_USB_DRIVER=ftdi_sio\n"

    def fake_run(cmd, **kw):
        if cmd[0] == "udevadm":
            return _completed(props)
        return _completed(returncode=1)

    monkeypatch.setattr(hd.subprocess, "run", fake_run)
    tty = next(d for d in hd.harvest()["devices"]
               if d.get("device_path") == "/dev/ttyUSB0")
    assert tty["driver"] == "ftdi_sio"
    assert tty["udev_properties"]["ID_USB_DRIVER"] == "ftdi_sio"


def test_udev_whitelist_enforced(monkeypatch):
    monkeypatch.setattr(hd.shutil, "which", lambda n: "/usr/bin/" + n)

    def fake_glob(pattern):
        return ["/dev/ttyUSB0"] if "ttyUSB" in pattern else []

    monkeypatch.setattr(hd, "glob", fake_glob)
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)

    def fake_run(cmd, **kw):
        if cmd[0] == "udevadm":
            return _completed(UDEVADM_OUTPUT)
        return _completed(returncode=1)

    monkeypatch.setattr(hd.subprocess, "run", fake_run)

    result = hd.harvest()
    tty = next(d for d in result["devices"]
               if d.get("device_path") == "/dev/ttyUSB0")
    props = tty["udev_properties"] or {}
    assert "SOME_SECRET_KEY" not in props
    for key in props:
        assert key in hd._UDEV_WHITELIST


def test_udevadm_timeout(monkeypatch):
    monkeypatch.setattr(hd.shutil, "which", lambda n: "/usr/bin/" + n)

    def fake_glob(pattern):
        return ["/dev/ttyUSB0"] if "ttyUSB" in pattern else []

    monkeypatch.setattr(hd, "glob", fake_glob)
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)

    def fake_run(cmd, **kw):
        if cmd[0] == "udevadm":
            raise subprocess.TimeoutExpired(cmd, 5)
        return _completed(returncode=1)

    monkeypatch.setattr(hd.subprocess, "run", fake_run)

    result = hd.harvest()
    tty = next(d for d in result["devices"]
               if d.get("device_path") == "/dev/ttyUSB0")
    assert tty["udev_properties"] is None


def test_v4l2_parse(monkeypatch):
    monkeypatch.setattr(hd.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(hd, "glob", lambda p: [])
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)

    def fake_run(cmd, **kw):
        if cmd == ["v4l2-ctl", "--list-devices"]:
            return _completed(V4L2_OUTPUT)
        return _completed(returncode=1)

    monkeypatch.setattr(hd.subprocess, "run", fake_run)

    result = hd.harvest()
    video = [d for d in result["devices"] if d["device_class"] == "video"]
    paths = {d["device_path"] for d in video}
    assert "/dev/video0" in paths
    assert "/dev/video1" in paths


def test_dmesg_filtered_keywords(monkeypatch):
    monkeypatch.setattr(hd.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(hd, "glob", lambda p: [])
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)

    def fake_run(cmd, **kw):
        if cmd[0] == "dmesg":
            return _completed(DMESG_OUTPUT)
        return _completed(returncode=1)

    monkeypatch.setattr(hd.subprocess, "run", fake_run)

    result = hd.harvest()
    assert result["dmesg_usb"] is not None
    lines = result["dmesg_usb"].splitlines()
    # "eth0: renamed" and "EXT4-fs" lines must be excluded
    assert not any("eth0" in line for line in lines)
    assert not any("EXT4" in line for line in lines)
    # USB and video lines must be present
    assert any("usb" in line.lower() for line in lines)
    assert any("video4linux" in line.lower() or "tty" in line.lower()
               for line in lines)


def test_dmesg_permission_denied(monkeypatch):
    monkeypatch.setattr(hd.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(hd, "glob", lambda p: [])
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)

    def fake_run(cmd, **kw):
        if cmd[0] == "dmesg":
            return _completed(returncode=1, stderr="Operation not permitted")
        return _completed(returncode=1)

    monkeypatch.setattr(hd.subprocess, "run", fake_run)

    result = hd.harvest()
    assert result["dmesg_usb"] is None


def test_harvest_empty_when_no_devices(monkeypatch):
    monkeypatch.setattr(hd.shutil, "which", _no_which)
    monkeypatch.setattr(hd, "glob", lambda p: [])
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)

    result = hd.harvest()
    assert result["devices"] == []
    assert result["status"] == "skipped"
    assert result["lsusb_verbose"] is None
    assert result["dmesg_usb"] is None
