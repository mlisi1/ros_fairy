"""Snapshot of running Docker containers. Graceful no-op without Docker."""

import json
import logging
import re
import subprocess
import time
from typing import Any

log = logging.getLogger("ros_fairy.harvest.docker_info")

DOCKER_TIMEOUT_S = 10
# `ros2 pkg list` exec'd into a container can legitimately take a few
# seconds (spawning python, scanning the ament index) — this budget is
# deliberately separate from DOCKER_TIMEOUT_S (which covers the quick `ps`/
# `inspect` calls) so probing containers' package lists can never starve
# the rest of the docker harvest, the same starvation bug that used to hit
# harvest/ros_graph.py's per-node param dumps.
CONTAINER_EXEC_TIMEOUT_S = 10
CONTAINER_PKG_LIST_BUDGET_S = 30

_COMPOSE_PROJECT = "com.docker.compose.project"
_COMPOSE_FILES = "com.docker.compose.project.config_files"

# What a probe prints between these lines is its answer; anything else (a
# .bashrc banner, an `echo` in a sourced setup file) is not.
_BEGIN, _END = "__ros_fairy_begin__", "__ros_fairy_end__"
# ROS package names: lowercase letters, digits and underscores (REP 144).
_ROS_PACKAGE = re.compile(r"^[a-z][a-z0-9_]*$")
# Python packages in the container, read with importlib.metadata (pip is
# often not installed in robot images).
_PY_LIST = ("import json, importlib.metadata as m; "
            "print(json.dumps(sorted({(d.metadata['Name'], d.version) "
            "for d in m.distributions() if d.metadata['Name']})))")


def _run(args: list[str], timeout: float, partial_ok: bool = False
         ) -> str | None:
    """``docker <args>``'s stdout, or None. Output is decoded leniently: one
    odd byte must not lose the whole result. ``partial_ok`` keeps stdout
    from a non-zero exit (``docker inspect`` of several objects exits 1 if
    any one of them has gone, but still prints the others)."""
    try:
        result = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0 and not (partial_ok and result.stdout.strip()):
        return None
    return result.stdout


def _in_container(container_id: str, script: str, timeout: float,
                  login_shell: bool) -> list[str] | None:
    """Lines a shell ``script`` printed inside the container, or None.

    The command runs under the container's own ``timeout`` when it has one:
    a timeout here only kills the ``docker exec`` client, and the process
    inside would otherwise be left running in the robot's container. The
    answer is read between markers, so login-shell noise isn't taken for it.
    """
    limit = max(1, int(timeout) - 1)
    wrapped = f"echo {_BEGIN}; {script}; echo {_END}"
    shell = ["bash", "-ic", wrapped] if login_shell else ["sh", "-c", wrapped]
    guard = ("if command -v timeout >/dev/null 2>&1; then "
             f'exec timeout -k 2 {limit} "$@"; else exec "$@"; fi')
    out = _run(["exec", container_id, "sh", "-c", guard, "sh", *shell],
               timeout=timeout)
    if out is None:
        return None
    lines = out.splitlines()
    try:
        start = lines.index(_BEGIN) + 1
        end = lines.index(_END, start)
    except ValueError:
        return None  # cut short: not a complete answer
    return [line.strip() for line in lines[start:end] if line.strip()]


def harvest() -> dict[str, Any]:
    """Return {docker_containers: [...], raw_inspect: [...], available: bool}.

    Never raises: any Docker problem yields an empty result with
    available=False so the watchdog records status 'skipped'.
    """
    empty = {"docker_containers": [], "raw_inspect": [], "available": False}
    deadline = time.monotonic() + DOCKER_TIMEOUT_S

    ps = _run(["ps", "-q"], timeout=DOCKER_TIMEOUT_S)
    if ps is None:
        return empty
    ids = [line.strip() for line in ps.splitlines() if line.strip()]
    if not ids:
        return {**empty, "available": True}

    # A container that stopped since `docker ps` makes inspect exit 1; the
    # others are still printed and still worth recording.
    inspect_out = _run(["inspect", *ids],
                       timeout=max(1.0, deadline - time.monotonic()),
                       partial_ok=True)
    if inspect_out is None:
        return empty
    try:
        raw = json.loads(inspect_out)
    except json.JSONDecodeError:
        return empty
    if len(raw) < len(ids):
        log.info("%d container(s) stopped while being inspected",
                 len(ids) - len(raw))

    digests = _image_digests([entry.get("Image") for entry in raw])
    # Separate budget from `deadline` above — see CONTAINER_PKG_LIST_BUDGET_S.
    pkg_deadline = time.monotonic() + CONTAINER_PKG_LIST_BUDGET_S

    containers = []
    for entry in raw:
        config = entry.get("Config") or {}
        labels = config.get("Labels") or {}
        running = (entry.get("State") or {}).get("Running") is True
        container_id = entry.get("Id")
        probe = running and container_id
        ros_packages = _container_ros_packages(container_id, pkg_deadline) \
            if probe else None
        containers.append({
            "name": (entry.get("Name") or "").lstrip("/"),
            "image": config.get("Image") or "",
            "digest": digests.get(entry.get("Image")),
            "compose_project": labels.get(_COMPOSE_PROJECT),
            "compose_file": labels.get(_COMPOSE_FILES),
            "ros_packages": ros_packages,
            # Only for containers running ROS: that is the robot's software,
            # and the watchdog's own Python (python_env) is not its Python.
            "python_packages": _container_python_packages(
                container_id, pkg_deadline) if probe and ros_packages
            else None,
        })
    return {"docker_containers": containers, "raw_inspect": raw,
            "available": True}


def _container_ros_packages(container_id: str, deadline: float
                            ) -> list[str] | None:
    """Best-effort `ros2 pkg list` run inside a running container.

    A robot whose ROS stack lives entirely in Docker — nothing installed on
    the host — otherwise gets a package list that reflects the wrong
    environment (see harvest/ros_graph.py's list_packages(), which only ever
    sees the host). Two attempts, mirroring what an operator doing
    `docker exec -it <container> bash` and then `ros2 pkg list` would see:
    a plain shell first (works when the image bakes ROS env vars into its
    Dockerfile ENV — `docker exec` inherits those), then an interactive
    shell (sources ~/.bashrc, where hand-rolled robot images more commonly
    put the `source .../setup.bash` line instead). Never raises; None means
    "couldn't tell" (no Docker, container has no ROS, both attempts failed —
    all indistinguishable from here), not "zero packages".
    """
    for login_shell in (False, True):
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            return None
        lines = _in_container(container_id, "ros2 pkg list",
                              min(CONTAINER_EXEC_TIMEOUT_S, remaining),
                              login_shell)
        packages = sorted(p for p in lines or [] if _ROS_PACKAGE.match(p))
        if packages:
            return packages
    return None


def _container_python_packages(container_id: str, deadline: float
                               ) -> list[dict] | None:
    """The Python distributions the container's ``python3`` sees, with the
    same two shells as the ROS probe (the interactive one adds ROS's own
    Python packages). None when it couldn't be read."""
    best: list[dict] | None = None
    for login_shell in (False, True):
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            break
        lines = _in_container(container_id, f'python3 -c "{_PY_LIST}"',
                              min(CONTAINER_EXEC_TIMEOUT_S, remaining),
                              login_shell)
        try:
            pairs = json.loads(lines[-1]) if lines else None
        except json.JSONDecodeError:
            pairs = None
        if pairs and (best is None or len(pairs) > len(best)):
            best = [{"name": n, "version": v} for n, v in pairs]
    return best


def _image_digests(image_ids: list[str | None]) -> dict[str, str | None]:
    """Image id -> first RepoDigest, for every image in one call with its own
    budget (one call per image used to share what was left of the inspect
    budget, so later containers got no digest just for coming last). An
    image built locally has no RepoDigest: None."""
    ids = sorted({i for i in image_ids if i})
    if not ids:
        return {}
    out = _run(["image", "inspect", "--format",
                "{{.Id}} {{json .RepoDigests}}", *ids],
               timeout=DOCKER_TIMEOUT_S, partial_ok=True)
    if out is None:
        log.warning("could not read image digests")
        return {}
    digests: dict[str, str | None] = {}
    for line in out.splitlines():
        image_id, _, rest = line.partition(" ")
        try:
            found = json.loads(rest)
        except json.JSONDecodeError:
            continue
        digests[image_id] = found[0] if isinstance(found, list) and found \
            else None
    return digests
