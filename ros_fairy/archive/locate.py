"""Resolve a user-supplied mission identifier to its archive on disk.

Shared by the verbs that take a mission argument (``diff``, ``verify``) so the
acceptance rules stay in one place. The archive directories' mission_record.json
files are the source of truth; the SQLite index is only used to map numbers and
IDs to paths.
"""

import json
from pathlib import Path

from ros_fairy.archive import index
from ros_fairy.manifest.schema import (
    MissionRecord,
    NewerRecordError,
    read_record,
)


class LocateError(Exception):
    """Plain-language failure resolving or loading a mission archive."""


STALE_HINT = ("the mission list may be out of date (an archive moved or "
              "deleted by hand); run `ros2 fairy reindex` to rebuild it")


def resolve_archive(identifier: str) -> Path:
    """Map a user-supplied identifier to an archive directory.

    Accepts (in order of precedence):
      - an archive folder   ->  a path containing mission_record.json (an
                                existing folder wins over the number reading
                                of an all-digit name)
      - a positive integer  ->  Nth most recent mission (1 = newest); plain
                                digits only, so " 3" or "+3" are not numbers
      - a mission ID string ->  looked up in the index
    A path the index points to that is gone gives a ``reindex`` hint.
    """
    p = Path(identifier).expanduser()
    if p.is_dir() and (p / "mission_record.json").is_file():
        return p

    if identifier.isdigit():
        n = int(identifier)
        if n < 1:
            raise LocateError(f"Mission number must be 1 or higher (got {n}).")
        try:
            rows, total = index.query(limit=n)
        except index.IndexUnavailableError as exc:
            raise LocateError(str(exc)) from exc
        if n > len(rows):
            raise LocateError(
                f"There {'is' if total == 1 else 'are'} only {total} saved "
                f"mission{'s' if total != 1 else ''}; {n} is out of range.")
        return _existing(rows[n - 1], f"mission {n}")

    try:
        row = index.find_mission(identifier)
    except index.IndexUnavailableError as exc:
        raise LocateError(str(exc)) from exc
    if row is not None:
        return _existing(row, identifier)

    raise LocateError(
        f"Can't find a mission matching '{identifier}'. "
        "Use a number (1 = most recent), an archive path, or a mission ID "
        "(e.g. m-20260612-140258-9f3a).")


def _existing(row: dict, label: str) -> Path:
    """The row's archive, if it is still there and still that mission."""
    path = Path(row["archive_path"])
    if not (path / "mission_record.json").is_file():
        raise LocateError(f"The archive of {label} isn't at {path} any more: "
                          f"{STALE_HINT}.")
    return path


def load_record_with_notes(path: Path) -> tuple[MissionRecord, list[str]]:
    """``(record, fields set aside)`` from an archive directory. Fields are
    set aside when the record was saved by a newer ros-fairy."""
    record_file = path / "mission_record.json"
    if not record_file.is_file():
        raise LocateError(
            f"{path} doesn't look like a mission archive "
            "(no mission_record.json found).")
    try:
        return read_record(json.loads(record_file.read_text()))
    except NewerRecordError as exc:
        raise LocateError(f"Can't read the mission at {path}: {exc}.") from exc
    except Exception as exc:
        raise LocateError(
            f"Could not read mission record at {path}: {exc}") from exc


def load_record(path: Path) -> MissionRecord:
    """Load and validate ``mission_record.json`` from an archive directory."""
    return load_record_with_notes(path)[0]
