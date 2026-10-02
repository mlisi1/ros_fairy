"""The whole live-ROS capture through one DDS participant.

Lists nodes and topics, fetches every node's parameters (concurrent
``list_parameters``/``get_parameters`` service calls) and reads the latched
``/robot_description`` and ``/tf_static``, all from a single rclpy node.

It replaced one ``ros2 param dump`` subprocess per node plus a second rclpy
participant: each of those claimed a CycloneDDS participant slot, and on a
robot already near Cyclone's per-host limit (32 with ROS 2's default
discovery settings) the harvest failed outright and could starve the robot's
own nodes of slots (2026-10-01: 5 of 8 field missions lost their graph).

The participant also opts out of slot numbering (``ParticipantIndex=none``),
for this process only: it is found by multicast instead, so ros-fairy works
however full the robot is, without anyone changing the robot's DDS config.

Runs as a short-lived child process (``python -m ros_fairy.harvest.
ros_snapshot``) printing JSON: a hang can be killed, and no rclpy state lives
in the long-running watchdog.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any

# Appended to whatever CYCLONEDDS_URI the environment has; later config wins.
# Inert for other RMWs.
CYCLONE_NO_INDEX = "<Discovery><ParticipantIndex>none</ParticipantIndex>" \
                   "</Discovery>"

DISCOVERY_MIN_S = 2.0      # always wait at least this long for discovery
DISCOVERY_SETTLE_S = 1.0   # ...then until the node list is stable this long
DISCOVERY_MAX_S = 8.0
PARAMS_BUDGET_S = 20.0     # all parameter calls, concurrently
SERVICE_WAIT_S = 5.0       # a node whose parameter service never appears
                           # (e.g. started without one) is given up on
LATCHED_BUDGET_S = 10.0    # /robot_description + /tf_static, from start

# tf2's TransformListener spawns bare nodes with this generated name that
# never declare parameters; asking them only burns the budget.
TF_LISTENER_NODE = re.compile(r"/transform_listener_impl_[0-9a-f]+$")

SELF_NAME = "ros_fairy_harvest"


class SnapshotError(Exception):
    """The snapshot could not be taken at all (no ROS, no participant...)."""


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    uri = env.get("CYCLONEDDS_URI", "").strip()
    env["CYCLONEDDS_URI"] = f"{uri},{CYCLONE_NO_INDEX}" if uri \
        else CYCLONE_NO_INDEX
    return env


def take(timeout_s: float | None = None,
         nodes_only: bool = False) -> dict[str, Any]:
    """Run the snapshot in a child process and return its result.

    Raises SnapshotError when no snapshot could be taken; a partial one
    (some parameters or latched topics missing) is returned normally.
    """
    timeout_s = timeout_s or (DISCOVERY_MAX_S + PARAMS_BUDGET_S + 20)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "ros_fairy.harvest.ros_snapshot",
             *(["--nodes-only"] if nodes_only else [])],
            capture_output=True, text=True, timeout=timeout_s,
            env=child_env())
    except subprocess.TimeoutExpired as exc:
        raise SnapshotError(f"ROS snapshot timed out after {timeout_s:.0f}s") \
            from exc
    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        detail = (proc.stderr or proc.stdout).strip()[-800:]
        raise SnapshotError(f"ROS snapshot failed (exit {proc.returncode}): "
                            f"{detail or 'no output'}") from None
    if result.get("error"):
        raise SnapshotError(result["error"])
    return result


# -- child side -----------------------------------------------------------------

def _value(pv) -> Any:
    """rcl_interfaces/ParameterValue -> plain Python (as `ros2 param dump`)."""
    t = pv.type
    if t == 1:
        return pv.bool_value
    if t == 2:
        return pv.integer_value
    if t == 3:
        return pv.double_value
    if t == 4:
        return pv.string_value
    if t == 5:
        return [b if isinstance(b, int) else ord(b) for b in pv.byte_array_value]
    if t == 6:
        return list(pv.bool_array_value)
    if t == 7:
        return list(pv.integer_array_value)
    if t == 8:
        return list(pv.double_array_value)
    if t == 9:
        return list(pv.string_array_value)
    return None  # PARAMETER_NOT_SET


def complete_values(names: list[str], values: dict[str, Any]) -> dict[str, Any]:
    """Every listed name, with None for one that has no value (declared but
    never set) — so "unset" is recorded, not mistaken for "absent"."""
    return {name: values.get(name) for name in names}


def nest(flat: dict[str, Any]) -> dict[str, Any]:
    """{"a.b": 1} -> {"a": {"b": 1}}, the shape `ros2 param dump` writes."""
    out: dict[str, Any] = {}
    for name in sorted(flat):
        parts = name.split(".")
        cur = out
        for part in parts[:-1]:
            nxt = cur.setdefault(part, {})
            if not isinstance(nxt, dict):  # "a" and "a.b" both set
                nxt = cur[part] = {"": nxt}
            cur = nxt
        cur[parts[-1]] = flat[name]
    return out


def merge_transforms(acc: dict[tuple[str, str], dict],
                     transforms: list[dict]) -> None:
    """Add one /tf_static message's transforms; a later message for the same
    parent→child replaces the earlier one (as tf2's buffer would)."""
    for t in transforms:
        acc[(t["parent_frame"], t["child_frame"])] = t


def transform_to_dict(t) -> dict[str, Any]:
    tr, rot = t.transform.translation, t.transform.rotation
    return {
        "parent_frame": t.header.frame_id,
        "child_frame": t.child_frame_id,
        "translation": {"x": tr.x, "y": tr.y, "z": tr.z},
        "rotation": {"x": rot.x, "y": rot.y, "z": rot.z, "w": rot.w},
    }


def is_hidden(fqn: str) -> bool:
    """Hidden nodes (a name segment starting with "_", e.g. ros2cli's own
    `/_ros2cli_<pid>`) are tooling; `ros2 node list` leaves them out too."""
    return any(part.startswith("_") for part in fqn.split("/") if part)


def _fqn(name: str, namespace: str) -> str:
    return (namespace.rstrip("/") + "/" + name) if namespace != "/" \
        else "/" + name


def snapshot(nodes_only: bool = False) -> dict[str, Any]:  # pragma: no cover
    """The capture itself (child side; needs a live ROS environment)."""
    import rclpy
    from rcl_interfaces.srv import GetParameters, ListParameters
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import (DurabilityPolicy, HistoryPolicy, QoSProfile,
                           ReliabilityPolicy)
    from std_msgs.msg import String
    from tf2_msgs.msg import TFMessage

    started = time.monotonic()
    out: dict[str, Any] = {"error": None, "robot_description": None,
                           "tf_static": None, "parameters": {}}
    context = rclpy.Context()
    try:
        rclpy.init(context=context)
        node = rclpy.create_node(SELF_NAME, context=context,
                                 start_parameter_services=False,
                                 enable_rosout=False)
    except Exception as exc:
        try:
            rclpy.shutdown(context=context)
        except Exception:
            pass
        return {"error": f"could not join the ROS graph: {exc}"}

    executor = SingleThreadedExecutor(context=context)
    executor.add_node(node)
    def latched(depth: int) -> QoSProfile:
        return QoSProfile(depth=depth, history=HistoryPolicy.KEEP_LAST,
                          reliability=ReliabilityPolicy.RELIABLE,
                          durability=DurabilityPolicy.TRANSIENT_LOCAL)

    # /tf_static has one latched message per publisher (robot_state_publisher,
    # each camera driver...). They are merged, not overwritten — keeping only
    # the last one made every mission capture a different publisher's frames
    # (2026-10-02) — and the queue is tf2's own depth, so samples arriving
    # together aren't dropped.
    transforms: dict[tuple[str, str], dict] = {}
    tf_messages = [0]

    def on_urdf(msg):
        out["robot_description"] = msg.data

    def on_tf(msg):
        merge_transforms(transforms, [transform_to_dict(t)
                                      for t in msg.transforms])
        tf_messages[0] += 1

    node.create_subscription(String, "/robot_description", on_urdf,
                             latched(1))
    node.create_subscription(TFMessage, "/tf_static", on_tf, latched(100))

    def spin_for(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            executor.spin_once(timeout_sec=0.05)

    # Discovery: wait until the node list stops growing.
    spin_for(DISCOVERY_MIN_S)
    names: set[str] = set()
    stable_since = time.monotonic()
    while time.monotonic() - started < DISCOVERY_MAX_S:
        current = {fqn for fqn in (_fqn(n, ns) for n, ns in
                                   node.get_node_names_and_namespaces())
                   if fqn != "/" + SELF_NAME and not is_hidden(fqn)}
        if current != names:
            names, stable_since = current, time.monotonic()
        elif time.monotonic() - stable_since >= DISCOVERY_SETTLE_S:
            break
        spin_for(0.2)
    out["nodes"] = sorted(names)
    out["topics"] = sorted(
        ({"name": n, "type": ",".join(types)}
         for n, types in node.get_topic_names_and_types()),
        key=lambda t: t["name"])
    out["captured_at"] = datetime.now(timezone.utc).isoformat()
    if nodes_only:
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown(context=context)
        return out

    # Parameters: list then get, every node concurrently, each call sent once
    # its service has been discovered. A batch get that comes back short is
    # re-asked name by name: rclcpp rejects the *whole* request when any one
    # name is declared without a value (robot_localization does this for
    # every unused sensor slot), which used to lose the node's parameters
    # entirely — with `ros2 param dump` too (2026-10-02).
    waiting: dict[str, Any] = {}       # fqn -> ListParameters client, not sent
    pending: dict[tuple, Any] = {}     # (fqn, stage, name|None) -> future
    names_of: dict[str, list[str]] = {}
    singles: dict[str, dict[str, Any]] = {}  # fqn -> {name: value}
    queue: dict[str, list[str]] = {}         # fqn -> names still to ask
    get_cli: dict[str, Any] = {}
    clients = []
    for fqn in out["nodes"]:
        if TF_LISTENER_NODE.search(fqn):
            continue
        cli = node.create_client(ListParameters, f"{fqn}/list_parameters")
        clients.append(cli)
        waiting[fqn] = cli
    params_start = time.monotonic()
    deadline = params_start + PARAMS_BUDGET_S

    def finish(fqn: str, values: dict[str, Any]) -> None:
        out["parameters"][fqn] = {fqn: {"ros__parameters": nest(
            complete_values(names_of[fqn], values))}}

    def ask_next(fqn: str) -> None:
        nm = queue[fqn].pop(0)
        pending[(fqn, "one", nm)] = get_cli[fqn].call_async(
            GetParameters.Request(names=[nm]))

    while (waiting or pending) and time.monotonic() < deadline:
        executor.spin_once(timeout_sec=0.05)
        for fqn, cli in list(waiting.items()):
            if cli.service_is_ready():
                pending[(fqn, "list", None)] = cli.call_async(
                    ListParameters.Request())
                del waiting[fqn]
        if time.monotonic() - params_start > SERVICE_WAIT_S:
            waiting.clear()  # never offered a parameter service
        for key, fut in list(pending.items()):
            if not fut.done():
                continue
            del pending[key]
            fqn, stage, name = key
            failed = fut.exception() is not None or fut.result() is None
            if stage == "list":
                if failed:
                    continue
                names_of[fqn] = list(fut.result().result.names)
                get_cli[fqn] = node.create_client(GetParameters,
                                                  f"{fqn}/get_parameters")
                clients.append(get_cli[fqn])
                pending[(fqn, "get", None)] = get_cli[fqn].call_async(
                    GetParameters.Request(names=names_of[fqn]))
            elif stage == "get":
                values = [] if failed else fut.result().values
                if len(values) == len(names_of[fqn]):
                    finish(fqn, dict(zip(names_of[fqn], map(_value, values))))
                    continue
                # One at a time per node: a burst of requests overflows the
                # service's request queue (depth 10) and the rest are
                # silently dropped.
                singles[fqn], queue[fqn] = {}, list(names_of[fqn])
                ask_next(fqn)
            else:  # "one"
                values = [] if failed else fut.result().values
                if len(values) == 1:
                    singles[fqn][name] = _value(values[0])
                if queue[fqn]:
                    ask_next(fqn)
                else:
                    finish(fqn, singles.pop(fqn))
    # Out of time mid-way through a node's one-by-one fetch: keep what came.
    for fqn, values in singles.items():
        finish(fqn, values)
    out["params_missing"] = sorted(
        fqn for fqn in out["nodes"]
        if not TF_LISTENER_NODE.search(fqn) and fqn not in out["parameters"])

    # Latched topics usually arrived during the above; give them the rest of
    # their budget — until every /tf_static publisher has been heard from.
    def latched_done() -> bool:
        urdf_ok = out["robot_description"] is not None or \
            node.count_publishers("/robot_description") == 0
        return urdf_ok and \
            tf_messages[0] >= node.count_publishers("/tf_static")

    while not latched_done() and time.monotonic() - started < LATCHED_BUDGET_S:
        executor.spin_once(timeout_sec=0.05)
    out["tf_static"] = list(transforms.values()) if tf_messages[0] else None

    for cli in clients:
        node.destroy_client(cli)
    executor.shutdown()
    node.destroy_node()
    rclpy.shutdown(context=context)
    return out


def main() -> int:  # pragma: no cover - exercised via take() on a robot
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nodes-only", action="store_true",
                        help="stop after discovery (no parameters/latched)")
    args = parser.parse_args()
    try:
        result = snapshot(nodes_only=args.nodes_only)
    except ImportError as exc:
        result = {"error": f"rclpy is not available: {exc}"}
    except Exception as exc:
        result = {"error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
