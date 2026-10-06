"""Which build of ros-fairy is running: version, git commit, code id.

``__version__`` alone can't tell two installs apart. The git details are
written into the installed package at build time (``setup.py`` generates
``ros_fairy/_build_info.py``: an installed package has no ``.git``); a
development checkout without that file asks git directly. ``code_id`` (a hash
of the package's source files) is always there, and also catches edits made
after the build.
"""

import functools
import subprocess
from pathlib import Path
from typing import Any

from ros_fairy import __version__
from ros_fairy.utils.code_id import code_id

GIT_TIMEOUT_S = 5


def git_details(repo: Path) -> dict[str, Any]:
    """commit / describe / branch / dirty of the checkout at ``repo``.
    Empty when it isn't one, or git isn't available."""
    def git(*args: str) -> str | None:
        try:
            # safe.directory: a root install (sudo ./install.sh) of a checkout
            # owned by the operator is otherwise refused as "dubious
            # ownership".
            out = subprocess.run(
                ["git", "-c", f"safe.directory={repo}", "-C", str(repo),
                 *args], capture_output=True, text=True,
                timeout=GIT_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    if not commit:
        return {}
    status = git("status", "--porcelain", "--untracked-files=no")
    return {
        "commit": commit,
        "describe": git("describe", "--always", "--dirty", "--tags"),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(status) if status is not None else None,
    }


@functools.lru_cache(maxsize=1)
def build_info() -> dict[str, Any]:
    """This process's build, computed once (a long-running process keeps
    reporting the code it started with)."""
    info: dict[str, Any] = {"version": __version__, "commit": None,
                            "describe": None, "branch": None, "dirty": None,
                            "built_at": None, "code_id": code_id(),
                            "source": "unknown"}
    try:
        from ros_fairy import _build_info  # written by setup.py at build
        info.update(_build_info.BUILD)
        info["source"] = "install"
    except ImportError:
        repo = Path(__file__).resolve().parent.parent
        details = git_details(repo) if (repo / ".git").exists() else {}
        if details:
            info.update(details)
            info["source"] = "checkout"
    return info


def short(info: dict[str, Any] | None) -> str:
    """One human label: "d3a47db", "v0.2.0-3-gd3a47db-dirty", or the
    version and code id when no commit is known."""
    if not info:
        return "unknown"
    if info.get("describe"):
        return info["describe"]
    if info.get("commit"):
        return info["commit"][:7] + ("-dirty" if info.get("dirty") else "")
    return f"{info.get('version', '?')} (code {info.get('code_id', '?')})"


def same_code(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    """Whether two builds run the same code (by code id, which covers both
    the commit and any uncommitted edits)."""
    return bool(a and b and a.get("code_id") == b.get("code_id"))
