"""Change a saved mission's briefing, keeping the crate consistent.

The briefing (operator, goal, place, environment, notes) appears in four
files: mission_record.json, README.md, ro-crate-metadata.json and, through
their checksums, checksums.sha256. All four are rewritten together:

- mission_record.json: the new values, plus an entry in provenance.edits
  (when, which account, each field before and after);
- README.md: rendered again, keeping the warnings it was saved with;
- ro-crate-metadata.json: patched, not rebuilt. Building it from the old and
  the new record and changing only the values that differ keeps what can't
  be rebuilt from the record (the extra files listed, the license);
- checksums.sha256: the lines of those three files, nothing else.

The crate is checked against its checksums first: an archive already changed
by hand isn't touched (re-hashing would bless the change).

A redaction (``redact=True``) also removes the old values: the changed
fields' "before" values are not kept, earlier edits' values of those fields
are blanked, the folder is renamed when its name came from a redacted place
or operator, and the index is compacted so no old page keeps the text.
Copies made before (exports, backups) are out of its reach.

Crash safety: the new files are written next to the old ones
(``<name>.editing``), then a marker (``.edit-ready``) says they are complete,
then each is renamed into place and the marker removed. An interrupted edit
is rolled forward when the marker exists, and thrown away when it doesn't
(:func:`finish_interrupted`).
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ros_fairy import SCHEMA_VERSION
from ros_fairy.archive import assembler, index, ro_crate
from ros_fairy.archive.assembler import CHECKSUMS_FILE
from ros_fairy.manifest import validator
from ros_fairy.manifest.schema import (
    EditedValue,
    MissionRecord,
    RecordEdit,
    read_record,
)
from ros_fairy.utils import fsio, operators

# Briefing field -> (section of the record, attribute).
FIELDS = {"operator_name": ("identity", "operator_name"),
          "goal": ("intent", "goal"),
          "location_name": ("intent", "location_name"),
          "environment": ("intent", "environment"),
          "notes": ("intent", "notes")}
REQUIRED = ("operator_name", "goal", "location_name")

RECORD, README, CRATE = "mission_record.json", "README.md", \
    "ro-crate-metadata.json"
REWRITTEN = (CRATE, README, RECORD, CHECKSUMS_FILE)  # renamed in this order
PENDING_SUFFIX = ".editing"
READY_MARKER = ".edit-ready"


class EditError(Exception):
    """Why the briefing can't be changed, in plain words."""


def leftovers() -> tuple[str, ...]:
    """The names an interrupted edit leaves in the archive."""
    return (READY_MARKER, *(f"{name}{PENDING_SUFFIX}" for name in REWRITTEN))


def interrupted(crate: Path) -> bool:
    return any((crate / name).exists() for name in leftovers())


def finish_interrupted(crate: Path) -> str | None:
    """Complete (marker present) or discard (absent) an interrupted edit.
    Returns what was done, or None when there was nothing to do."""
    if not interrupted(crate):
        return None
    pending = [crate / f"{name}{PENDING_SUFFIX}" for name in REWRITTEN]
    if (crate / READY_MARKER).exists():
        for name, new in zip(REWRITTEN, pending, strict=True):
            if new.exists():
                new.replace(crate / name)
        fsio.fsync_dir(crate)
        (crate / READY_MARKER).unlink()
        fsio.fsync_dir(crate)
        return "finished"
    for new in pending:
        new.unlink(missing_ok=True)
    fsio.fsync_dir(crate)
    return "discarded"


def changed_by_hand(crate: Path) -> list[str]:
    """Files listed in checksums.sha256 that are missing or differ."""
    sums = crate / CHECKSUMS_FILE
    if not sums.is_file():
        return []
    differ = []
    for line in sums.read_text(errors="replace").splitlines():
        digest, _, rel = line.partition("  ")
        if rel and (not (crate / rel).is_file()
                    or fsio.sha256_file(crate / rel) != digest):
            differ.append(rel)
    return differ


def load(crate: Path) -> MissionRecord:
    try:
        record, set_aside = read_record(
            json.loads((crate / RECORD).read_text()))
    except Exception as exc:
        raise EditError(f"{crate / RECORD} can't be read: "
                        f"{str(exc).splitlines()[0]}") from exc
    if set_aside:
        raise EditError("this mission was saved by a newer ros-fairy, with "
                        f"details this version doesn't know ({', '.join(set_aside)}); "
                        "editing it here would lose them. Update ros-fairy "
                        "first.")
    return record


def briefing(record: MissionRecord) -> dict[str, str | None]:
    return {field: getattr(getattr(record, section), attr)
            for field, (section, attr) in FIELDS.items()}


def _saved_warnings(readme: str, record: MissionRecord) -> list[str]:
    """The warnings the README was saved with that aren't the recordings'
    own health warnings (those come from the record): the only input of
    the README that the record doesn't hold."""
    lines = readme.splitlines()
    try:
        start = lines.index("## Warnings") + 2
    except ValueError:
        return []
    bullets = []
    for line in lines[start:]:
        if not line.startswith("- "):
            break
        bullets.append(line[2:])
    for health in (w.plain_text for b in record.bags
                   for w in b.health_warnings):
        if health in bullets:
            bullets.remove(health)
    return bullets


def _patch_crate(current: dict, before: dict, after: dict) -> dict:
    """``current`` with every value that differs between ``before`` and
    ``after`` (two builds from the old and the new record) set as in
    ``after``; everything else, extra files and license included, kept."""
    old = {e["@id"]: e for e in before["@graph"]}
    graph = {e["@id"]: e for e in current["@graph"]}
    for entity in after["@graph"]:
        was = old.get(entity["@id"], {})
        target = graph.get(entity["@id"])
        if target is None:
            current["@graph"].append(entity)
            continue
        for key, value in entity.items():
            if was.get(key) != value:
                target[key] = value
        for key in set(was) - set(entity):
            target.pop(key, None)
    return current


def _renamed(crate: Path, record: MissionRecord) -> Path:
    """Where the crate goes after a redaction: renamed when its name was
    built from a place or operator that is no longer in the record."""
    stamp = record.identity.created_at.astimezone().strftime(
        "%Y-%m-%d_%H-%M-%S")
    base = (f"{stamp}_{assembler.sanitise(record.intent.location_name)}"
            f"_{assembler.sanitise(record.identity.operator_name)}")
    if re.fullmatch(rf"{re.escape(base)}(_\d+)?", crate.name):
        return crate
    target = crate.parent / assembler.archive_name(record)
    crate.rename(target)
    fsio.fsync_dir(crate.parent)
    return target


def apply(crate: Path, wanted: dict[str, str | None], redact: bool = False,
          by: str | None = None) -> tuple[dict[str, EditedValue], Path]:
    """Change the briefing of the mission saved in ``crate`` to ``wanted``
    (fields not given are kept; "" or None clears an optional one).
    ``redact`` removes the old values (see the module notes); ``by`` names
    the person, required for it. Returns the changes made (empty when there
    were none) and the crate's folder (renamed by a redaction of the place
    or operator). Raises EditError."""
    if redact and not (by or "").strip():
        raise EditError("a redaction needs the name of the person doing it")
    finish_interrupted(crate)
    record = load(crate)
    differ = changed_by_hand(crate)
    if differ:
        raise EditError(
            "the archive was changed after it was saved (" + ", ".join(differ)
            + "): `ros2 fairy verify` shows what. Nothing was edited, so as "
            "not to cover that up.")
    current = briefing(record)
    changes: dict[str, EditedValue] = {}
    for field, value in wanted.items():
        value = (value or "").strip() or None
        if value is None and field in REQUIRED:
            raise EditError(f"{field.replace('_', ' ')} can't be empty")
        error = validator.length_error(field, value)
        if error:
            raise EditError(error)
        if value != current[field]:
            changes[field] = EditedValue(before=current[field], after=value)
    if not changes:
        return {}, crate

    old = record.model_copy(deep=True)
    for field, change in changes.items():
        section, attr = FIELDS[field]
        setattr(getattr(record, section), attr, change.after)
    if redact:
        for change in changes.values():
            change.before, change.redacted = None, True
        for earlier in record.provenance.edits:
            for field in changes:
                if field in earlier.changes:
                    earlier.changes[field] = EditedValue(redacted=True)
    record.provenance.edits.append(RecordEdit(
        at=datetime.now(timezone.utc), account=operators.invoking_user(),
        redacted=redact, by=(by or "").strip() or None, changes=changes))
    record.schema_version = SCHEMA_VERSION
    record.provenance.schema_version = SCHEMA_VERSION

    readme = assembler._render_readme(
        record, _saved_warnings((crate / README).read_text(), old))
    crate_doc = _patch_crate(json.loads((crate / CRATE).read_text()),
                             ro_crate.build(old), ro_crate.build(record))
    texts: dict[str, str] = {
        RECORD: json.dumps(record.model_dump(mode="json"), indent=2) + "\n",
        README: readme,
        CRATE: json.dumps(crate_doc, indent=2, ensure_ascii=False) + "\n",
    }
    sums = crate / CHECKSUMS_FILE
    if sums.is_file():
        texts[CHECKSUMS_FILE] = _updated_checksums(sums.read_text(), texts)
    _write_together(crate, texts)

    if redact:
        crate = _renamed(crate, record)
    index.insert(record, crate)
    if redact:
        index.compact()
    return changes, crate


def _updated_checksums(listing: str, texts: dict[str, str]) -> str:
    import hashlib
    lines = []
    for line in listing.splitlines():
        digest, _, rel = line.partition("  ")
        if rel in texts:
            digest = hashlib.sha256(texts[rel].encode()).hexdigest()
        lines.append(f"{digest}  {rel}")
    return "\n".join(lines) + "\n"


def _write_together(crate: Path, texts: dict[str, Any]) -> None:
    for name, text in texts.items():
        # Keep each file's mode: saved crates are group-writable, so every
        # operator can manage them (assembler._make_group_writable).
        try:
            mode = (crate / name).stat().st_mode & 0o777
        except OSError:
            mode = None
        fsio.atomic_write_text(crate / f"{name}{PENDING_SUFFIX}", text,
                               mode=mode)
    fsio.atomic_write_text(crate / READY_MARKER, "")
    finish_interrupted(crate)
