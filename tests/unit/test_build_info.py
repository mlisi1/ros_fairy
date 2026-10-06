"""Which ros-fairy build captured and saved a mission (record format 1.2)."""

import json
import subprocess
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

from ros_fairy import build_info
from ros_fairy.archive import assembler
from ros_fairy.manifest import builder
from ros_fairy.subcommands import doctor, verify
from ros_fairy.ui import diff
from ros_fairy.utils import paths
from tests.unit.test_archive import _spool

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _fresh():
    build_info.build_info.cache_clear()
    yield
    build_info.build_info.cache_clear()


def _git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True,
                          text=True).stdout.strip()


@pytest.mark.skipif(not (REPO / ".git").exists(), reason="not a checkout")
def test_checkout_reports_its_commit():
    info = build_info.build_info()
    assert info["source"] == "checkout"
    assert info["commit"] == _git("rev-parse", "HEAD")
    assert info["code_id"] and info["version"]
    assert build_info.short(info).startswith(_git("rev-parse", "--short",
                                                  "HEAD")[:7])


def test_install_reports_what_setup_wrote(monkeypatch):
    fake = types.ModuleType("ros_fairy._build_info")
    fake.BUILD = {"commit": "a" * 40, "describe": "v1.0-2-gaaaaaaa",
                  "branch": "main", "dirty": False,
                  "built_at": "2026-10-06T10:00:00+00:00"}
    monkeypatch.setitem(sys.modules, "ros_fairy._build_info", fake)
    info = build_info.build_info()
    assert info["source"] == "install" and info["commit"] == "a" * 40
    assert build_info.short(info) == "v1.0-2-gaaaaaaa"


def test_short_labels():
    assert build_info.short(None) == "unknown"
    assert build_info.short({"commit": "b" * 40, "dirty": True}) == \
        "bbbbbbb-dirty"
    assert build_info.short({"version": "0.1.0", "code_id": "c0de"}) == \
        "0.1.0 (code c0de)"
    assert build_info.same_code({"code_id": "x"}, {"code_id": "x"})
    assert not build_info.same_code({"code_id": "x"}, {"code_id": "y"})
    assert not build_info.same_code(None, {"code_id": "y"})


def test_git_is_asked_with_safe_directory(tmp_path):
    """A root install of an operator-owned checkout: git would refuse it as
    'dubious ownership' without safe.directory."""
    with mock.patch.object(build_info.subprocess, "run",
                           return_value=subprocess.CompletedProcess(
                               [], 0, "abc\n", "")) as run:
        build_info.git_details(tmp_path)
    assert all(f"safe.directory={tmp_path}" in call.args[0]
               for call in run.call_args_list)


@pytest.mark.skipif(not (REPO / ".git").exists(), reason="not a checkout")
def test_setup_writes_build_details():
    namespace = {"__file__": str(REPO / "setup.py"), "__name__": "setup"}
    with mock.patch("setuptools.setup"):
        exec(compile((REPO / "setup.py").read_text(), "setup.py", "exec"),
             namespace)
    details = namespace["_build_details"]()
    assert details["commit"] == _git("rev-parse", "HEAD")
    assert details["built_at"]
    assert "BuildPyWithBuildInfo" in namespace


# -- in the record, the crate and the tools ---------------------------------------

def test_saved_crate_names_both_builds(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    assert harvest["provenance"]["harvested_by"]["code_id"]
    record = builder.build(harvest, context)
    crate = assembler.assemble(record, harvest)
    saved = json.loads((crate / "mission_record.json").read_text())
    mine = build_info.build_info()
    assert saved["provenance"]["assembled_by"]["code_id"] == mine["code_id"]
    assert saved["provenance"]["harvested_by"]["code_id"] == mine["code_id"]
    graph = json.loads((crate / "ro-crate-metadata.json").read_text())["@graph"]
    app = next(e for e in graph if e.get("name") == "ros-fairy")
    if mine["commit"]:
        assert app["identifier"] == mine["commit"]
        assert app["softwareVersion"] == (mine["describe"] or mine["commit"])
    checks = verify.verify_archive(crate)
    assert any(c["title"].startswith("Saved by ros-fairy ") for c in checks)


def test_capture_by_another_build_is_pointed_out(fairy_dirs):
    harvest, _ = _spool(fairy_dirs)
    harvest["provenance"]["harvested_by"] = {
        **build_info.build_info(), "code_id": "0ld", "describe": "0ldbuild"}
    warnings = builder.harvest_level_warnings(harvest)
    assert any("captured by a different ros-fairy build (0ldbuild)" in w
               for w in warnings)
    harvest["provenance"]["harvested_by"] = build_info.build_info()
    assert not any("different ros-fairy build" in w
                   for w in builder.harvest_level_warnings(harvest))


def test_diff_shows_build_changes(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    a = builder.build(harvest, context)
    b = a.model_copy(deep=True)
    b.provenance.harvested_by = b.provenance.harvested_by.model_copy(
        update={"code_id": "new", "describe": "newbuild"})
    rows = diff._diff_context(a, b)
    assert ("Captured by ros-fairy", build_info.short(
        a.provenance.harvested_by.model_dump()), "newbuild") in rows
    old = a.model_copy(deep=True)
    old.provenance.harvested_by = None  # a record from before 1.2
    assert not any(r[0].startswith("Captured by")
                   for r in diff._diff_context(old, b))


def test_doctor_names_the_builds(fairy_dirs):
    paths.watchdog_state_path().write_text(json.dumps(
        {"code_id": "0ld", "build": "d3a47db"}))
    detail = doctor._check_watchdog_code()["detail"]
    assert "watchdog d3a47db" in detail
    assert build_info.short(build_info.build_info()) in detail
