"""Mission diff display (ros2 fairy diff).

Compares two MissionRecord objects section by section, printing only what
actually changed. Sections with no differences are silently omitted.
"""

import difflib
import re
from pathlib import Path

from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from ros_fairy.manifest.schema import MissionRecord
from ros_fairy.ui.review import human_size
from ros_fairy.utils.topic_health import humanize_duration

# ROS-internal nodes get a random-id suffix baked into their name (tf2's
# TransformListener is the classic case: /transform_listener_impl_565e5a3d…),
# so every mission has a different set purely by chance — they're noise in a
# diff, not a real change. Match anything ending in a long hex run.
_RANDOM_ID_NODE = re.compile(r"_[0-9a-f]{8,}$", re.IGNORECASE)


def _mission_label(r: MissionRecord) -> str:
    dt = r.identity.created_at.astimezone().strftime("%Y-%m-%d %H:%M")
    goal = r.intent.goal if len(r.intent.goal) <= 50 else r.intent.goal[:49] + "…"
    return f"{dt}  {goal}  —  {r.intent.location_name}  ({r.identity.operator_name})"


def _table(rows: list[tuple[str, str, str]]) -> Table:
    """Three-column grid: label | A value | B value.

    Convention for the value columns:
      a="", b="..."  → item added in B (green)
      a="...", b=""  → item removed in B (dim + "(removed)")
      both non-empty → value changed (dim old, bold new)
    """
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", min_width=26)
    t.add_column(min_width=22)
    t.add_column()
    for label, a, b in rows:
        if not a and b:
            t.add_row(label, "", Text(b, style="green"))
        elif a and not b:
            t.add_row(label, Text(a, style="dim"), Text("(removed)", style="dim"))
        else:
            t.add_row(label, Text(a, style="dim"), Text(b, style="bold"))
    return t


# ── per-section diff helpers ──────────────────────────────────────────────────

def _diff_context(a: MissionRecord, b: MissionRecord) -> list[tuple]:
    rows = []
    for label, va, vb in [
        ("Goal",        a.intent.goal,              b.intent.goal),
        ("Location",    a.intent.location_name,     b.intent.location_name),
        ("Operator",    a.identity.operator_name,   b.identity.operator_name),
        ("Environment", a.intent.environment or "", b.intent.environment or ""),
        ("Notes",       a.intent.notes or "",       b.intent.notes or ""),
    ]:
        if va != vb:
            rows.append((label, va or "(none)", vb or "(none)"))
    return rows


def _diff_software(a: MissionRecord, b: MissionRecord) -> list[tuple]:
    rows = []
    if a.software.ros_distro != b.software.ros_distro:
        rows.append(("ROS distro",
                     a.software.ros_distro or "(unknown)",
                     b.software.ros_distro or "(unknown)"))

    # None means the deb list wasn't captured: say so rather than listing
    # every package as removed.
    apt_a, apt_b = a.software.apt_ros_versions, b.software.apt_ros_versions
    if apt_a is not None and apt_b is not None:
        for pkg in sorted(set(apt_a) | set(apt_b)):
            va, vb = apt_a.get(pkg), apt_b.get(pkg)
            if va != vb:
                rows.append((pkg, va or "", vb or ""))
    elif (apt_a is None) != (apt_b is None):
        rows.append(("ROS debs captured", "no" if apt_a is None else "yes",
                     "no" if apt_b is None else "yes"))

    # None (or [] in older records — a ROS host is never empty) means the
    # package list wasn't captured; diffing it would list every package as
    # removed (2026-10-01).
    pkgs_a, pkgs_b = a.software.ros_packages, b.software.ros_packages
    if pkgs_a and pkgs_b:
        for pkg in sorted(set(pkgs_a) - set(pkgs_b)):
            rows.append((f"host pkg {pkg}", "installed", ""))
        for pkg in sorted(set(pkgs_b) - set(pkgs_a)):
            rows.append((f"host pkg {pkg}", "", "installed"))
    elif bool(pkgs_a) != bool(pkgs_b):
        rows.append(_captured_row("host packages captured", pkgs_a, pkgs_b))

    ca = {c.name: c for c in a.software.docker_containers}
    cb = {c.name: c for c in b.software.docker_containers}
    for name in sorted(set(ca) | set(cb)):
        ia = (ca[name].digest or ca[name].image) if name in ca else None
        ib = (cb[name].digest or cb[name].image) if name in cb else None
        if ia != ib:
            def _short(s): return s[:48] + "…" if s and len(s) > 48 else (s or "")
            rows.append((f"container {name}", _short(ia), _short(ib)))

        # ros_packages is None when the probe never ran (container down, no
        # ROS found, or — for old records — the harvest predates package
        # capture entirely). That's "unknown", not "empty": diffing it
        # against a populated list would report every package as newly
        # installed, which is just a gap in one snapshot, not a real change.
        ra = ca[name].ros_packages if name in ca else None
        rb = cb[name].ros_packages if name in cb else None
        if ra is not None and rb is not None:
            pa, pb = set(ra), set(rb)
            for pkg in sorted(pa - pb):
                rows.append((f"{name}: pkg {pkg}", "installed", ""))
            for pkg in sorted(pb - pa):
                rows.append((f"{name}: pkg {pkg}", "", "installed"))
        elif (ra is None) != (rb is None):
            rows.append((f"{name}: packages captured",
                         "no" if ra is None else "yes",
                         "no" if rb is None else "yes"))

    return rows


def _diff_sensors(a: MissionRecord, b: MissionRecord) -> list[tuple]:
    rows = []
    sa = {s.sensor_id: s for s in a.sensors}
    sb = {s.sensor_id: s for s in b.sensors}
    for sid in sorted(set(sa) | set(sb)):
        s_a, s_b = sa.get(sid), sb.get(sid)
        if s_a is None:
            assert s_b is not None  # sid came from sb's keys
            rows.append((s_b.make_model, "",
                         "✓ detected" if s_b.detected_at_start else "configured"))
        elif s_b is None:
            rows.append((s_a.make_model,
                         "✓ detected" if s_a.detected_at_start else "configured", ""))
        elif s_a.detected_at_start != s_b.detected_at_start:
            rows.append((s_a.make_model,
                         "✓ detected" if s_a.detected_at_start else "✗ not detected",
                         "✓ detected" if s_b.detected_at_start else "✗ not detected"))
    return rows


def _captured_row(label: str, a, b) -> tuple:
    return (label, "yes" if a else "no", "yes" if b else "no")


def _diff_graph(a: MissionRecord, b: MissionRecord) -> list[tuple]:
    rows: list[tuple] = []

    nodes_a = {n for n in a.ros_graph.nodes if not _RANDOM_ID_NODE.search(n)}
    nodes_b = {n for n in b.ros_graph.nodes if not _RANDOM_ID_NODE.search(n)}
    # No nodes means the graph wasn't captured (a live mission always has at
    # least its recorder) — not that every node was removed or added.
    if not nodes_a or not nodes_b:
        if not nodes_a and not nodes_b:
            return []  # nothing to compare; show_diff says so
        return [_captured_row("ROS graph captured", nodes_a, nodes_b)]
    for n in sorted(nodes_a - nodes_b):
        rows.append((n, "running", ""))
    for n in sorted(nodes_b - nodes_a):
        rows.append((n, "", "running"))

    topics_a = {t.name for t in a.ros_graph.topics}
    topics_b = {t.name for t in b.ros_graph.topics}
    for t in sorted(topics_a - topics_b):
        rows.append((t, "published", ""))
    for t in sorted(topics_b - topics_a):
        rows.append((t, "", "published"))

    return rows


def _load_urdf_text(record: MissionRecord, crate: Path | None) -> str | None:
    """The actual URDF text for a mission.

    Once archived, ``ros_graph.robot_description`` is rewritten from the raw
    XML to a crate-relative path ("harvest/robot_description.urdf") — see
    assembler.assemble — so comparing the field itself would just compare two
    identical path strings, hiding every real change. Read the file from the
    crate when one is given; fall back to the field as-is for records that
    were never archived (e.g. built directly in tests).
    """
    value = record.ros_graph.robot_description
    if not value:
        return None
    if crate is not None:
        candidate = crate / value
        if candidate.is_file():
            try:
                return candidate.read_text()
            except OSError:
                return None
    return value


def _diff_urdf(a: MissionRecord, b: MissionRecord,
               crate_a: Path | None, crate_b: Path | None) -> list[tuple]:
    text_a = _load_urdf_text(a, crate_a)
    text_b = _load_urdf_text(b, crate_b)
    if text_a == text_b:
        return []
    if text_a is None or text_b is None:
        return [("Robot description (URDF)",
                 "(none)" if text_a is None else "present",
                 "(none)" if text_b is None else "present")]

    rows: list[tuple] = []
    sm = difflib.SequenceMatcher(a=text_a.splitlines(), b=text_b.splitlines(),
                                 autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        old_lines, new_lines = text_a.splitlines()[i1:i2], \
            text_b.splitlines()[j1:j2]
        for k in range(max(len(old_lines), len(new_lines))):
            old = old_lines[k].strip() if k < len(old_lines) else ""
            new = new_lines[k].strip() if k < len(new_lines) else ""
            rows.append((f"URDF line {i1 + k + 1}", old, new))
    return rows


def _diff_tf_static(a: MissionRecord, b: MissionRecord) -> list[tuple]:
    tf_a, tf_b = a.ros_graph.tf_static, b.ros_graph.tf_static
    # None means the /tf_static capture never got a reply (ros_descriptions
    # harvest failed/timed out) — distinct from an empty list, which means it
    # was reached and genuinely saw zero static transforms. Treating None as
    # [] would report every transform on the other side as "newly added"
    # when really this side just has no data to compare.
    if tf_a is None or tf_b is None:
        if tf_a == tf_b:
            return []
        return [("Static transforms captured",
                 "no" if tf_a is None else "yes",
                 "no" if tf_b is None else "yes")]

    by_a = {(tf.get("parent_frame"), tf.get("child_frame")): tf for tf in tf_a}
    by_b = {(tf.get("parent_frame"), tf.get("child_frame")): tf for tf in tf_b}

    rows: list[tuple] = []
    for key in sorted(set(by_a) - set(by_b)):
        rows.append((f"tf {key[0]} → {key[1]}", "present", ""))
    for key in sorted(set(by_b) - set(by_a)):
        rows.append((f"tf {key[0]} → {key[1]}", "", "present"))
    for key in sorted(set(by_a) & set(by_b)):
        leaves_a = dict(_leaves("", by_a[key]))
        leaves_b = dict(_leaves("", by_b[key]))
        for leaf in sorted(set(leaves_a) | set(leaves_b)):
            if leaf in ("parent_frame", "child_frame"):
                continue
            va, vb = leaves_a.get(leaf), leaves_b.get(leaf)
            if va != vb:
                rows.append((f"tf {key[0]} → {key[1]}: {leaf}",
                             str(va) if leaf in leaves_a else "",
                             str(vb) if leaf in leaves_b else ""))
    return rows


def _flatten_params(parameters: dict[str, dict]) -> dict[str, dict]:
    """node -> {param_name: value}, unwrapping the `ros2 param dump` envelope.

    Raw shape is {node: {node: {"ros__parameters": {name: value}}}} — the
    inner node key just mirrors the outer one (that's the YAML `ros2 param
    dump <node>` emits).
    """
    flat: dict[str, dict] = {}
    for node, doc in parameters.items():
        if _RANDOM_ID_NODE.search(node) or not isinstance(doc, dict):
            continue
        inner = doc.get(node, doc)
        params = inner.get("ros__parameters", {}) if isinstance(inner, dict) else {}
        flat[node] = params if isinstance(params, dict) else {}
    return flat


def _leaves(prefix: str, value) -> list[tuple[str, object]]:
    """Recurse through nested parameter namespaces to (dotted.path, value)
    pairs — a nav2-style plugin param is a whole nested dict, and diffing it
    as one opaque value dumps the entire thing the moment any one field
    inside it changes."""
    if isinstance(value, dict):
        out = []
        for key, sub in value.items():
            out.extend(_leaves(f"{prefix}.{key}" if prefix else key, sub))
        return out
    return [(prefix, value)]


def _param_text(value) -> str:
    """A declared parameter with no value is captured as None."""
    return "(not set)" if value is None else str(value)


def _diff_parameters(a: MissionRecord, b: MissionRecord) -> list[tuple]:
    rows: list[tuple] = []
    flat_a = _flatten_params(a.ros_graph.parameters)
    flat_b = _flatten_params(b.ros_graph.parameters)
    # Nothing captured on a side (graph harvest failed) is not "no changes".
    if not flat_a or not flat_b:
        if not flat_a and not flat_b:
            return []  # nothing to compare; show_diff says so
        return [_captured_row("parameters captured", flat_a, flat_b)]
    for node in sorted(set(flat_a) & set(flat_b)):
        leaves_a = dict(_leaves("", flat_a[node]))
        leaves_b = dict(_leaves("", flat_b[node]))
        # A name one capture never got a value for is unknown there, not
        # removed: compare it only where both sides have it.
        unknown = set(a.ros_graph.parameters_not_captured.get(node, [])) | \
            set(b.ros_graph.parameters_not_captured.get(node, []))
        for key in sorted(set(leaves_a) | set(leaves_b)):
            if key in unknown and (key not in leaves_a or key not in leaves_b):
                continue
            va, vb = leaves_a.get(key), leaves_b.get(key)
            if va != vb:
                rows.append((f"{node}: {key}",
                             _param_text(va) if key in leaves_a else "",
                             _param_text(vb) if key in leaves_b else ""))

    # A node missing from ros_graph.parameters isn't necessarily unchanged —
    # `ros2 param dump` may simply have timed out for it that run
    # (ros_graph.complete=False tracks this at the mission level; a whole
    # mission can come back with nothing captured at all, as happened here).
    # Flag the gap instead of silently treating "no data" as "no change" —
    # but only for nodes that existed in both missions' graphs, since a
    # brand-new/removed node's capture asymmetry is already explained by the
    # ROS graph section.
    nodes_a = {n for n in a.ros_graph.nodes if not _RANDOM_ID_NODE.search(n)}
    nodes_b = {n for n in b.ros_graph.nodes if not _RANDOM_ID_NODE.search(n)}
    persisting = nodes_a & nodes_b
    for node in sorted((set(flat_a) ^ set(flat_b)) & persisting):
        rows.append((f"{node}: parameters captured",
                     "yes" if node in flat_a else "no",
                     "yes" if node in flat_b else "no"))
    return rows


def _notes(a: MissionRecord, b: MissionRecord, has_changes: bool) -> dict:
    """Display-only notes for sections that would otherwise be silently
    omitted, so "identical" and "never captured" don't look the same. Not
    changes, so diff_as_dict leaves them out."""
    notes = {}
    if not a.ros_graph.nodes and not b.ros_graph.nodes:
        notes["ROS graph"] = "not captured in either mission"
    flat_a = _flatten_params(a.ros_graph.parameters)
    flat_b = _flatten_params(b.ros_graph.parameters)
    if not flat_a and not flat_b:
        notes["Parameters"] = "not captured in either mission"
    elif has_changes and set(flat_a) & set(flat_b):
        shared = len(set(flat_a) & set(flat_b))
        notes["Parameters"] = (f"no changes across {shared} shared "
                               f"node{'s' if shared != 1 else ''}")
    return notes


def _diff_recordings(a: MissionRecord, b: MissionRecord) -> list[tuple]:
    rows: list[tuple] = []

    dur_a = sum(bag.duration_s or 0 for bag in a.bags)
    dur_b = sum(bag.duration_s or 0 for bag in b.bags)
    if abs(dur_a - dur_b) > 1:
        rows.append(("Duration", humanize_duration(dur_a), humanize_duration(dur_b)))

    size_a = sum(bag.size_bytes for bag in a.bags)
    size_b = sum(bag.size_bytes for bag in b.bags)
    if size_a != size_b:
        rows.append(("Size", human_size(size_a), human_size(size_b)))

    total_a = sum(len(bag.health_warnings) for bag in a.bags)
    total_b = sum(len(bag.health_warnings) for bag in b.bags)
    if total_a != total_b:
        rows.append(("Warnings",
                     str(total_a) if total_a else "none",
                     str(total_b) if total_b else "none"))

    return rows


def _usb_key(dev) -> tuple:
    """The same physical device across missions: vendor, product, and its
    serial (or, without one, the port it sits in)."""
    return (dev.vendor_id, dev.product_id, dev.serial or dev.port_path
            or dev.sysfs_name)


def _usb_label(dev) -> str:
    name = dev.product or f"{dev.vendor_id}:{dev.product_id}"
    return f"USB {name}" + (f" ({dev.serial})" if dev.serial else "")


def _usb_settings(dev) -> dict[str, str]:
    out = {"port": dev.port_path or dev.sysfs_name,
           "speed (Mb/s)": dev.speed_mbps, "driver": dev.driver}
    for key in ("control", "autosuspend_delay_ms"):
        out[f"power/{key}"] = dev.power.get(key)
    for tty in dev.serial_ports:
        out[f"{tty.tty} latency_timer"] = tty.latency_timer_ms
    off = sorted(r.rule for r in dev.udev_rules_applied
                 if r.in_effect is False)
    out["rules not in effect"] = ", ".join(off) or None
    return {k: v for k, v in out.items() if v is not None}


def _diff_usb_udev(a: MissionRecord, b: MissionRecord) -> list[tuple]:
    """The robot's udev rules and how its USB devices were managed. Not
    captured on a side (older record, failed probe) is said, not diffed."""
    rows: list[tuple] = []
    ra, rb = a.udev_rules, b.udev_rules
    if ra is not None and rb is not None:
        ca = {r.path: r for r in ra.custom}
        cb = {r.path: r for r in rb.custom}
        for path in sorted(set(ca) | set(cb)):
            if path not in cb:
                rows.append((f"udev rule {path}", "present", ""))
            elif path not in ca:
                rows.append((f"udev rule {path}", "", "present"))
            elif ca[path].sha256 != cb[path].sha256:
                rows.append((f"udev rule {path}", ca[path].sha256[:12],
                             cb[path].sha256[:12] + " (changed)"))
    elif (ra is None) != (rb is None):
        rows.append(_captured_row("udev rules captured", ra, rb))

    ua, ub = a.usb, b.usb
    if ua is None or ub is None:
        if (ua is None) != (ub is None):
            rows.append(_captured_row("USB details captured", ua, ub))
        return rows
    for key in sorted(set(ua.usbcore) | set(ub.usbcore)):
        va, vb = ua.usbcore.get(key), ub.usbcore.get(key)
        if va != vb:
            rows.append((f"usbcore {key}", va or "", vb or ""))
    for module in sorted(set(ua.driver_parameters) | set(ub.driver_parameters)):
        pa = ua.driver_parameters.get(module, {})
        pb = ub.driver_parameters.get(module, {})
        if not pa or not pb:
            continue  # driver not loaded in one mission: no device using it
        for key in sorted(set(pa) | set(pb)):
            if pa.get(key) != pb.get(key):
                rows.append((f"{module} {key}", pa.get(key, ""),
                             pb.get(key, "")))
    da = {_usb_key(d): d for d in ua.devices}
    db = {_usb_key(d): d for d in ub.devices}
    for key in sorted(set(da) | set(db), key=lambda k: tuple(map(str, k))):
        if key not in db:
            rows.append((_usb_label(da[key]), "connected", ""))
            continue
        if key not in da:
            rows.append((_usb_label(db[key]), "", "connected"))
            continue
        sa, sb = _usb_settings(da[key]), _usb_settings(db[key])
        for setting in sorted(set(sa) | set(sb)):
            if sa.get(setting) != sb.get(setting):
                rows.append((f"{_usb_label(da[key])}: {setting}",
                             sa.get(setting, ""), sb.get(setting, "")))
    return rows


# ── public entry point ────────────────────────────────────────────────────────

def show_diff(a: MissionRecord, b: MissionRecord,
              console: Console | None = None,
              crate_a: Path | None = None, crate_b: Path | None = None) -> None:
    console = console or Console()

    header = Table.grid(padding=(0, 2))
    header.add_column(style="bold cyan", width=2)
    header.add_column()
    header.add_row("A", _mission_label(a))
    header.add_row("B", _mission_label(b))

    sections = [
        ("Mission context",       _diff_context(a, b)),
        ("Software",              _diff_software(a, b)),
        ("Sensors",               _diff_sensors(a, b)),
        ("ROS graph",             _diff_graph(a, b)),
        ("Parameters",            _diff_parameters(a, b)),
        ("Robot description",     _diff_urdf(a, b, crate_a, crate_b)),
        ("Static transforms",     _diff_tf_static(a, b)),
        ("USB and udev",          _diff_usb_udev(a, b)),
        ("Recordings",            _diff_recordings(a, b)),
    ]
    rows_by_title = dict(sections)
    for title, note in _notes(a, b, any(rows for _, rows in sections)).items():
        if not rows_by_title[title]:
            rows_by_title[title] = [(note, "", "")]
    changed = [(title, rows_by_title[title]) for title, _ in sections
               if rows_by_title[title]]

    if not changed:
        body = Group(header, Text(""),
                     Text("No differences found.", style="dim"))
    else:
        parts: list = [header]
        for title, rows in changed:
            parts.append(Text(""))
            parts.append(Rule(title, style="dim"))
            parts.append(_table(rows))
        body = Group(*parts)

    console.print(Panel(body, title="Mission diff", border_style="cyan"))


def _mission_summary(r: MissionRecord) -> dict:
    return {
        "mission_id": r.identity.mission_id,
        "created_at": r.identity.created_at.isoformat(),
        "operator": r.identity.operator_name,
        "goal": r.intent.goal,
        "location": r.intent.location_name,
    }


def diff_as_dict(a: MissionRecord, b: MissionRecord,
                 crate_a: Path | None = None,
                 crate_b: Path | None = None) -> dict:
    """Machine-readable form of the same diff show_diff() renders.

    `changes` holds only sections that differ; each change is the section's
    label plus the before/after values (as displayed: empty `a` = added in B,
    empty `b` = removed in B).
    """
    sections = {
        "mission_context":  _diff_context(a, b),
        "software":         _diff_software(a, b),
        "sensors":          _diff_sensors(a, b),
        "ros_graph":        _diff_graph(a, b),
        "parameters":       _diff_parameters(a, b),
        "robot_description": _diff_urdf(a, b, crate_a, crate_b),
        "tf_static":        _diff_tf_static(a, b),
        "usb_udev":         _diff_usb_udev(a, b),
        "recordings":       _diff_recordings(a, b),
    }
    changes = {
        name: [{"label": label, "a": va, "b": vb} for label, va, vb in rows]
        for name, rows in sections.items() if rows
    }
    return {
        "mission_a": _mission_summary(a),
        "mission_b": _mission_summary(b),
        "changes": changes,
    }
