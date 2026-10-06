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
import math
import os
import re
import threading
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
REQUEST_TIMEOUT_S = 3.0    # one service call; then it is sent once more
REQUEST_TRIES = 2
LATCHED_WAIT_S = 3.0       # after the parameters, for latched samples still
                           # missing (most arrive during discovery)
TEARDOWN_GRACE_S = 5.0     # the result is already printed; a hung rclpy
                           # shutdown is cut short after this

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
    timeout_s = timeout_s or (DISCOVERY_MAX_S + PARAMS_BUDGET_S
                              + LATCHED_WAIT_S + TEARDOWN_GRACE_S + 15)
    try:
        # Bytes, decoded leniently: one odd byte in a DDS warning on stderr
        # must not cost the whole snapshot.
        proc = subprocess.run(
            [sys.executable, "-m", "ros_fairy.harvest.ros_snapshot",
             *(["--nodes-only"] if nodes_only else [])],
            capture_output=True, timeout=timeout_s, env=child_env())
        stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        # The child prints its result before tearing rclpy down; a hang in
        # the teardown still leaves a complete snapshot on stdout.
        stdout, stderr, code = exc.stdout or b"", exc.stderr or b"", None
    out_text = stdout.decode("utf-8", "replace")
    try:
        result = json.loads(out_text.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        if code is None:
            raise SnapshotError(
                f"ROS snapshot timed out after {timeout_s:.0f}s") from None
        detail = (stderr.decode("utf-8", "replace") or out_text)
        detail = detail.strip()[-800:]
        raise SnapshotError(f"ROS snapshot failed (exit {code}): "
                            f"{detail or 'no output'}") from None
    if result.get("error"):
        raise SnapshotError(result["error"])
    return result


# -- child side -----------------------------------------------------------------

def json_float(x: float) -> float | str:
    """A double as strict JSON allows: NaN and infinities become the strings
    "nan", "inf" and "-inf" (``json.dumps`` would write bare ``NaN``, which
    is not JSON and breaks jq and RO-Crate validators)."""
    if math.isnan(x):
        return "nan"
    if math.isinf(x):
        return "inf" if x > 0 else "-inf"
    return x


def _value(pv) -> Any:
    """rcl_interfaces/ParameterValue -> plain Python (as `ros2 param dump`)."""
    t = pv.type
    if t == 1:
        return pv.bool_value
    if t == 2:
        return pv.integer_value
    if t == 3:
        return json_float(pv.double_value)
    if t == 4:
        return pv.string_value
    if t == 5:
        return [b if isinstance(b, int) else ord(b) for b in pv.byte_array_value]
    if t == 6:
        return list(pv.bool_array_value)
    if t == 7:
        return list(pv.integer_array_value)
    if t == 8:
        return [json_float(x) for x in pv.double_array_value]
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


def merge_transforms(acc: dict[str, dict], transforms: list[dict],
                     conflicts: list[str] | None = None) -> None:
    """Add one /tf_static message's transforms, keyed by child frame as tf2
    keys them: a frame has one parent. A later transform for the same child
    replaces the earlier one (as tf2's buffer would); a *different* parent
    for it is noted in ``conflicts``, since the tree then depends on which
    publisher spoke last."""
    for t in transforms:
        old = acc.get(t["child_frame"])
        if old is not None and old["parent_frame"] != t["parent_frame"] \
                and conflicts is not None:
            conflicts.append(f"{t['child_frame']}: {old['parent_frame']} "
                             f"replaced by {t['parent_frame']}")
        acc[t["child_frame"]] = t


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


def offers_parameters(node, fqn: str) -> bool:
    """Whether the graph shows ``fqn`` serving ``<fqn>/list_parameters``.

    If the graph can't say (lookup error), assume yes and let the call try.
    """
    namespace, _, name = fqn.rpartition("/")
    try:
        services = node.get_service_names_and_types_by_node(
            name, namespace or "/")
    except Exception:
        return True
    return any(srv == f"{fqn}/list_parameters" for srv, _ in services)


def _fqn(name: str, namespace: str) -> str:
    return (namespace.rstrip("/") + "/" + name) if namespace != "/" \
        else "/" + name


def _description_topics(topics: list[tuple[str, list[str]]]
                        ) -> tuple[list[str], list[str]]:
    """Every robot-description and static-TF topic in the graph, namespaced
    ones included (``/jo/robot_description``, a remapped ``/jo/tf_static``)."""
    urdf = sorted(n for n, types in topics
                  if n.endswith("/robot_description")
                  and "std_msgs/msg/String" in types)
    tf = sorted(n for n, types in topics
                if n.endswith("/tf_static") and "tf2_msgs/msg/TFMessage" in types)
    return urdf, tf


def pick_description(urdfs: dict[str, str]) -> tuple[str | None, str | None]:
    """(topic, URDF) to archive: the root ``/robot_description`` if it
    published, else the first namespaced one that did."""
    if "/robot_description" in urdfs:
        return "/robot_description", urdfs["/robot_description"]
    for topic in sorted(urdfs):
        return topic, urdfs[topic]
    return None, None


def snapshot(emit, nodes_only: bool = False) -> None:  # pragma: no cover
    """The capture itself (child side; needs a live ROS environment).

    Calls ``emit(result)`` once the result is complete, *before* rclpy is
    torn down.
    """
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
        emit({"error": f"could not join the ROS graph: {exc}"})
        return

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
    # together aren't dropped. Each publisher is told apart by its GID, so
    # "heard from every publisher" counts publishers, not messages.
    urdfs: dict[str, str] = {}
    transforms: dict[str, dict] = {}
    conflicts: list[str] = []
    tf_heard: dict[str, set] = {}
    subscribed: set[str] = set()

    def gid(info) -> Any:
        g = (info or {}).get("publisher_gid") if isinstance(info, dict) \
            else getattr(info, "publisher_gid", None)
        if isinstance(g, dict):
            g = g.get("data", g)
        try:
            return bytes(g)
        except Exception:
            return repr(g)

    def subscribe_urdf(topic: str) -> None:
        if topic in subscribed:
            return
        subscribed.add(topic)

        def on_urdf(msg, topic=topic):
            urdfs[topic] = msg.data
        node.create_subscription(String, topic, on_urdf, latched(1))

    def subscribe_tf(topic: str) -> None:
        if topic in subscribed:
            return
        subscribed.add(topic)
        tf_heard[topic] = set()

        def on_tf(msg, info, topic=topic):
            merge_transforms(transforms, [transform_to_dict(t)
                                          for t in msg.transforms], conflicts)
            tf_heard[topic].add(gid(info))
        node.create_subscription(TFMessage, topic, on_tf, latched(100))

    # The usual names right away, so latched samples arrive during discovery.
    subscribe_urdf("/robot_description")
    subscribe_tf("/tf_static")

    def spin_for(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            executor.spin_once(timeout_sec=0.05)

    def teardown(clients=()) -> None:
        for cli in clients:
            node.destroy_client(cli)
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown(context=context)

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
    topic_list = node.get_topic_names_and_types()
    out["topics"] = sorted(
        ({"name": n, "type": ",".join(types)} for n, types in topic_list),
        key=lambda t: t["name"])
    out["captured_at"] = datetime.now(timezone.utc).isoformat()
    if nodes_only:
        emit(out)
        teardown()
        return

    # A namespaced robot publishes its description under its namespace.
    urdf_topics, tf_topics = _description_topics(topic_list)
    for topic in urdf_topics:
        subscribe_urdf(topic)
    for topic in tf_topics:
        subscribe_tf(topic)

    # Parameters: list then get, every node concurrently, each call sent once
    # its service has been discovered. A batch get that comes back short is
    # re-asked name by name: rclcpp rejects the *whole* request when any one
    # name is declared without a value (robot_localization does this for
    # every unused sensor slot), which used to lose the node's parameters
    # entirely — with `ros2 param dump` too (2026-10-02).
    #
    # Every node gets a client. Service endpoints are discovered separately
    # from node names and can arrive seconds later on a busy graph, so "no
    # parameter service" is only concluded once SERVICE_WAIT_S has passed and
    # the graph still shows none (it used to be decided at once, silently
    # skipping nodes whose services just hadn't been seen yet).
    waiting: dict[str, Any] = {}       # fqn -> ListParameters client, not sent
    pending: dict[tuple, Any] = {}     # (fqn, stage, name|None) -> future
    sent: dict[tuple, tuple] = {}      # key -> (client, request, time, tries)
    names_of: dict[str, list[str]] = {}
    singles: dict[str, dict[str, Any]] = {}  # fqn -> {name: value}
    queue: dict[str, list[str]] = {}         # fqn -> names still to ask
    unfetched: dict[str, list[str]] = {}     # fqn -> names never answered
    get_cli: dict[str, Any] = {}
    clients = []
    out["no_param_service"] = []
    for fqn in out["nodes"]:
        if TF_LISTENER_NODE.search(fqn):
            continue
        cli = node.create_client(ListParameters, f"{fqn}/list_parameters")
        clients.append(cli)
        waiting[fqn] = cli
    params_start = time.monotonic()
    deadline = params_start + PARAMS_BUDGET_S

    def send(key: tuple, client, request, tries: int = 1) -> None:
        pending[key] = client.call_async(request)
        sent[key] = (client, request, time.monotonic(), tries)

    def finish(fqn: str, values: dict[str, Any]) -> None:
        # Only names that were answered: one that never was is "not
        # captured", recorded in params_partial — not None, which means
        # "declared without a value".
        answered = [n for n in names_of[fqn] if n in values]
        out["parameters"][fqn] = {fqn: {"ros__parameters": nest(
            complete_values(answered, values))}}
        missing = [n for n in names_of[fqn] if n not in values]
        if missing:
            unfetched[fqn] = missing

    def ask_next(fqn: str) -> None:
        nm = queue[fqn].pop(0)
        send((fqn, "one", nm), get_cli[fqn], GetParameters.Request(names=[nm]))

    def handle(key: tuple, result) -> None:
        """A finished call: ``result`` is None when it failed or timed out."""
        fqn, stage, name = key
        if stage == "list":
            if result is None:
                return  # stays in params_missing
            names_of[fqn] = list(result.result.names)
            get_cli[fqn] = node.create_client(GetParameters,
                                              f"{fqn}/get_parameters")
            clients.append(get_cli[fqn])
            send((fqn, "get", None), get_cli[fqn],
                 GetParameters.Request(names=names_of[fqn]))
        elif stage == "get":
            values = [] if result is None else result.values
            if len(values) == len(names_of[fqn]):
                finish(fqn, dict(zip(names_of[fqn], map(_value, values))))
                return
            # One at a time per node: a burst of requests overflows the
            # service's request queue (depth 10) and the rest are silently
            # dropped.
            singles[fqn], queue[fqn] = {}, list(names_of[fqn])
            if queue[fqn]:
                ask_next(fqn)
            else:
                finish(fqn, singles.pop(fqn))
        else:  # "one"
            # An answer with no value is rclcpp refusing a name declared
            # without one: that *is* the answer (unset, None). Only no answer
            # at all leaves the name out, as "not captured".
            if result is not None:
                values = result.values
                singles[fqn][name] = _value(values[0]) if len(values) == 1 \
                    else None
            if queue[fqn]:
                ask_next(fqn)
            else:
                finish(fqn, singles.pop(fqn))

    while (waiting or pending) and time.monotonic() < deadline:
        executor.spin_once(timeout_sec=0.05)
        now = time.monotonic()
        for fqn, cli in list(waiting.items()):
            if cli.service_is_ready():
                send((fqn, "list", None), cli, ListParameters.Request())
                del waiting[fqn]
        if waiting and now - params_start > SERVICE_WAIT_S:
            for fqn in waiting:
                if not offers_parameters(node, fqn):
                    out["no_param_service"].append(fqn)
            waiting.clear()  # advertised but never ready: params_missing
        for key, fut in list(pending.items()):
            client, request, sent_at, tries = sent[key]
            if not fut.done():
                # A node that just died stays in the graph until its lease
                # expires, and a response can be lost: don't let one call
                # hold everything up — resend once, then give up on it.
                if now - sent_at > REQUEST_TIMEOUT_S:
                    client.remove_pending_request(fut)
                    del pending[key]
                    if tries < REQUEST_TRIES:
                        send(key, client, request, tries + 1)
                    else:
                        handle(key, None)
                continue
            del pending[key]
            failed = fut.exception() is not None or fut.result() is None
            handle(key, None if failed else fut.result())
    # Out of time mid-way through a node's one-by-one fetch: keep what came;
    # the names still queued or in flight are recorded as not captured.
    for fqn, values in singles.items():
        finish(fqn, values)
    out["params_partial"] = {fqn: sorted(n) for fqn, n in unfetched.items()}
    out["no_param_service"].sort()
    out["params_missing"] = sorted(
        fqn for fqn in out["nodes"]
        if not TF_LISTENER_NODE.search(fqn) and fqn not in out["parameters"]
        and fqn not in out["no_param_service"])

    # Latched samples usually arrived during the above. Give the stragglers a
    # short wait of their own (the old budget counted from the start and was
    # nearly always spent by now) — until every publisher has been heard.
    def latched_done() -> bool:
        for topic in urdf_topics or ["/robot_description"]:
            if topic not in urdfs and node.count_publishers(topic) > 0:
                return False
        return all(len(tf_heard[t]) >= node.count_publishers(t)
                   for t in tf_heard)

    latched_end = time.monotonic() + LATCHED_WAIT_S
    while not latched_done() and time.monotonic() < latched_end:
        executor.spin_once(timeout_sec=0.05)

    urdf_pubs = sum(node.count_publishers(t) for t in
                    set(urdf_topics) | {"/robot_description"})
    tf_pubs = sum(node.count_publishers(t) for t in tf_heard)
    out["description_publishers"] = {"robot_description": urdf_pubs,
                                     "tf_static": tf_pubs}
    out["robot_description_topic"], out["robot_description"] = \
        pick_description(urdfs)
    # Nobody publishing static transforms is a known answer ([]); publishers
    # that never delivered one is not (None).
    if any(tf_heard.values()):
        out["tf_static"] = list(transforms.values())
    elif tf_pubs == 0:
        out["tf_static"] = []
    out["tf_static_conflicts"] = conflicts

    emit(out)
    teardown(clients)


def main() -> int:  # pragma: no cover - exercised via take() on a robot
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nodes-only", action="store_true",
                        help="stop after discovery (no parameters/latched)")
    args = parser.parse_args()
    emitted = []

    def emit(result: dict[str, Any]) -> None:
        """Print the result now, before rclpy is torn down: a hang in the
        teardown must not lose a finished snapshot."""
        sys.stdout.write(json.dumps(result, allow_nan=False) + "\n")
        sys.stdout.flush()
        emitted.append(True)
        # The parent has what it needs; don't let a stuck DDS shutdown keep
        # this process (and its participant) around.
        timer = threading.Timer(TEARDOWN_GRACE_S, os._exit, args=(0,))
        timer.daemon = True
        timer.start()

    try:
        snapshot(emit, nodes_only=args.nodes_only)
    except ImportError as exc:
        if not emitted:
            emit({"error": f"rclpy is not available: {exc}"})
    except Exception as exc:
        if not emitted:
            emit({"error": f"{type(exc).__name__}: {exc}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
