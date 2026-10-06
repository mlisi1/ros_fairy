"""ROS graph snapshot: nodes, topics, parameters, URDF and static transforms.

The live part is taken through a single DDS participant by
:mod:`ros_fairy.harvest.ros_snapshot`; only ``ros2 pkg list`` (which reads
the local install and never touches DDS) is still a CLI subprocess.
"""

import logging
import subprocess
from typing import Any

from ros_fairy.harvest import ros_snapshot

log = logging.getLogger("ros_fairy.harvest.ros_graph")

ROS2_CLI_TIMEOUT_S = 20


class RosGraphError(Exception):
    """ROS was unreachable, or the graph could not be captured."""


def _run(args: list[str], timeout: float = ROS2_CLI_TIMEOUT_S) -> str:
    try:
        result = subprocess.run(
            ["ros2", *args], capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise RosGraphError("ros2 CLI not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RosGraphError(f"'ros2 {args[0]}' timed out") from exc
    if result.returncode != 0:
        raise RosGraphError(
            f"'ros2 {' '.join(args)}' failed: {result.stderr.strip()}")
    return result.stdout


def list_packages() -> list[str]:
    return sorted(line.strip() for line in _run(["pkg", "list"]).splitlines()
                  if line.strip())


def _take(nodes_only: bool) -> dict[str, Any]:
    try:
        snap = ros_snapshot.take(nodes_only=nodes_only)
    except ros_snapshot.SnapshotError as exc:
        raise RosGraphError(str(exc)) from exc
    # The harvest runs while a recorder is live, and the recorder is itself a
    # node: an empty graph means discovery failed, not that nothing runs.
    if not snap.get("nodes"):
        raise RosGraphError("no ROS nodes visible: ROS is not running, or "
                            "DDS discovery cannot reach it from here")
    return snap


def list_nodes() -> list[str]:
    """Visible nodes only (no parameters) — for quick reachability checks."""
    return _take(nodes_only=True)["nodes"]


def harvest() -> dict[str, Any]:
    """Full graph snapshot, including ``robot_description``/``tf_static``.

    Raises RosGraphError when no snapshot could be taken or it saw no nodes.
    Nodes whose parameters couldn't be fetched degrade to complete=False.
    """
    snap = _take(nodes_only=False)
    no_service = snap.get("no_param_service") or []
    if no_service:
        log.info("%d node(s) offer no parameter service (nothing to "
                 "capture): %s", len(no_service), ", ".join(no_service))
    missing = snap.get("params_missing") or []
    if missing:
        log.info("parameters not captured for %d of %d node(s): %s",
                 len(missing), len(missing) + len(snap["parameters"]),
                 ", ".join(missing))
    partial = snap.get("params_partial") or {}
    for fqn, names in sorted(partial.items()):
        log.info("%d parameter(s) of %s did not answer in time: %s",
                 len(names), fqn, ", ".join(names[:10])
                 + (" …" if len(names) > 10 else ""))
    for conflict in snap.get("tf_static_conflicts") or []:
        log.warning("two static transforms give one frame different "
                    "parents (%s); kept the later one", conflict)
    topic = snap.get("robot_description_topic")
    if topic and topic != "/robot_description":
        log.info("robot description read from %s", topic)
    try:
        packages = list_packages()
    except RosGraphError as exc:
        log.warning("listing installed ROS packages failed: %s", exc)
        packages = None
    return {
        "captured_at": snap.get("captured_at"),
        "nodes": snap["nodes"],
        "topics": snap.get("topics", []),
        "ros_packages": packages,
        "parameters": snap.get("parameters", {}),
        # Names a node listed but never returned a value for: "not
        # captured", so the diff doesn't report them as removed.
        "parameters_not_captured": partial,
        "complete": not missing and not partial,
        "robot_description": snap.get("robot_description"),
        "tf_static": snap.get("tf_static"),
        "description_publishers": snap.get("description_publishers"),
    }
