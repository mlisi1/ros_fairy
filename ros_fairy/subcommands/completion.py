"""Shell completion for mission arguments.

``ros2`` completes verbs and options through argcomplete already (ROS's
``setup.bash`` registers it). These completers add the saved missions: a
verb argument gets one with ``action.completer = ...``, which argparse
ignores outside completion.
"""

import glob
import os
from datetime import datetime

from ros_fairy.archive import index


def _description(row: dict) -> str:
    try:
        when = datetime.fromisoformat(row["created_at"]).astimezone() \
            .strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        when = ""
    goal = row.get("goal") or ""
    if len(goal) > 50:
        goal = goal[:49] + "…"
    return f"{when}  {goal}".strip()


def mission_ids(prefix: str, **_kwargs) -> dict[str, str]:
    """Saved mission IDs starting with ``prefix``, each described by its
    date and goal (shown by zsh and fish)."""
    try:
        rows = index.mission_summaries()
    except Exception:  # completion must never print a traceback
        return {}
    return {row["mission_id"]: _description(row) for row in rows
            if row["mission_id"].startswith(prefix)}


def _directories(prefix: str) -> dict[str, str]:
    expanded = os.path.expanduser(prefix)
    found = {}
    for path in sorted(glob.glob(glob.escape(expanded) + "*")):
        if os.path.isdir(path):
            # Keep what the user typed (~ stays ~); a trailing / lets them
            # go on into the folder.
            found[prefix + path[len(expanded):] + "/"] = ""
    return found


def missions_or_folders(prefix: str, **kwargs) -> dict[str, str]:
    """Mission IDs, or folders once the word looks like a path (an archive
    folder or a bag folder, which these verbs also take)."""
    if prefix.startswith(("/", ".", "~")):
        return _directories(prefix)
    return mission_ids(prefix, **kwargs)
