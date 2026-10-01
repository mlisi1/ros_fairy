"""ROS graph snapshot via ros2 CLI subprocesses only (no rclpy).

Commands used: ros2 node list, ros2 topic list -t, ros2 pkg list,
ros2 param dump <node>. Keeping to subprocess keeps this module portable
across ROS 2 distros.
"""

import logging
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from typing import Any

import yaml

log = logging.getLogger("ros_fairy.harvest.ros_graph")

ROS2_CLI_TIMEOUT_S = 20
PARAM_DUMP_BUDGET_S = 60
# Each `ros2 param dump <node>` call is its own subprocess, which creates its
# own fresh DDS participant (no daemon/pooling for live parameter service
# calls). On a real robot's graph — already dozens of long-lived participants
# — piling on 8 more at once turned out to be enough to exhaust CycloneDDS's
# automatic participant-index range: every dump call then fails to even
# create its node ("Failed to find a free participant index for domain 0"),
# which looks identical to every one of them hanging out the full per-call
# timeout. Keep this low so the harvest's own footprint stays small relative
# to the graph it's inspecting.
PARAM_DUMP_WORKERS = 2

# tf2's TransformListener always spawns a bare node with this auto-generated
# name purely to hold a /tf subscription; it never declares a parameter, so
# `ros2 param dump` has nothing to return for it and — on every field mission
# harvested so far — reliably burns the full per-node timeout finding that
# out. A real robot easily has 8+ of these; dumping them serially wasted most
# of the whole budget before any other node got a turn. Skip them outright.
_TF_LISTENER_NODE = re.compile(r"/transform_listener_impl_[0-9a-f]+$")


class RosGraphError(Exception):
    """ros2 CLI was unreachable or failed."""


def _run(args: list[str], timeout: float = ROS2_CLI_TIMEOUT_S) -> str:
    try:
        result = subprocess.run(
            ["ros2", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RosGraphError("ros2 CLI not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RosGraphError(f"'ros2 {args[0]}' timed out") from exc
    if result.returncode != 0:
        raise RosGraphError(
            f"'ros2 {' '.join(args)}' failed: {result.stderr.strip()}")
    return result.stdout


def list_nodes() -> list[str]:
    return sorted(line.strip() for line in _run(["node", "list"]).splitlines()
                  if line.strip())


def list_topics() -> list[dict[str, str]]:
    """Parse 'ros2 topic list -t' lines of the form '/name [pkg/msg/Type]'."""
    topics = []
    for line in _run(["topic", "list", "-t"]).splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, rest = line.partition(" ")
        topics.append({"name": name, "type": rest.strip().strip("[]")})
    return sorted(topics, key=lambda t: t["name"])


def list_packages() -> list[str]:
    return sorted(line.strip() for line in _run(["pkg", "list"]).splitlines()
                  if line.strip())


def dump_params(node: str, timeout: float = ROS2_CLI_TIMEOUT_S) -> dict:
    parsed = yaml.safe_load(_run(["param", "dump", node], timeout=timeout))
    return parsed if isinstance(parsed, dict) else {}


def harvest() -> dict[str, Any]:
    """Full graph snapshot.

    Raises RosGraphError if the basic listing commands fail (ROS down) or see
    no nodes at all. Individual param dump failures degrade to complete=False
    instead.

    An empty node list is a failure, not an empty graph: the harvest runs
    while a recorder is live, and the recorder is itself a node. The ros2 CLI
    exits 0 with no output when DDS discovery can't reach the robot's
    participants, which used to be archived as a complete, empty capture
    ("ok"), hiding the failure and stopping the retries (2026-10-01).

    Node param dumps run concurrently (bounded pool) against a shared
    wall-clock deadline rather than one-at-a-time against a shared budget: a
    single unresponsive node used to burn its full timeout and starve every
    node sorted after it out of even one attempt, regardless of how quickly
    they would have answered. Running them in parallel means a slow node
    only costs its own slot, not everyone else's turn.
    """
    nodes = list_nodes()
    if not nodes:
        raise RosGraphError("no ROS nodes visible: ROS is not running, or "
                            "DDS discovery cannot reach it from here")
    topics = list_topics()
    packages = list_packages()

    dumpable = [n for n in nodes if not _TF_LISTENER_NODE.search(n)]
    skipped = len(nodes) - len(dumpable)
    if skipped:
        log.debug("skipping %d tf2 transform_listener_impl node(s)", skipped)

    parameters: dict[str, dict] = {}
    complete = True

    # Not a `with` block on purpose: on the timeout path below we need
    # shutdown(wait=False) so returning doesn't block on nodes still stuck
    # in their own subprocess timeout — `Executor.__exit__` always calls
    # shutdown(wait=True), which would undo that.
    pool = ThreadPoolExecutor(max_workers=PARAM_DUMP_WORKERS)
    futures = {pool.submit(dump_params, node): node for node in dumpable}
    try:
        for future in as_completed(futures, timeout=PARAM_DUMP_BUDGET_S):
            node = futures[future]
            try:
                parameters[node] = future.result()
            except RosGraphError as exc:
                complete = False
                log.debug("param dump failed for %s: %s", node, exc)
        pool.shutdown(wait=True)
    except FuturesTimeoutError:
        complete = False
        still_running = [n for f, n in futures.items() if not f.done()]
        log.debug("param dump budget (%ds) exhausted with %d node(s) "
                  "still outstanding: %s", PARAM_DUMP_BUDGET_S,
                  len(still_running), ", ".join(still_running))
        pool.shutdown(wait=False, cancel_futures=True)

    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "nodes": nodes,
        "topics": topics,
        "ros_packages": packages,
        "parameters": parameters,
        "complete": complete,
    }
