"""SQLite mission index.

The index is a query cache for `ros2 fairy list`; the archive directories'
mission_record.json files are the source of truth, and reindex() can rebuild
the database from them at any time.
"""

import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ros_fairy.manifest.schema import MissionRecord, read_record
from ros_fairy.utils import paths, topic_health

DB_VERSION = "3"


class IndexUnavailableError(Exception):
    """The index exists but this account cannot read it (permissions)."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
    mission_id        TEXT PRIMARY KEY,
    created_at        TEXT NOT NULL,
    operator          TEXT NOT NULL,
    location          TEXT NOT NULL,
    goal              TEXT NOT NULL,
    archive_path      TEXT NOT NULL UNIQUE,
    duration_s        REAL NOT NULL DEFAULT 0,
    size_bytes        INTEGER NOT NULL DEFAULT 0,
    bag_count         INTEGER NOT NULL DEFAULT 0,
    warning_count     INTEGER NOT NULL DEFAULT 0,
    robot_name        TEXT,
    ros_fairy_version  TEXT,
    schema_version    TEXT NOT NULL,
    data_quality      TEXT
);
CREATE INDEX IF NOT EXISTS idx_missions_created
    ON missions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_missions_operator
    ON missions(operator COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_missions_location
    ON missions(location COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
-- One row per bag per mission; lets mission_close ask "have I already saved
-- a bag exactly like this one?" (topic_health.bag_fingerprint) before the
-- operator commits to saving — see archive/duplicates.py.
CREATE TABLE IF NOT EXISTS mission_bags (
    mission_id  TEXT NOT NULL,
    fingerprint TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mission_bags_fingerprint
    ON mission_bags(fingerprint);
CREATE INDEX IF NOT EXISTS idx_mission_bags_mission
    ON mission_bags(mission_id);
-- One row per mission that `ros2 fairy export` has actually produced a
-- bundle for — written only after the bundle file exists on disk, so
-- `export --all` never counts a failed/aborted export as done.
CREATE TABLE IF NOT EXISTS exports (
    mission_id  TEXT PRIMARY KEY,
    exported_at TEXT NOT NULL,
    bundle_path TEXT NOT NULL,
    format      TEXT NOT NULL,
    sha256      TEXT NOT NULL
);
"""


# Column order is the single source of truth for the positional INSERTs below.
_COLUMNS = (
    "mission_id", "created_at", "operator", "location", "goal", "archive_path",
    "duration_s", "size_bytes", "bag_count", "warning_count", "robot_name",
    "ros_fairy_version", "schema_version", "data_quality",
)
_INSERT = (f"INSERT OR REPLACE INTO missions ({', '.join(_COLUMNS)}) VALUES "
           f"({', '.join('?' for _ in _COLUMNS)})")


def _connect() -> sqlite3.Connection:
    db = paths.index_db_path()
    con = sqlite3.connect(db, timeout=5.0)
    con.row_factory = sqlite3.Row
    # Group-writable so an index created by one account (e.g. root during
    # setup) doesn't lock out the next operator's mission_close insert.
    try:
        os.chmod(db, 0o664)
    except OSError:
        pass
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 5000")
    con.executescript(_SCHEMA)
    # Migrate pre-2 databases: add columns the current schema expects.
    existing = {row[1] for row in con.execute("PRAGMA table_info(missions)")}
    if "data_quality" not in existing:
        con.execute("ALTER TABLE missions ADD COLUMN data_quality TEXT")
    con.execute("INSERT OR REPLACE INTO meta VALUES ('db_version', ?)",
                (DB_VERSION,))
    return con


def _connect_readonly() -> sqlite3.Connection:
    """A connection that cannot write — queries must not create or migrate.

    WAL reads still need the -shm sidecar, so an account without write access
    to the index directory raises IndexUnavailableError with a plain-language
    fix instead of sqlite3's traceback.
    """
    db = paths.index_db_path()
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout = 5000")
        # Probe the WAL sidecar now so the failure is caught in one place.
        con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return con
    except sqlite3.OperationalError as exc:
        raise IndexUnavailableError(
            f"I don't have permission to read the mission index ({db}). "
            "Ask your engineer to add your account to the 'ros-fairy' group "
            "(sudo usermod -aG ros-fairy <your username>), then log out and "
            "back in.") from exc


def _row_from_record(record: MissionRecord, archive_path: Path) -> tuple:
    return (
        record.identity.mission_id,
        # Normalised to UTC so the strings sort and compare as times, whatever
        # offset the record was written with.
        record.identity.created_at.astimezone(timezone.utc).isoformat(),
        record.identity.operator_name,
        record.intent.location_name,
        record.intent.goal,
        str(archive_path),
        sum(b.duration_s or 0 for b in record.bags),
        sum(b.size_bytes for b in record.bags),
        len(record.bags),
        sum(len(b.health_warnings) for b in record.bags),
        record.robot.name if record.robot else None,
        record.provenance.ros_fairy_version,
        record.schema_version,
        record.provenance.data_quality,
    )


def _replace_bag_fingerprints(con: sqlite3.Connection,
                              record: MissionRecord) -> None:
    con.execute("DELETE FROM mission_bags WHERE mission_id = ?",
               (record.identity.mission_id,))
    con.executemany(
        "INSERT INTO mission_bags (mission_id, fingerprint) VALUES (?, ?)",
        [(record.identity.mission_id, topic_health.bag_fingerprint(b))
         for b in record.bags])


def insert(record: MissionRecord, archive_path: Path) -> None:
    with _connect() as con:
        con.execute(_INSERT, _row_from_record(record, archive_path))
        _replace_bag_fingerprints(con, record)


def find_mission(mission_id: str) -> dict | None:
    """The index row of ``mission_id``, or None if it isn't indexed."""
    if not Path(paths.index_db_path()).exists():
        return None
    try:
        con = _connect()
    except sqlite3.OperationalError:
        con = _connect_readonly()
    try:
        row = con.execute("SELECT * FROM missions WHERE mission_id = ?",
                          (mission_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise
    finally:
        con.close()
    return dict(row) if row else None


def find_bag_duplicate(fingerprints: list[str],
                       exclude_mission_id: str | None = None) -> dict | None:
    """The most recent already-saved mission sharing any of ``fingerprints``.

    None if the index is unavailable/unreadable — this is a courtesy check,
    never fatal (mirrors ``duplicates.find_similar``).
    """
    if not fingerprints or not Path(paths.index_db_path()).exists():
        return None
    try:
        con = _connect()
    except sqlite3.OperationalError:
        con = _connect_readonly()
    try:
        placeholders = ", ".join("?" for _ in fingerprints)
        params: list[Any] = list(fingerprints)
        exclude_clause = ""
        if exclude_mission_id is not None:
            exclude_clause = "AND m.mission_id != ?"
            params.append(exclude_mission_id)
        row = con.execute(
            f"SELECT m.* FROM missions m "
            f"JOIN mission_bags b ON b.mission_id = m.mission_id "
            f"WHERE b.fingerprint IN ({placeholders}) {exclude_clause} "
            f"ORDER BY m.created_at DESC LIMIT 1", params).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise
    finally:
        con.close()
    return dict(row) if row else None


def mark_exported(mission_id: str, bundle_path: Path, fmt: str,
                  sha256: str) -> None:
    """Record that a bundle was actually written for ``mission_id``.

    Call only after the bundle file exists on disk — this is what
    ``export --all`` treats as "already exported".
    """
    with _connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO exports "
            "(mission_id, exported_at, bundle_path, format, sha256) "
            "VALUES (?, ?, ?, ?, ?)",
            (mission_id, datetime.now(timezone.utc).isoformat(),
             str(bundle_path), fmt, sha256))


def exported_mission_ids() -> set[str]:
    """Every mission_id with at least one recorded successful export."""
    if not Path(paths.index_db_path()).exists():
        return set()
    try:
        con = _connect()
    except sqlite3.OperationalError:
        con = _connect_readonly()
    try:
        rows = con.execute("SELECT mission_id FROM exports").fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return set()
        raise
    finally:
        con.close()
    return {row[0] for row in rows}


def _utc_bound(value: str, end: bool) -> str:
    """A --since/--until value as a UTC ISO string comparable with the
    stored ``created_at``. A bare date is a day in *local* time (what the
    operator sees in `list`): --since is its first instant, --until (which
    includes the day) the first instant of the next one."""
    try:
        if len(value) == 10:
            day = date.fromisoformat(value)
            if end:
                day += timedelta(days=1)
            moment = datetime(day.year, day.month, day.day).astimezone()
        else:
            moment = datetime.fromisoformat(value)
            if moment.tzinfo is None:
                moment = moment.astimezone()
    except ValueError:
        return value  # leave odd input to the string comparison
    return moment.astimezone(timezone.utc).isoformat()


def query(operator: str | None = None, location: str | None = None,
          since: str | None = None, until: str | None = None,
          quality: str | None = None,
          limit: int | None = 20) -> tuple[list[dict[str, Any]], int]:
    """Filtered mission rows, newest first. Returns (rows, total_matching).

    ``limit=None`` returns every matching row.
    """
    where, params = [], []
    if operator:
        where.append("operator LIKE ? COLLATE NOCASE")
        params.append(f"%{operator}%")
    if location:
        where.append("location LIKE ? COLLATE NOCASE")
        params.append(f"%{location}%")
    if quality:
        where.append("data_quality = ?")
        params.append(quality)
    if since:
        where.append("created_at >= ?")
        params.append(_utc_bound(since, end=False))
    if until:
        where.append("created_at < ?")
        params.append(_utc_bound(until, end=True))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    if not Path(paths.index_db_path()).exists():
        return [], 0
    try:
        con = _connect()
    except sqlite3.OperationalError:
        # No write access (index dir belongs to the ros-fairy group): reading
        # is still fine — WAL setup and schema migration belong to writers.
        con = _connect_readonly()
    try:
        with con:
            total = con.execute(
                f"SELECT COUNT(*) FROM missions{clause}", params).fetchone()[0]
            rows = con.execute(
                f"SELECT * FROM missions{clause} ORDER BY created_at DESC "
                f"LIMIT ?", [*params, -1 if limit is None else limit]).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return [], 0
        raise
    finally:
        con.close()
    return [dict(r) for r in rows], total


def delete(mission_id: str) -> None:
    """Forget a mission: its row, bag fingerprints and export record."""
    con = _connect()
    try:
        with con:
            for table in ("missions", "mission_bags", "exports"):
                con.execute(f"DELETE FROM {table} WHERE mission_id = ?",
                            (mission_id,))
    finally:
        con.close()


def reindex(archive_root: Path | None = None,
            report: dict | None = None) -> int:
    """Rebuild the index by scanning archive dirs for mission_record.json.

    Records saved by a newer ros-fairy are indexed with the fields this
    version doesn't know set aside. ``report``, when given, receives
    ``skipped`` ([(folder, reason)] for archives that couldn't be read) and
    ``duplicates`` ({mission_id: [folders]}: the same mission saved twice;
    the most recently assembled one is indexed).
    """
    archive_root = archive_root or paths.archive_dir()
    skipped: list[tuple[str, str]] = []
    by_id: dict[str, list[tuple[MissionRecord, Path]]] = {}
    for record_file in sorted(archive_root.glob("*/mission_record.json")):
        try:
            record, _ = read_record(json.loads(record_file.read_text()))
        except Exception as exc:
            skipped.append((record_file.parent.name, str(exc).splitlines()[0]))
            continue
        by_id.setdefault(record.identity.mission_id, []).append(
            (record, record_file.parent))

    def assembled(item: tuple[MissionRecord, Path]) -> str:
        at = item[0].provenance.assembled_at
        return at.isoformat() if at else ""

    count = 0
    with _connect() as con:
        con.execute("DELETE FROM missions")
        con.execute("DELETE FROM mission_bags")
        for items in by_id.values():
            record, folder = max(items, key=assembled)
            con.execute(_INSERT, _row_from_record(record, folder))
            _replace_bag_fingerprints(con, record)
            count += 1
    if report is not None:
        report["skipped"] = skipped
        report["duplicates"] = {
            mid: sorted(str(f) for _, f in items)
            for mid, items in by_id.items() if len(items) > 1}
    return count
