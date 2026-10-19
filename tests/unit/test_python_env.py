"""Unit tests for fairy.harvest.python_env.

All subprocess and importlib.metadata calls are monkeypatched; no pip or
real package index required.
"""

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import fairy.harvest.python_env as pe

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fake_dist(name: str, version: str, installer: str | None = "pip",
               direct_url: dict | None = None,
               verbs: bool = True) -> MagicMock:
    dist = MagicMock()
    # Ours provides `ros2 fairy`'s verbs; PyPI's unrelated fairy doesn't.
    dist.entry_points = [SimpleNamespace(group="fairy.verb")] if verbs else []
    # Mirror importlib.metadata.Distribution: .name/.version are properties
    # backed by the metadata mapping. The harvest reads the properties.
    dist.metadata = {"Name": name, "Version": version}
    dist.name = name
    dist.version = version

    def read_text(fname):
        if fname == "INSTALLER":
            return installer
        if fname == "direct_url.json" and direct_url is not None:
            return json.dumps(direct_url)
        return None

    dist.read_text = read_text
    return dist


def _completed(stdout: str = "", returncode: int = 0) -> MagicMock:
    r = MagicMock()
    r.stdout = stdout
    r.returncode = returncode
    return r


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_basic_structure(monkeypatch):
    dists = [_fake_dist("rich", "13.0.0"), _fake_dist("pydantic", "2.5.0")]
    monkeypatch.setattr(pe, "distributions", lambda: iter(dists))
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: _completed('[]' if "--format=json" in a[0] else ""))
    result = pe.harvest()
    assert "python_env" in result
    assert "pip_freeze" in result
    assert "pip_list_json" in result
    assert "status" in result
    env = result["python_env"]
    assert env["executable"] == sys.executable
    assert env["version"] == sys.version
    assert isinstance(env["packages"], list)
    assert len(env["packages"]) == 2


def test_executable_and_version_are_real():
    # These come directly from sys — no mocking needed.
    result = pe.harvest()
    assert result["python_env"]["executable"] == sys.executable
    assert result["python_env"]["version"] == sys.version


def test_venv_from_env_var(monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "/opt/venv")
    monkeypatch.setattr(pe, "distributions", lambda: iter([]))
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _completed())
    result = pe.harvest()
    assert result["python_env"]["venv_path"] == "/opt/venv"


def test_venv_none_when_system_interpreter(monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setattr(sys, "prefix", "/usr")
    monkeypatch.setattr(pe, "distributions", lambda: iter([]))
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _completed())
    result = pe.harvest()
    assert result["python_env"]["venv_path"] is None


def test_ros_fairy_editable_true(monkeypatch):
    dists = [_fake_dist(
        "fairy", "0.1.0",
        direct_url={"url": "file:///home/dev/ros_fairy",
                    "dir_info": {"editable": True}})]
    monkeypatch.setattr(pe, "distributions", lambda: iter(dists))
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _completed())
    result = pe.harvest()
    assert result["python_env"]["ros_fairy_editable"] is True
    pkg = result["python_env"]["packages"][0]
    assert pkg["editable"] is True
    assert pkg["location"] == "/home/dev/ros_fairy"


def test_ros_fairy_editable_false(monkeypatch):
    dists = [_fake_dist("fairy", "0.1.0")]  # no direct_url.json
    monkeypatch.setattr(pe, "distributions", lambda: iter(dists))
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _completed())
    result = pe.harvest()
    assert result["python_env"]["ros_fairy_editable"] is False


def test_an_unrelated_fairy_is_not_taken_for_ours(monkeypatch):
    dists = [_fake_dist("fairy", "0.1.0", verbs=False,
                        direct_url={"url": "file:///x",
                                    "dir_info": {"editable": True}})]
    monkeypatch.setattr(pe, "distributions", lambda: iter(dists))
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _completed())
    assert pe.harvest()["python_env"]["ros_fairy_editable"] is False


def test_pip_unavailable(monkeypatch):
    monkeypatch.setattr(pe, "distributions", lambda: iter([
        _fake_dist("somelib", "1.0.0")]))

    def fake_run(cmd, **kw):
        raise FileNotFoundError("pip not found")

    monkeypatch.setattr("subprocess.run", fake_run)
    result = pe.harvest()
    env = result["python_env"]
    assert env["packages"]  # importlib.metadata still worked
    assert result["pip_freeze"] is None
    assert result["pip_list_json"] is None
    assert result["status"] == "partial"


def test_pip_freeze_timeout(monkeypatch):
    import subprocess
    monkeypatch.setattr(pe, "distributions", lambda: iter([
        _fake_dist("somelib", "1.0.0")]))

    call_count = {"n": 0}

    def fake_run(cmd, **kw):
        call_count["n"] += 1
        if "freeze" in cmd:
            raise subprocess.TimeoutExpired(cmd, 30)
        return _completed('[]')

    monkeypatch.setattr("subprocess.run", fake_run)
    result = pe.harvest()
    assert result["pip_freeze"] is None
    assert result["pip_list_json"] is not None  # list succeeded


def test_installer_from_dist_info(monkeypatch):
    dists = [_fake_dist("conda-pkg", "2.0.0", installer="conda")]
    monkeypatch.setattr(pe, "distributions", lambda: iter(dists))
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _completed())
    result = pe.harvest()
    assert result["python_env"]["packages"][0]["installer"] == "conda"


def test_installer_none_when_absent(monkeypatch):
    dists = [_fake_dist("manual-pkg", "1.0.0", installer=None)]
    monkeypatch.setattr(pe, "distributions", lambda: iter(dists))
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _completed())
    result = pe.harvest()
    assert result["python_env"]["packages"][0]["installer"] is None


def test_editable_location_from_pip_list(monkeypatch):
    """pip list --format=json editable_project_location fills in when
    direct_url.json is absent."""
    dists = [_fake_dist("mylib", "0.0.1", installer="pip")]
    monkeypatch.setattr(pe, "distributions", lambda: iter(dists))
    pip_list = json.dumps([{
        "name": "mylib", "version": "0.0.1",
        "editable_project_location": "/home/dev/mylib"}])
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **kw: _completed(
            pip_list if "--format=json" in cmd else "mylib==0.0.1\n"))
    result = pe.harvest()
    pkg = result["python_env"]["packages"][0]
    assert pkg["editable"] is True
    assert pkg["location"] == "/home/dev/mylib"
