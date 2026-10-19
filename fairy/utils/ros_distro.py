"""ROS 2 distro detection and per-distro capability lookup.

The core stays version-agnostic by using subprocess and
environment introspection rather than per-distro code paths. The one thing that
genuinely differs by distro and that the core must reason about is rosbag2's
*default* storage format: Foxy–Iron default to sqlite3, Jazzy and later default
to MCAP. When a bag's ``metadata.yaml`` omits its ``storage_identifier`` (older
or hand-rolled bags), inferring the recording distro's default beats blindly
assuming one format.

This is a small data-driven registry, not a behaviour switch — unknown distros
degrade to a conservative default and everything else stays subprocess-driven.

Lives in ``utils`` (not ``harvest``) so both the harvest layer and ``utils``
modules like ``topic_health`` can depend on it without reversing the layering.
"""

import os
from dataclasses import dataclass

SQLITE3 = "sqlite3"
MCAP = "mcap"

# Conservative fallback for an unknown / unsourced distro. sqlite3 is the only
# format the timestamp reader supports today, so guessing it degrades to a
# harmless empty result if wrong (no .db3 found) rather than mis-parsing.
DEFAULT_STORAGE_FALLBACK = SQLITE3

# rosbag2 switched its default storage plugin from sqlite3 to MCAP in Jazzy.
_DEFAULT_STORAGE: dict[str, str] = {
    "foxy": SQLITE3,
    "galactic": SQLITE3,
    "humble": SQLITE3,
    "iron": SQLITE3,
    "jazzy": MCAP,
    "kilted": MCAP,
    "rolling": MCAP,
}


@dataclass(frozen=True)
class DistroCaps:
    """What the core needs to know about a ROS 2 distro."""

    name: str | None
    default_storage: str


def detect() -> str | None:
    """The active ROS 2 distro from ``$ROS_DISTRO``, or None if unsourced."""
    distro = os.environ.get("ROS_DISTRO")
    if not distro or not distro.strip():
        return None
    return distro.strip().lower()


def infer_from_packages(package_names) -> str | None:
    """Best-effort distro from installed ``ros-<distro>-*`` package names.

    ``$ROS_DISTRO`` is the source of truth, but the watchdog often runs
    unsourced (harvest may not see what the recorder does), so
    ``detect`` returns None even on a fully-installed robot. The apt inventory
    still names the distro — ``ros-jazzy-rclcpp`` and friends — so fall back to
    the most common recognised distro prefix among the installed packages.
    """
    counts: dict[str, int] = {}
    for name in package_names or ():
        parts = name.split("-")
        if len(parts) >= 2 and parts[0] == "ros" and parts[1] in _DEFAULT_STORAGE:
            counts[parts[1]] = counts.get(parts[1], 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda d: counts[d])


def default_storage(distro: str | None = None) -> str:
    """rosbag2's default ``storage_identifier`` for ``distro``.

    Falls back to the detected distro, then to ``DEFAULT_STORAGE_FALLBACK``.
    """
    key = (distro or detect() or "").lower()
    return _DEFAULT_STORAGE.get(key, DEFAULT_STORAGE_FALLBACK)


def capabilities(distro: str | None = None) -> DistroCaps:
    """Capability bundle for ``distro`` (detected if not given)."""
    distro = distro or detect()
    key = (distro or "").lower()
    return DistroCaps(
        name=distro,
        default_storage=_DEFAULT_STORAGE.get(key, DEFAULT_STORAGE_FALLBACK),
    )
