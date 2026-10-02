"""Assemble the RO-Crate mission archive.

Failure-safe staging algorithm: everything is built under
archive/.staging/<name>/ and committed with a single atomic rename. At every
instant each bag exists in exactly one place (or, mid cross-device copy, in
its original place plus an unverified copy), and the final archive directory
either doesn't exist or is complete.

Every save keeps a small plan file next to its staging tree
(``.staging/<name>.plan.json``) recording how far it got:

- ``building``  — only copies are in staging; throwing it away loses nothing.
- ``moving``    — spool bags are being moved into staging; each move is listed
                  with its source, so an interrupted save can be finished.
- ``committed`` — the crate is in the archive; only the index entry and the
                  spool clean-up may still be pending.

The plan is removed last, after the spool is cleared, so a crash or Ctrl-C at
any point leaves something ``pending_saves()`` can finish or discard safely.
All saves run under ``save_lock()``: one save at a time per robot.
"""

import contextlib
import errno
import fcntl
import json
import logging
import os
import re
import shutil
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ros_fairy.archive import index, ro_crate
from ros_fairy.manifest.schema import FOREIGN_SOURCES, MissionRecord
from ros_fairy.utils import fsio, paths

log = logging.getLogger("ros_fairy.archive.assembler")

PLAN_SUFFIX = ".plan.json"
BUILDING, MOVING, COMMITTED = "building", "moving", "committed"
# Head-room kept free on the archive disk on top of the recordings themselves
# (manifests, README, filesystem overhead).
SPACE_MARGIN_BYTES = 64 * 1024 * 1024


class AssemblyError(Exception):
    """Plain-language, user-facing assembly failure."""


class SaveInProgressError(AssemblyError):
    """Another mission_close holds the save lock."""


def sanitise(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text).encode(
        "ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "unknown"


def _name_taken(name: str) -> bool:
    staging_root = paths.staging_dir()
    return ((paths.archive_dir() / name).exists()
            or (staging_root / name).exists()
            or (staging_root / f"{name}{PLAN_SUFFIX}").exists())


def archive_name(record: MissionRecord) -> str:
    # Date *and* time to the second (colons aren't filesystem-safe, so HH-MM-SS).
    stamp = record.identity.created_at.astimezone().strftime("%Y-%m-%d_%H-%M-%S")
    base = (f"{stamp}_{sanitise(record.intent.location_name)}"
            f"_{sanitise(record.identity.operator_name)}")
    # A name held by an interrupted save in staging is taken too: reusing it
    # would mean deleting that save's tree, which may hold moved recordings.
    name, n = base, 1
    while _name_taken(name):
        n += 1
        name = f"{base}_{n}"
    return name


def _bag_file_hashes(bag_dir: Path) -> dict[str, str]:
    """Bag-relative file path -> sha256 for every file in the bag directory."""
    return {
        f.relative_to(bag_dir).as_posix(): fsio.sha256_file(f)
        for f in sorted(bag_dir.rglob("*")) if f.is_file()
    }


def _free_name(directory: Path, name: str) -> str:
    """``name``, or ``<stem>_2<suffix>``, ... if a file of that name exists."""
    if not (directory / name).exists():
        return name
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 2
    while (directory / f"{stem}_{n}{suffix}").exists():
        n += 1
    return f"{stem}_{n}{suffix}"


def _fsync_dir_quietly(path: Path) -> None:
    try:
        fsio.fsync_dir(path)
    except OSError:
        pass


def _move_bag(src: Path, dest: Path,
              progress: Callable[[str], None] | None,
              expected: dict[str, str] | None = None,
              on_copied: Callable[[], None] | None = None) -> None:
    """Move a spool bag into staging.

    Across filesystems this is copy → flush → verify → ``on_copied`` → delete:
    the original is only deleted once a verified copy is on disk, and
    ``on_copied`` lets the caller record that fact first (a crash during the
    delete then leaves a known-good copy, not an ambiguous pair).
    """
    if progress:
        progress(f"Saving recording {src.name}")
    try:
        src.rename(dest)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    # archive mounted on another filesystem: copy, flush, verify, delete
    shutil.copytree(src, dest)
    fsio.fsync_tree(dest)
    good = (_bag_file_hashes(dest) == expected) if expected is not None \
        else fsio.dir_size_bytes(dest) == fsio.dir_size_bytes(src)
    if not good:
        shutil.rmtree(dest, ignore_errors=True)
        raise AssemblyError(
            "Copying a recording didn't complete correctly — the "
            "original data is untouched in the spool.")
    if on_copied:
        on_copied()
    shutil.rmtree(src)


def _move_back(src: Path, dest: Path) -> None:
    """Undo ``_move_bag``: put a complete staged bag back where it came from."""
    if src.exists():  # remains of an interrupted delete; dest is verified
        shutil.rmtree(src)
    try:
        dest.rename(src)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.copytree(dest, src)
        fsio.fsync_tree(src)
        shutil.rmtree(dest)
    _fsync_dir_quietly(src.parent)


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


def unique_names(names: list[str]) -> list[str]:
    """``names`` with repeats suffixed _2, _3... The first of each keeps its
    name, and a suffix never takes a name another folder really has."""
    real = set(names)
    used: set[str] = set()
    out = []
    for name in names:
        candidate, n = name, 1
        while candidate in used or (candidate != name and candidate in real):
            n += 1
            candidate = f"{name}_{n}"
        used.add(candidate)
        out.append(candidate)
    return out


# -- locking, plans, spool ---------------------------------------------------

def _ensure_staging_root() -> Path:
    staging_root = paths.staging_dir()
    # Group-writable like every other ros-fairy dir: a staging root created by
    # one account (e.g. root) must not lock out the next operator's save.
    staging_root.mkdir(parents=True, exist_ok=True)
    try:
        staging_root.chmod(0o2775)
    except OSError:
        pass
    return staging_root


@contextlib.contextmanager
def save_lock() -> Iterator[None]:
    """Hold the robot-wide save lock, or raise SaveInProgressError.

    Two saves at once would compute the same archive name and race on the
    same staging tree and spool.
    """
    try:
        lock_path = _ensure_staging_root() / ".lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o664)
    except PermissionError as exc:
        raise AssemblyError(
            f"I don't have permission to write to the mission archive "
            f"({paths.archive_dir()}). Ask your engineer to add your account "
            "to the 'ros-fairy' group (sudo usermod -aG ros-fairy <your "
            "username>), then log out and back in.") from exc
    try:
        try:
            os.fchmod(fd, 0o664)
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SaveInProgressError(
                "Someone else is saving or reviewing a mission on this robot "
                "right now (mission_close is already running). Wait for it "
                "to finish, then run this again.") from None
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _plan_path(name: str) -> Path:
    return paths.staging_dir() / f"{name}{PLAN_SUFFIX}"


def _write_plan(plan: dict) -> None:
    fsio.atomic_write_json(_plan_path(plan["name"]), plan)
    _fsync_dir_quietly(paths.staging_dir())


def _read_plan(name: str) -> dict | None:
    try:
        return json.loads(_plan_path(name).read_text())
    except (OSError, ValueError):
        return None


def _drop_plan(name: str) -> None:
    try:
        _plan_path(name).unlink(missing_ok=True)
    except OSError:
        return
    _fsync_dir_quietly(paths.staging_dir())


def _discard_staging(plan: dict) -> None:
    """Throw away a staging tree that holds only copies.

    The plan goes back to ``building`` first, so if the delete fails half-way
    the leftover is still recognised as safe-to-discard next time.
    """
    name = plan["name"]
    staging = paths.staging_dir() / name
    plan["state"] = BUILDING
    try:
        _write_plan(plan)
    except OSError:
        pass
    shutil.rmtree(staging, ignore_errors=True)
    if not staging.exists():
        _drop_plan(name)


def _harvest_bag_paths(harvest: dict | None) -> set[str]:
    return {b.get("path") for b in (harvest or {}).get("bags", [])
            if b.get("path")}


def _clear_spool(mission_id: str | None, saved_paths: set[str]) -> None:
    """Remove a saved mission's context from the spool, durably.

    Only when the spool still belongs to that mission: an operator who briefed
    a new mission since must not lose it. Bags recorded after the save began
    (still listed in harvest.json but not in ``saved_paths``) keep their
    records, so the next mission_close can save them.
    """
    from ros_fairy.manifest import builder
    harvest, context = builder.load_spool()
    spool_id = ((context or {}).get("identity") or {}).get("mission_id")
    if mission_id and spool_id and spool_id != mission_id:
        log.warning("spool now holds mission %s, not the saved %s; leaving "
                    "it alone", spool_id, mission_id)
        return
    leftovers = [paths.mission_context_path(), paths.session_env_path(),
                 paths.watchdog_log_path()]
    unsaved = [b for b in (harvest or {}).get("bags", [])
               if b.get("path") not in saved_paths]
    if harvest is not None and unsaved:
        log.info("%d recording(s) in the spool were not part of the saved "
                 "mission; keeping them for the next save", len(unsaved))
        try:
            fsio.atomic_write_json(paths.harvest_json_path(),
                                   {**harvest, "bags": unsaved})
        except OSError:
            log.warning("could not rewrite harvest.json", exc_info=True)
    else:
        leftovers.append(paths.harvest_json_path())
    for leftover in leftovers:
        try:
            leftover.unlink(missing_ok=True)
        except OSError:
            log.warning("could not remove %s", leftover, exc_info=True)
    # Make the clearing durable too: a spool that survives a power cut hands
    # this mission's harvest and bag list to the next one.
    _fsync_dir_quietly(paths.spool_dir())


def _crate_bag_sources(crate: Path) -> set[str]:
    """Original bag paths of a saved crate, from its archived harvest."""
    try:
        doc = json.loads((crate / "harvest" / "harvest.json").read_text())
    except (OSError, ValueError):
        return set()
    return _harvest_bag_paths(doc)


def _make_group_writable(root: Path) -> None:
    """Let every operator in the ros-fairy group delete or repair the crate.

    Only what this account owns can be changed; the rest is left as is.
    """
    uid = os.geteuid()
    for dirpath, _, filenames in os.walk(root):
        for p, bits in [(Path(dirpath), 0o2070)] + \
                [(Path(dirpath) / f, 0o060) for f in filenames]:
            try:
                st = p.lstat()
                if st.st_uid == uid and not p.is_symlink():
                    p.chmod((st.st_mode & 0o7777) | bits)
            except OSError:
                pass


def _commit(staging: Path, final: Path) -> None:
    """Durably rename a finished staging tree into the archive.

    Everything is flushed first, so a power cut either leaves the staging copy
    (resumable) or a complete crate — never a renamed tree of empty files.
    """
    fsio.fsync_tree(staging)
    staging.rename(final)
    # past the commit point: a flush failure must not report a failure
    _fsync_dir_quietly(final.parent)
    _fsync_dir_quietly(staging.parent)


def _post_commit(final: Path, plan: dict, record: MissionRecord | None,
                 warn: Callable[[str], None]) -> None:
    """Index the committed crate, clear the spool, then drop the plan."""
    try:
        if record is None:
            record = MissionRecord.model_validate(
                json.loads((final / "mission_record.json").read_text()))
        index.insert(record, final)
    except Exception as exc:
        log.warning("adding %s to the index failed", final, exc_info=True)
        warn("The mission is saved, but it couldn't be added to the mission "
             f"list ({exc}). Run `ros2 fairy reindex` to bring the list up "
             "to date.")
    saved = set(plan.get("bag_sources") or []) or _crate_bag_sources(final)
    _clear_spool(plan.get("mission_id"), saved)
    _drop_plan(plan["name"])


def _commit_and_finish(plan: dict, record: MissionRecord | None,
                       warn: Callable[[str], None]) -> Path:
    staging = paths.staging_dir() / plan["name"]
    final = paths.archive_dir() / plan["name"]
    if final.exists():
        raise AssemblyError(
            f"A mission named {final.name} already exists in the archive; "
            f"the interrupted copy is still in {staging}.")
    _make_group_writable(staging)
    try:
        _commit(staging, final)
    except OSError as exc:
        raise AssemblyError(
            f"Saving was interrupted; your data is safe in {staging}. "
            "Run mission_close again to finish saving.") from exc
    plan["state"] = COMMITTED
    try:
        _write_plan(plan)
        _post_commit(final, plan, record, warn)
    except KeyboardInterrupt:
        # The crate is saved; the plan left behind lets the next
        # mission_close finish the tidy-up.
        log.warning("interrupted after committing %s", final)
    except OSError:
        log.warning("tidy-up after committing %s failed", final,
                    exc_info=True)
    return final


# -- interrupted saves -------------------------------------------------------

RESUME, TIDY, DISCARD, STUCK = "resume", "tidy", "discard", "stuck"


@dataclass
class PendingSave:
    """A save left unfinished by a crash, power cut or Ctrl-C."""
    name: str
    kind: str          # RESUME | TIDY | DISCARD | STUCK
    goal: str | None = None
    reason: str | None = None


def _staged_record(staging: Path) -> MissionRecord | None:
    try:
        return MissionRecord.model_validate(
            json.loads((staging / "mission_record.json").read_text()))
    except Exception:
        return None


def _missing_staged_bags(staging: Path, record: MissionRecord) -> list[str]:
    return [b.path for b in record.bags if not (staging / b.path).is_dir()]


def pending_saves() -> list[PendingSave]:
    """Every unfinished save, oldest name first."""
    staging_root = paths.staging_dir()
    if not staging_root.is_dir():
        return []
    names = set()
    for p in staging_root.iterdir():
        if p.name.startswith("."):
            continue
        if p.name.endswith(PLAN_SUFFIX):
            names.add(p.name[:-len(PLAN_SUFFIX)])
        elif p.is_dir():
            names.add(p.name)
    out = []
    for name in sorted(names):
        staging = staging_root / name
        final = paths.archive_dir() / name
        plan = _read_plan(name)
        record = _staged_record(staging) if staging.is_dir() else None
        goal = record.intent.goal if record else None
        if plan is None and _plan_path(name).exists():
            out.append(PendingSave(name, STUCK, reason="its progress file "
                                   "is unreadable"))
        elif plan is None:
            # Left by a version without plan files. Spool bags were only
            # moved after mission_record.json was written, so without one
            # staging holds nothing but copies.
            if record is None:
                out.append(PendingSave(name, DISCARD))
            elif _missing_staged_bags(staging, record):
                out.append(PendingSave(
                    name, STUCK, goal, reason="some of its recordings are "
                    "not in it, and this older save didn't note where they "
                    "came from"))
            else:
                out.append(PendingSave(name, RESUME, goal))
        elif plan.get("state") == BUILDING:
            out.append(PendingSave(name, DISCARD))
        elif staging.is_dir():
            out.append(PendingSave(name, RESUME, goal))
        elif (final / "mission_record.json").is_file():
            out.append(PendingSave(name, TIDY))
        else:
            out.append(PendingSave(name, STUCK, reason="its folder has "
                                   "disappeared"))
    return out


def discard_pending(pending: PendingSave) -> None:
    """Remove a DISCARD save: only copies, the originals are untouched."""
    plan = _read_plan(pending.name) or {"name": pending.name}
    _discard_staging(plan)


def finish_pending(pending: PendingSave,
                   progress: Callable[[str], None] | None = None,
                   warn: Callable[[str], None] | None = None) -> Path:
    """Finish a RESUME or TIDY save. Returns the crate path.

    Raises AssemblyError (staging left untouched) when it can't be finished.
    """
    warn = warn or (lambda msg: None)
    name = pending.name
    staging = paths.staging_dir() / name
    final = paths.archive_dir() / name
    plan = _read_plan(name)
    if pending.kind == TIDY:
        assert plan is not None
        _post_commit(final, plan, None, warn)
        return final
    if pending.kind != RESUME:
        raise AssemblyError(f"The interrupted save in {staging} can't be "
                            f"finished: {pending.reason}.")

    record = _staged_record(staging)
    if record is None:
        raise AssemblyError(
            f"The interrupted save in {staging} is missing its mission "
            "description, so it can't be finished. Nothing was changed; "
            "ask your robot engineer to look at it.")
    if plan is None:  # older save without a plan, verified complete
        plan = {"name": name, "mission_id": record.identity.mission_id,
                "state": MOVING, "bag_sources": [], "moves": []}
    expected = {b.path: b.file_sha256 for b in record.bags}
    for mv in plan.get("moves", []):
        src, dest = Path(mv["src"]), staging / mv["dest"]
        if mv.get("copied"):  # verified copy; finish deleting the original
            if src.exists():
                shutil.rmtree(src)
            continue
        if dest.exists() and not src.exists():
            continue  # the rename went through
        if not src.exists():
            raise AssemblyError(
                f"A recording that belongs to this save ({src.name}) is "
                f"missing. Nothing was changed; the save is still in "
                f"{staging}. Ask your robot engineer to look at it.")
        if dest.exists():  # an unverified cross-device copy: start over
            shutil.rmtree(dest)

        def copied(mv=mv):
            mv["copied"] = True
            _write_plan(plan)
        try:
            _move_bag(src, dest, progress, expected.get(mv["dest"]), copied)
        except OSError as exc:
            raise AssemblyError(
                f"Finishing the interrupted save failed "
                f"({exc.strerror or exc}). Your recordings are safe; run "
                "mission_close again to retry.") from exc
    missing = _missing_staged_bags(staging, record)
    if missing:
        raise AssemblyError(
            f"The interrupted save in {staging} is missing "
            f"{len(missing)} recording(s), so it can't be finished. Nothing "
            "was changed; ask your robot engineer to look at it.")
    return _commit_and_finish(plan, record, warn)


def saved_crate_for(mission_id: str | None) -> Path | None:
    """The crate this mission was already saved as, if the index knows one."""
    if not mission_id:
        return None
    try:
        row = index.find_mission(mission_id)
    except Exception:
        return None
    if row and Path(row["archive_path"]).is_dir():
        return Path(row["archive_path"])
    return None


def clear_saved_spool(crate: Path, mission_id: str) -> None:
    """Clear spool leftovers of a mission already saved as ``crate``."""
    _clear_spool(mission_id, _crate_bag_sources(crate))


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


# -- assembly ----------------------------------------------------------------

def _space_needed(bags: list[tuple[Any, Path]], staging_root: Path) -> int:
    """Bytes this save writes: foreign copies, and spool bags that can't be
    renamed because the archive is on another filesystem."""
    archive_dev = staging_root.stat().st_dev
    need = 0
    for bag, src in bags:
        try:
            if bag.source in FOREIGN_SOURCES or \
                    src.stat().st_dev != archive_dev:
                need += fsio.dir_size_bytes(src)
        except OSError:
            pass
    return need


def _copy_failed_message(src: Path, exc: OSError) -> str:
    if _is_out_of_space(exc):
        return ("There isn't enough disk space to save this mission. Nothing "
                "was changed.")
    return (f"A recording ({src}) couldn't be copied into the archive "
            f"({exc.strerror or exc}). Nothing was changed — the original "
            "is untouched where it was recorded.")


def _is_out_of_space(exc: BaseException) -> bool:
    if getattr(exc, "errno", None) == errno.ENOSPC:
        return True
    # shutil.copytree collects per-file errors into one shutil.Error with
    # errno None: look inside it.
    return isinstance(exc, shutil.Error) and "No space left" in str(exc)


def _stage(record: MissionRecord, harvest_doc: dict[str, Any], staging: Path,
           progress: Callable[[str], None] | None,
           warn: Callable[[str], None]) -> list[tuple[Path, str]]:
    """Steps 1-3: everything except the spool bags into staging.

    Returns the spool bags still to move, as (source, crate name).
    """
    n_listed = len(record.bags)
    # Foreign recordings are referenced where they were made; drop any whose
    # source vanished since detection so the crate never references a missing
    # bag (the loss is reported to the operator via harvest_level_warnings).
    record.bags = [b for b in record.bags
                   if b.source not in FOREIGN_SOURCES or Path(b.path).is_dir()]
    sources = [Path(b.path) for b in record.bags]

    need = _space_needed(list(zip(record.bags, sources, strict=True)),
                         staging.parent)
    free = shutil.disk_usage(staging.parent).free
    if need + SPACE_MARGIN_BYTES > free:
        raise AssemblyError(
            "There isn't enough disk space to save this mission: it needs "
            f"about {need / 1e9:.1f} GB but only {free / 1e9:.1f} GB is "
            "free. Nothing was changed.")

    # Folder names inside the crate. Foreign recordings come from anywhere, so
    # two can share a name ("test", "ext_live"); they must not collide
    # (2026-10-02: the second copy failed with "File exists").
    crate_names = unique_names([p.name for p in sources])

    (staging / "bags").mkdir(parents=True)
    if progress:
        progress("Collecting mission context")

    harvest_dir = staging / "harvest"
    harvest_dir.mkdir()
    extra_files = [{"id": "harvest/harvest.json",
                    "name": "Raw harvest data",
                    "encodingFormat": "application/json"}]
    fsio.atomic_write_json(harvest_dir / "harvest.json", harvest_doc)
    watchdog_log = paths.watchdog_log_path()
    if watchdog_log.is_file():
        shutil.copy2(watchdog_log, harvest_dir / "watchdog.log")
        extra_files.append({"id": "harvest/watchdog.log",
                            "name": "Recording assistant log",
                            "encodingFormat": "text/plain"})
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

    # Calibration files keep their own name unless two share it (two
    # "ost.yaml" from different folders): the second becomes "ost_2.yaml".
    # The same file referenced twice is stored once.
    cal_dir = staging / "calibrations"
    stored: dict[Path, str] = {}
    for cal in record.calibrations:
        source = Path(cal.source_path)
        if not source.is_file():
            continue
        key = source.resolve()
        if key not in stored:
            cal_dir.mkdir(exist_ok=True)
            stored[key] = _free_name(cal_dir, source.name)
            shutil.copy2(source, cal_dir / stored[key])
        cal.archived_path = f"calibrations/{stored[key]}"
        cal.sha256 = fsio.sha256_file(cal_dir / stored[key])

    raw_inspect = harvest_doc.get("raw_docker_inspect") or []
    if raw_inspect:
        docker_dir = staging / "docker"
        docker_dir.mkdir()
        fsio.atomic_write_json(docker_dir / "containers.json", raw_inspect)
        extra_files.append({"id": "docker/containers.json",
                            "name": "Container inventory",
                            "encodingFormat": "application/json"})
        # Docker's compose label lists every -f file, comma-separated. Each
        # project gets its own folder, even if two names sanitise alike.
        project_dirs: dict[str, str] = {}
        seen = set()
        for container in record.software.docker_containers:
            project = container.compose_project
            files = [f.strip() for f in (container.compose_file or "")
                     .split(",") if f.strip()]
            if not project or not files:
                continue
            if project not in project_dirs:
                base = dirname = sanitise(project)
                n = 1
                while dirname in project_dirs.values():
                    n += 1
                    dirname = f"{base}_{n}"
                project_dirs[project] = dirname
            for compose in files:
                compose_path = Path(compose)
                if (project, compose) in seen or not compose_path.is_file():
                    continue
                seen.add((project, compose))
                dest_dir = docker_dir / "compose" / project_dirs[project]
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest_name = _free_name(dest_dir, compose_path.name)
                shutil.copy2(compose_path, dest_dir / dest_name)
                extra_files.append({
                    "id": f"docker/compose/{project_dirs[project]}/"
                          f"{dest_name}",
                    "name": f"Compose file ({project})",
                    "encodingFormat": "application/yaml"})

    # Step 3a: copy foreign recordings into staging *before* the manifests
    # are written. Copying is non-destructive (the operator's original is
    # left in place). A foreign bag that vanished since the pre-assembly check
    # is dropped and reported; any other copy failure (a full disk, a read
    # error) aborts the save — silently saving a mission without one of its
    # recordings is worse than not saving yet.
    kept: list[tuple[Any, Path, str]] = []
    vanished: list[str] = []
    for bag, src, name in zip(record.bags, sources, crate_names, strict=True):
        if bag.source in FOREIGN_SOURCES:
            if progress:
                progress(f"Copying recording {src.name}")
            dest = staging / "bags" / name
            try:
                shutil.copytree(src, dest)
            except OSError as exc:
                shutil.rmtree(dest, ignore_errors=True)
                if src.exists():
                    raise AssemblyError(_copy_failed_message(src, exc)) \
                        from exc
                log.warning("foreign recording %s vanished during assembly "
                            "(%s); dropping it from the mission", src, exc)
                vanished.append(src.name)
                continue
        kept.append((bag, src, name))
    record.bags = [k[0] for k in kept]

    warnings: list[str] = []
    if vanished:
        msg = (f"{len(vanished)} recording(s) disappeared from where they "
               "were recorded while the mission was being saved, so they "
               f"are not in it: {', '.join(vanished)}.")
        warnings.append(msg)
        warn(msg)
    if not record.bags:
        raise AssemblyError(
            "None of this mission's recordings can be found any more, so "
            "there is nothing to save. Nothing was changed.")
    if len(record.bags) != n_listed:
        # The review graded the recordings it was shown; grade what is
        # actually being saved.
        from ros_fairy.manifest import quality
        record.provenance.data_quality = quality.assess(
            record, harvest_doc).level

    # Crate-relative bag paths + per-file checksums + assembly provenance,
    # then manifests. Foreign bags are hashed as copied into staging (the
    # original may still change); spool bags are moved verbatim, so hashing
    # them in the spool pins the archived bytes.
    to_move = []
    for bag, src, name in kept:
        if bag.source in FOREIGN_SOURCES:
            bag.file_sha256 = _bag_file_hashes(staging / "bags" / name)
        else:
            bag.file_sha256 = _bag_file_hashes(src)
            to_move.append((src, name))
        bag.path = f"bags/{name}"
    record.provenance.assembled_at = datetime.now(timezone.utc)

    from ros_fairy.manifest import builder
    warnings = builder.harvest_level_warnings(harvest_doc) + warnings
    (staging / "README.md").write_text(_render_readme(record, warnings))
    fsio.atomic_write_json(
        staging / "mission_record.json",
        record.model_dump(mode="json"))
    ro_crate.write(record, staging, extra_files,
                   license_url=harvest_doc.get("default_license"))
    return to_move


def assemble(record: MissionRecord, harvest_doc: dict[str, Any],
             progress: Callable[[str], None] | None = None,
             warn: Callable[[str], None] | None = None) -> Path:
    """Build and commit the mission archive. Returns the final path.

    Raises AssemblyError with a plain-language message; on failure before the
    commit point the spool is left (or put back) exactly as it was. ``warn``
    receives plain-language notices for the operator (a recording dropped,
    the index not updated) when the save still succeeds. Callers that may run
    concurrently hold ``save_lock()``.
    """
    warn = warn or (lambda msg: None)
    existing = saved_crate_for(record.identity.mission_id)
    if existing is not None:
        raise AssemblyError(
            f"This mission is already saved as {existing.name}. Nothing was "
            "changed.")
    staging_root = _ensure_staging_root()
    name = archive_name(record)
    staging = staging_root / name
    bag_sources = [b.path for b in record.bags]
    plan: dict[str, Any] = {"version": 1, "name": name,
                            "mission_id": record.identity.mission_id,
                            "state": BUILDING, "bag_sources": bag_sources,
                            "moves": []}

    # Steps 1-3a. Anything going wrong — including Ctrl-C — leaves only
    # copies in staging, which are thrown away.
    try:
        _write_plan(plan)
        to_move = _stage(record, harvest_doc, staging, progress, warn)
    except BaseException as exc:
        _discard_staging(plan)
        if isinstance(exc, AssemblyError) or not isinstance(exc, OSError):
            raise
        if _is_out_of_space(exc):
            raise AssemblyError("There isn't enough disk space to save this "
                                "mission. Nothing was changed.") from exc
        raise AssemblyError(f"Saving failed ({exc.strerror or exc}). "
                            "Nothing was changed.") from exc

    # Step 4: move spool bags — the only step touching spool data. Each move
    # is noted in the plan first, so an interrupted save can be finished.
    expected = {b.path: b.file_sha256 for b in record.bags}
    plan["state"] = MOVING
    plan["moves"] = [{"src": str(src), "dest": f"bags/{name}",
                      "copied": False} for src, name in to_move]
    moved: list[tuple[Path, Path]] = []
    try:
        _write_plan(plan)
        for mv in plan["moves"]:
            src, dest = Path(mv["src"]), staging / mv["dest"]

            def copied(mv=mv, src=src, dest=dest):
                mv["copied"] = True
                _write_plan(plan)
                moved.append((src, dest))
            _move_bag(src, dest, progress, expected.get(mv["dest"]), copied)
            if not mv["copied"]:  # renamed
                moved.append((src, dest))
    except BaseException as exc:
        try:
            for src, dest in reversed(moved):
                _move_back(src, dest)
        except BaseException:
            log.exception("putting the recordings back failed")
            raise AssemblyError(
                f"Saving was interrupted, but your recordings are safe in "
                f"{staging}. Run mission_close again to finish saving.") \
                from exc
        _discard_staging(plan)
        if isinstance(exc, AssemblyError) or not isinstance(exc, OSError):
            raise
        if _is_out_of_space(exc):
            raise AssemblyError("There isn't enough disk space to save this "
                                "mission. Your data is back in the spool, "
                                "unchanged.") from exc
        raise AssemblyError("Saving the recordings failed "
                            f"({exc.strerror or exc}). "
                            "Your data is back in the spool, unchanged."
                            ) from exc

    # Step 5: commit point; then index, spool clean-up and plan removal.
    return _commit_and_finish(plan, record, warn)
