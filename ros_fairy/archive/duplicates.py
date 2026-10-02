"""Detect a duplicate mission about to be saved.

Two different problems, two different checks:

- ``find_similar``/``describe``: catches the field *mistake* where an
  operator re-briefs and saves the same outing twice, often with a typo in
  the place name (e.g. "Crosslab" vs "Crossloab"). A fuzzy, time-windowed
  heuristic on operator-typed metadata — it can't tell a genuine mistake
  from a routine repeat visit, so it only fires within a short window (see
  ``DEFAULT_WINDOW``).
- ``find_exact_duplicate``/``describe_exact``: catches actually
  *reprocessing the same recording* — a mission_close retry, a bag
  ``adopt``ed twice — by content fingerprint (``topic_health.
  bag_fingerprint``), regardless of how much time has passed. No fuzziness:
  two independently-recorded bags matching on size, message count, duration
  and every per-topic count simultaneously is practically impossible, so
  this can be stated with much more confidence than the metadata heuristic.

Both are surfaced at ``mission_close`` so the operator can notice before
saving a confusing duplicate. Neither blocks — the dashcam principle is to
never lose a recording; the choice stays with the operator.
"""

import difflib
from datetime import datetime, timedelta

from ros_fairy.archive import index
from ros_fairy.manifest.schema import MissionRecord
from ros_fairy.utils.topic_health import bag_fingerprint, humanize_duration

# A location this close (0..1) counts as "the same place, maybe mistyped".
# "crosslab"/"crossloab" ≈ 0.94; unrelated names fall well below.
LOCATION_SIMILARITY = 0.85
# The mistake this guards against — re-briefing and re-saving the same
# outing — happens within the same sitting (minutes, occasionally an hour).
# Repeat missions at one place hours or days apart (routine validation runs,
# a site revisited later) are normal and shouldn't be flagged just because
# the date already tells them apart (reported 2026-09-11: two genuinely
# separate missions ~20.5h apart at the same lab were flagged).
DEFAULT_WINDOW = timedelta(hours=2)


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def find_similar(record: MissionRecord,
                 window: timedelta = DEFAULT_WINDOW) -> list[dict]:
    """Saved missions that look like duplicates of ``record``, newest first.

    Criteria: same operator, saved within ``window`` of this mission's time, and
    a location string at least ``LOCATION_SIMILARITY`` similar. Index errors
    degrade to an empty list — this is a courtesy check, never fatal.
    """
    try:
        rows, _ = index.query(operator=record.identity.operator_name, limit=50)
    except Exception:
        return []

    new_loc = _norm(record.intent.location_name)
    new_when = record.identity.created_at
    matches = []
    for row in rows:
        if row["mission_id"] == record.identity.mission_id:
            continue
        if _norm(row["operator"]) != _norm(record.identity.operator_name):
            continue
        try:
            when = datetime.fromisoformat(row["created_at"])
        except (ValueError, TypeError):
            continue
        if abs((new_when - when).total_seconds()) > window.total_seconds():
            continue
        ratio = difflib.SequenceMatcher(
            None, new_loc, _norm(row["location"])).ratio()
        if ratio >= LOCATION_SIMILARITY:
            matches.append(row)
    return matches


def describe(record: MissionRecord, rows: list[dict]) -> str | None:
    """One plain-language warning for all likely duplicates (``find_similar``
    output, newest first), or None if there are none.

    One line per match buried the review under near-identical warnings during
    a session of repeated test missions (2026-10-02); the newest match is the
    one the operator most likely re-saved, the rest are just counted.
    """
    if not rows:
        return None
    row = rows[0]
    try:
        when = datetime.fromisoformat(row["created_at"])
        elapsed = (record.identity.created_at - when).total_seconds()
        ago = f"{humanize_duration(elapsed)} ago"
    except (ValueError, TypeError):
        ago = "earlier"
    more = len(rows) - 1
    others = f" (and {more} more there recently)" if more else ""
    typo = "" if _norm(row["location"]) == _norm(record.intent.location_name) \
        else " — check for a typo in the place name"
    return (f'You already saved a mission at "{row["location"]}" {ago}'
            f"{others}. Save this only if it really is a different "
            f"mission{typo}.")


def find_exact_duplicate(record: MissionRecord) -> dict | None:
    """The already-saved mission whose bags this recording matches, if any.

    Unlike ``find_similar``, this has no time window and no location
    matching — a content match is a content match regardless of when or
    where the operator says it happened. Index errors degrade to no match —
    this is a courtesy check, never fatal.
    """
    if not record.bags:
        return None
    fingerprints = [bag_fingerprint(b) for b in record.bags]
    try:
        return index.find_bag_duplicate(
            fingerprints, exclude_mission_id=record.identity.mission_id)
    except Exception:
        return None


def describe_exact(row: dict) -> str:
    """A plain-language one-liner: this looks like the same recording."""
    return (f'This looks like the same recording as the mission you already '
            f'saved as "{row["goal"]}" at "{row["location"]}" — not just a '
            "similar one. Saving again will create a second copy of the "
            "same data.")
