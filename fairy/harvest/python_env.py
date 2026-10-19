"""Harvest the Python runtime and installed packages.

Uses sys/importlib.metadata for structured data (always available, no
subprocess). Subprocess pip calls are best-effort extras stored in
raw_python_env for archiving; their failure never blocks the harvest.
"""

import json
import logging
import os
import subprocess
import sys
from importlib.metadata import Distribution, distributions
from pathlib import Path
from typing import Any

log = logging.getLogger("fairy.harvest.python_env")

PIP_TIMEOUT_S = 30


def _venv_path() -> str | None:
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        return venv
    prefix = sys.prefix
    if Path(prefix) in (Path("/usr"), Path("/usr/local")):
        return None
    return prefix


def _editable_location(dist: Distribution) -> tuple[bool, str | None]:
    """Return (editable, source_path) from PEP 610 direct_url.json."""
    try:
        raw = dist.read_text("direct_url.json")
        if raw is None:
            return False, None
        data = json.loads(raw)
        url = data.get("url", "")
        dir_info = data.get("dir_info", {})
        editable = bool(dir_info.get("editable", False))
        location = url[len("file://"):] if url.startswith("file://") else None
        return editable, location
    except Exception:
        return False, None


def _installer(dist: Distribution) -> str | None:
    try:
        raw = dist.read_text("INSTALLER")
        return raw.strip() if raw else None
    except Exception:
        return None


def _pip_version() -> str | None:
    try:
        from importlib.metadata import version
        return version("pip")
    except Exception:
        return None


def _run_pip(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip"] + args,
            capture_output=True, text=True, timeout=PIP_TIMEOUT_S)
        return result.stdout if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        log.debug("pip %s failed: %s", " ".join(args), exc)
        return None


def harvest() -> dict:
    """Collect Python environment metadata. Never raises.

    Returns a dict with keys:
      "python_env"   — structured data (maps to PythonEnv schema model)
      "pip_freeze"   — raw pip freeze text, or None (goes to raw_python_env)
      "pip_list_json"— raw pip list --format=json text, or None
      "status"       — "ok" or "partial" (used by run_pipeline to set harvest_status)
    """
    try:
        return _harvest()
    except Exception as exc:
        log.warning("python_env harvest failed entirely: %s", exc)
        return {
            "python_env": {
                "executable": sys.executable,
                "version": sys.version,
                "venv_path": None,
                "pip_version": None,
                "packages": [],
                "ros_fairy_editable": False,
                "sys_path": list(sys.path),
            },
            "pip_freeze": None,
            "pip_list_json": None,
            "status": "partial",
        }


def _harvest() -> dict:
    packages: list[dict[str, Any]] = []
    ros_fairy_editable = False

    for dist in sorted(distributions(),
                       key=lambda d: (d.name or "").lower()):
        name = dist.name or ""
        version = dist.version or ""
        if not name:
            continue
        editable, location = _editable_location(dist)
        pkg: dict[str, Any] = {
            "name": name,
            "version": version,
            "installer": _installer(dist),
            "editable": editable,
            "location": location,
        }
        packages.append(pkg)
        # By its verbs too: PyPI has an unrelated package called fairy.
        if name.lower().replace("-", "_") == "fairy" and any(
                e.group == "fairy.verb" for e in dist.entry_points):
            ros_fairy_editable = editable

    pip_freeze = _run_pip(["freeze"])
    pip_list_json = _run_pip(["list", "--format=json"])

    # pip list --format=json exposes editable_project_location (pip >= 22.1);
    # use it to fill in location when direct_url.json wasn't present.
    if pip_list_json:
        try:
            by_name = {p["name"].lower(): p
                       for p in json.loads(pip_list_json)}
            for pkg in packages:
                entry = by_name.get(pkg["name"].lower())
                if entry and pkg["location"] is None:
                    loc = entry.get("editable_project_location")
                    if loc:
                        pkg["location"] = loc
                        pkg["editable"] = True
        except (json.JSONDecodeError, AttributeError, KeyError):
            pass

    partial = pip_freeze is None and pip_list_json is None

    return {
        "python_env": {
            "executable": sys.executable,
            "version": sys.version,
            "venv_path": _venv_path(),
            "pip_version": _pip_version(),
            "packages": packages,
            "ros_fairy_editable": ros_fairy_editable,
            "sys_path": list(sys.path),
        },
        "pip_freeze": pip_freeze,
        "pip_list_json": pip_list_json,
        "status": "partial" if partial else "ok",
    }
