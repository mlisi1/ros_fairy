import pytest

from fairy.subcommands import doctor
from fairy.utils import harvest_tools


@pytest.fixture
def without(monkeypatch):
    """Make the given commands and Python modules look absent."""
    absent: set[str] = set()
    monkeypatch.setattr(
        harvest_tools.shutil, "which",
        lambda cmd, path=None: None if cmd in absent else f"/usr/bin/{cmd}")
    monkeypatch.setattr(
        harvest_tools.importlib.util, "find_spec",
        lambda name: None if name in absent else object())
    return absent


def test_nothing_missing(without):
    assert harvest_tools.missing_tools() == []
    assert harvest_tools.missing_modules() == []
    assert doctor._check_harvest_tools()["status"] == doctor.OK


def test_packages_are_listed_once(without):
    without.update({"setpriv", "dmesg", "lsusb"})
    assert harvest_tools.apt_packages(harvest_tools.missing_tools()) == \
        ["usbutils", "util-linux"]


def test_module_entry_points(without, capsys):
    without.update({"lsusb", "v4l2-ctl", "rocrate"})
    assert harvest_tools.main(["apt"]) == 0
    assert capsys.readouterr().out == "usbutils v4l-utils\n"
    harvest_tools.main(["pip"])
    assert capsys.readouterr().out == "rocrate\n"
    harvest_tools.main(["list"])
    listed = capsys.readouterr().out
    assert "lsusb (usbutils): USB devices" in listed
    assert "rocrate (Python)" in listed
    assert harvest_tools.main(["what"]) == 2


def test_sbin_is_searched(monkeypatch):
    seen = {}
    monkeypatch.setattr(harvest_tools.shutil, "which",
                        lambda cmd, path=None: seen.setdefault("path", path))
    harvest_tools.missing_tools()
    assert "/usr/sbin" in seen["path"].split(":")


def test_doctor_warns_with_the_install_commands(without):
    without.update({"lspci", "rocrate"})
    result = doctor._check_harvest_tools()
    assert result["status"] == doctor.WARN
    assert "lspci" in result["title"] and "rocrate" in result["title"]
    assert "PCI devices" in result["detail"]
    assert "sudo apt install pciutils" in result["hint"]
    assert "pip install --break-system-packages rocrate" in result["hint"]
    assert "./install.sh --with-deps" in result["hint"]
