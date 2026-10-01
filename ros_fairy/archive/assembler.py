"""Assemble the RO-Crate mission archive.

Failure-safe staging algorithm: everything is built under
archive/.staging/<name>/ and committed with a single atomic rename. At every
instant each bag exists in exactly one place, and the final archive directory
either doesn't exist or is complete.
"""

import errno
import json
import logging
import re
import shutil
import unicodedata
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ros_fairy.archive import index, ro_crate
from ros_fairy.manifest.schema import FOREIGN_SOURCES, MissionRecord
from ros_fairy.utils import fsio, paths

log = logging.getLogger("ros_fairy.archive.assembler")


class AssemblyError(Exception):
    """Plain-language, user-facing assembly failure."""


def sanitise(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text).encode(
        "ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "unknown"


def archive_name(record: MissionRecord) -> str:
    # Date *and* time to the second (colons aren't filesystem-safe, so HH-MM-SS).
    stamp = record.identity.created_at.astimezone().strftime("%Y-%m-%d_%H-%M-%S")
    base = (f"{stamp}_{sanitise(record.intent.location_name)}"
            f"_{sanitise(record.identity.operator_name)}")
    name, n = base, 1
    while (paths.archive_dir() / name).exists():
        n += 1
        name = f"{base}_{n}"
    return name


def _bag_file_hashes(bag_dir: Path) -> dict[str, str]:
    """Bag-relative file path -> sha256 for every file in the bag directory."""
    return {
        f.relative_to(bag_dir).as_posix(): fsio.sha256_file(f)
        for f in sorted(bag_dir.rglob("*")) if f.is_file()
    }


def _move_bag(src: Path, dest: Path,
              progress: Callable[[str], None] | None) -> None:
    if progress:
        progress(f"Saving recording {src.name}")
    try:
        src.rename(dest)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        # archive mounted on another filesystem: copy, verify, delete
        shutil.copytree(src, dest)
        if fsio.dir_size_bytes(dest) != fsio.dir_size_bytes(src):
            shutil.rmtree(dest, ignore_errors=True)
            raise AssemblyError(
                "Copying a recording didn't complete correctly — the "
                "original data is untouched in the spool.") from exc
        shutil.rmtree(src)


def _render_readme(record: MissionRecord, warnings: list[str]) -> str:
    total_s = sum(b.duration_s or 0 for b in record.bags)
    total_bytes = sum(b.size_bytes for b in record.bags)
    lines = [
        f"# {record.intent.goal}",
        "",
        f"- **Mission ID:** {record.identity.mission_id}",
        f"- **Date:** {record.identity.created_at.isoformat()}",
        f"- **Operator:** {record.identity.operator_name}",
        f"- **Location:** {record.intent.location_name}",
    ]
    if record.intent.environment:
        lines.append(f"- **Environment:** {record.intent.environment}")
    if record.robot:
        lines.append(f"- **Robot:** {record.robot.name} "
                     f"({record.robot.platform})")
    length = (f"{total_s / 60:.0f} minutes"
              if any(b.duration_s for b in record.bags) else "length unknown")
    lines += [
        f"- **Recordings:** {len(record.bags)}, "
        f"{length}, {total_bytes / 1e9:.1f} GB",
    ]
    if record.intent.notes:
        lines += ["", f"**Notes:** {record.intent.notes}"]
    if record.hardware_devices:
        devices = record.hardware_devices
        with_serial = [d for d in devices if d.serial_number]
        named = []
        for d in devices:
            label = d.product_name or d.vendor_name
            if label and label not in named:
                named.append(label)
        lines += ["", "## Connected hardware", "",
                  f"- {len(devices)} device(s) were detected when the mission "
                  "started; the full list is in mission_record.json."]
        if named:
            shown = ", ".join(named[:8])
            more = f", and {len(named) - 8} more" if len(named) > 8 else ""
            lines += [f"- Recognised devices: {shown}{more}."]
        if with_serial:
            lines += [f"- {len(with_serial)} of these record a serial number. "
                      "**Serial numbers can identify a specific physical unit** "
                      "— consider this before sharing the archive."]
    all_warnings = warnings + [w.plain_text for b in record.bags
                               for w in b.health_warnings]
    if all_warnings:
        lines += ["", "## Warnings", ""]
        lines += [f"- {w}" for w in all_warnings]
    lines += ["", "Packaged by ros-fairy "
              f"{record.provenance.ros_fairy_version} as an RO-Crate. "
              "See ro-crate-metadata.json and mission_record.json.", ""]
    return "\n".join(lines)


def find_interrupted_staging() -> Path | None:
    """An archive left in staging by a crash after bags were moved."""
    staging_root = paths.staging_dir()
    if not staging_root.is_dir():
        return None
    candidates = sorted(p for p in staging_root.iterdir() if p.is_dir())
    return candidates[0] if candidates else None


def find_incomplete_crates() -> list[Path]:
    """Archive folders without a mission_record.json: saves cut off mid-way.

    They are neither listed nor indexed (reindex keys on mission_record.json),
    so without this they sit in the archive unnoticed.
    """
    root = paths.archive_dir()
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir()
                  if p.is_dir() and not p.name.startswith(".")
                  and not (p / "mission_record.json").is_file())


def _commit(staging: Path, final: Path) -> None:
    """Durably rename a finished staging tree into the archive.

    Everything is flushed first, so a power cut either leaves the staging copy
    (resumable) or a complete crate — never a renamed tree of empty files.
    """
    fsio.fsync_tree(staging)
    staging.rename(final)
    try:  # past the commit point: a flush failure must not report a failure
        fsio.fsync_dir(final.parent)
        fsio.fsync_dir(staging.parent)
    except OSError:
        pass


def resume_commit(staging: Path) -> Path:
    """Commit a previously interrupted staging directory."""
    final = paths.archive_dir() / staging.name
    if final.exists():
        raise AssemblyError(
            f"A mission named {staging.name} already exists in the archive; "
            f"the interrupted copy is still in {staging}.")
    _commit(staging, final)
    record_file = final / "mission_record.json"
    if record_file.is_file():
        record = MissionRecord.model_validate(
            json.loads(record_file.read_text()))
        index.insert(record, final)
    return final


def assemble(record: MissionRecord, harvest_doc: dict[str, Any],
             progress: Callable[[str], None] | None = None) -> Path:
    """Build and commit the mission archive. Returns the final path.

    Raises AssemblyError with a plain-language message; on failure before the
    commit point the spool is left (or put back) exactly as it was.
    """
    name = archive_name(record)
    staging_root = paths.staging_dir()
    # Group-writable like every other ros-fairy dir: a staging root created by
    # one account (e.g. root) must not lock out the next operator's save.
    staging_root.mkdir(parents=True, exist_ok=True)
    try:
        staging_root.chmod(0o2775)
    except OSError:
        pass
    staging = staging_root / name
    if staging.exists():
        shutil.rmtree(staging)
    final = paths.archive_dir() / name

    # Foreign recordings are referenced where they were made; drop any whose
    # source vanished since detection so the crate never references a missing
    # bag (the loss is reported to the operator via harvest_level_warnings).
    record.bags = [b for b in record.bags
                   if b.source not in FOREIGN_SOURCES or Path(b.path).is_dir()]

    spool_bags = [Path(b.path) for b in record.bags]
    moved: list[tuple[Path, Path]] = []
    try:
        # Steps 1-3: small artifacts + manifests into staging
        (staging / "bags").mkdir(parents=True)
        if progress:
            progress("Collecting mission context")

        harvest_dir = staging / "harvest"
        harvest_dir.mkdir()
        extra_files = [{"id": "harvest/harvest.json",
                        "name": "Raw harvest data",
                        "encodingFormat": "application/json"}]
        fsio.atomic_write_json(harvest_dir / "harvest.json", harvest_doc)
        raw_py = harvest_doc.get("raw_python_env") or {}
        pip_freeze = raw_py.get("pip_freeze")
        if pip_freeze:
            (harvest_dir / "pip_freeze.txt").write_text(pip_freeze,
                                                        encoding="utf-8")
            extra_files.append({"id": "harvest/pip_freeze.txt",
                                 "name": "Python package freeze",
                                 "encodingFormat": "text/plain"})

        raw_hw = harvest_doc.get("raw_hardware") or {}
        lsusb_v = raw_hw.get("lsusb_verbose")
        if lsusb_v:
            (harvest_dir / "lsusb_verbose.txt").write_text(lsusb_v,
                                                           encoding="utf-8")
            extra_files.append({"id": "harvest/lsusb_verbose.txt",
                                 "name": "USB device descriptors",
                                 "encodingFormat": "text/plain"})
        dmesg_usb = raw_hw.get("dmesg_usb")
        if dmesg_usb:
            (harvest_dir / "dmesg_usb.txt").write_text(dmesg_usb,
                                                        encoding="utf-8")
            extra_files.append({"id": "harvest/dmesg_usb.txt",
                                 "name": "Kernel hardware messages",
                                 "encodingFormat": "text/plain"})

        if record.ros_graph.robot_description:
            (harvest_dir / "robot_description.urdf").write_text(
                record.ros_graph.robot_description)
            record.ros_graph.robot_description = \
                "harvest/robot_description.urdf"
            extra_files.append({"id": "harvest/robot_description.urdf",
                                "name": "Robot description (URDF)",
                                "encodingFormat": "application/xml"})
        if record.ros_graph.tf_static is not None:
            fsio.atomic_write_json(harvest_dir / "tf_static.json",
                                   record.ros_graph.tf_static)
            extra_files.append({"id": "harvest/tf_static.json",
                                "name": "Static transforms",
                                "encodingFormat": "application/json"})

        for cal in record.calibrations:
            source = Path(cal.source_path)
            if not source.is_file():
                continue
            cal_dir = staging / "calibrations"
            cal_dir.mkdir(exist_ok=True)
            dest = cal_dir / source.name
            shutil.copy2(source, dest)
            cal.archived_path = f"calibrations/{source.name}"
            cal.sha256 = fsio.sha256_file(dest)

        raw_inspect = harvest_doc.get("raw_docker_inspect") or []
        if raw_inspect:
            docker_dir = staging / "docker"
            docker_dir.mkdir()
            fsio.atomic_write_json(docker_dir / "containers.json",
                                   raw_inspect)
            extra_files.append({"id": "docker/containers.json",
                                "name": "Container inventory",
                                "encodingFormat": "application/json"})
            seen = set()
            for container in record.software.docker_containers:
                compose, project = container.compose_file, \
                    container.compose_project
                if not compose or not project or (project, compose) in seen:
                    continue
                seen.add((project, compose))
                compose_path = Path(compose)
                if not compose_path.is_file():
                    continue
                dest_dir = docker_dir / "compose" / sanitise(project)
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(compose_path, dest_dir / compose_path.name)
                extra_files.append({
                    "id": f"docker/compose/{sanitise(project)}/"
                          f"{compose_path.name}",
                    "name": f"Compose file ({project})",
                    "encodingFormat": "application/yaml"})

        # Step 3a: copy foreign recordings into staging *before* the manifests
        # are written. Copying is non-destructive (the operator's original is
        # left in place), so it is safe to do this early — and a foreign bag
        # that vanished since the pre-assembly check is dropped here with a
        # warning rather than aborting the whole save (issue #35). The manifests
        # below then describe exactly the bags that made it into the crate.
        surviving_bags, surviving_spool = [], []
        for bag, spool_bag in zip(record.bags, spool_bags, strict=True):
            if bag.source in FOREIGN_SOURCES:
                if progress:
                    progress(f"Copying recording {spool_bag.name}")
                try:
                    shutil.copytree(spool_bag, staging / "bags" / spool_bag.name)
                except OSError as exc:
                    log.warning("foreign recording %s vanished during assembly "
                                "(%s); dropping it from the mission",
                                spool_bag, exc)
                    continue
            surviving_bags.append(bag)
            surviving_spool.append(spool_bag)
        record.bags, spool_bags = surviving_bags, surviving_spool

        # Crate-relative bag paths + per-file checksums (spool bags are moved
        # verbatim and foreign bags are now copied into staging, so hashing the
        # source pins the archived bytes) + assembly provenance, then manifests.
        for bag, spool_bag in zip(record.bags, spool_bags, strict=True):
            bag.file_sha256 = _bag_file_hashes(spool_bag)
            bag.path = f"bags/{spool_bag.name}"
        record.provenance.assembled_at = datetime.now(timezone.utc)

        from ros_fairy.manifest import builder
        warnings = builder.harvest_level_warnings(harvest_doc)
        (staging / "README.md").write_text(_render_readme(record, warnings))
        fsio.atomic_write_json(
            staging / "mission_record.json",
            record.model_dump(mode="json"))
        ro_crate.write(record, staging, extra_files,
                       license_url=harvest_doc.get("default_license"))
    except AssemblyError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        if exc.errno == errno.ENOSPC:
            raise AssemblyError("There isn't enough disk space to save this "
                                "mission. Nothing was changed.") from exc
        raise AssemblyError(f"Saving failed ({exc.strerror or exc}). "
                            "Nothing was changed.") from exc

    # Step 4: move spool bags — the only step touching spool data. Foreign
    # recordings were already copied into staging in step 3a (their original
    # belongs to the operator), so they are skipped here.
    try:
        for bag, src in zip(record.bags, spool_bags, strict=True):
            if bag.source in FOREIGN_SOURCES:
                continue
            dest = staging / "bags" / src.name
            _move_bag(src, dest, progress)
            moved.append((src, dest))
    except (OSError, AssemblyError) as exc:
        for src, dest in reversed(moved):
            if dest.exists() and not src.exists():
                dest.rename(src)
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(exc, AssemblyError):
            raise
        raise AssemblyError("Saving the recordings failed "
                            f"({getattr(exc, 'strerror', exc) or exc}). "
                            "Your data is back in the spool, unchanged."
                            ) from exc

    # Step 5: commit point
    try:
        _commit(staging, final)
    except OSError as exc:
        raise AssemblyError(
            f"Saving was interrupted; your data is safe in {staging}. "
            "Run mission_close again to finish saving.") from exc

    # Step 6: index (failure here must not undo the archive)
    try:
        index.insert(record, final)
    except Exception:
        pass  # reindex() can rebuild; the crate on disk is the truth

    # Step 7: clear spool context files
    for leftover in (paths.harvest_json_path(),
                     paths.mission_context_path(),
                     paths.session_env_path()):
        try:
            leftover.unlink(missing_ok=True)
        except OSError:
            pass
    # Make the clearing durable too: a spool that survives a power cut hands
    # this mission's harvest and bag list to the next one.
    try:
        fsio.fsync_dir(paths.spool_dir())
    except OSError:
        pass
    return final
