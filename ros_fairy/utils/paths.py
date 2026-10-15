"""Canonical filesystem paths for ros-fairy.

Every other module must obtain paths from here. Two roots:

- the data directory (spool, archive, index, state, log): the
  ``ROS_FAIRY_VAR_DIR`` environment variable if set, else ``var_dir`` in
  ``<config dir>/paths.yaml`` (written by ``setup --var-dir`` and
  ``move_data``), else ``/var/ros-fairy``. The file is read by every process
  (operator shells, the service, sudo, cron, launch files), so moving the
  data needs no new login;
- the config directory: ``ROS_FAIRY_CONFIG_DIR`` if set, else
  ``/etc/ros-fairy``. It holds paths.yaml, so it can only move by the
  environment variable (tests use both variables).

Paths are resolved lazily (functions, not module constants) so an env change
made by a test fixture takes effect immediately.
"""

import os
from pathlib import Path

DEFAULT_VAR_DIR = "/var/ros-fairy"
DEFAULT_CONFIG_DIR = "/etc/ros-fairy"
VAR_DIR_ENV = "ROS_FAIRY_VAR_DIR"


class PathsConfigError(Exception):
    """paths.yaml exists but can't be used: in plain words, for the operator.

    Raised rather than falling back to the default, which would quietly
    record into (and list) a different archive."""


_cache: dict[tuple, Path | None] = {}


def paths_file() -> Path:
    return config_dir() / "paths.yaml"


def configured_var_dir() -> Path | None:
    """``var_dir`` from paths.yaml, or None when there is no such file.
    Cached until the file changes (the watchdog asks every second)."""
    path = paths_file()
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PathsConfigError(f"I can't read {path}: {exc}") from exc
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key not in _cache:
        _cache.clear()
        _cache[key] = _parse_paths_file(path)
    return _cache[key]


def _parse_paths_file(path: Path) -> Path:
    import yaml
    problem = None
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        problem = f"it can't be read ({str(exc).splitlines()[0]})"
    else:
        value = data.get("var_dir") if isinstance(data, dict) else None
        if not isinstance(value, str) or not value.strip():
            problem = "it has no `var_dir: /some/folder` line"
        elif not os.path.isabs(value.strip()):
            problem = f"var_dir ({value}) isn't an absolute path"
        else:
            return Path(value.strip())
    raise PathsConfigError(
        f"{path} says where the missions are kept, but {problem}. Fix it, or "
        "set it again with `ros2 fairy setup --var-dir <folder>`.")


def var_dir_source() -> str:
    """Where :func:`var_dir` comes from: "environment", "paths.yaml" or
    "default"."""
    if os.environ.get(VAR_DIR_ENV):
        return "environment"
    return "paths.yaml" if configured_var_dir() is not None else "default"


def var_dir() -> Path:
    env = os.environ.get(VAR_DIR_ENV)
    if env:
        return Path(env)
    return configured_var_dir() or Path(DEFAULT_VAR_DIR)


def config_dir() -> Path:
    return Path(os.environ.get("ROS_FAIRY_CONFIG_DIR", DEFAULT_CONFIG_DIR))


def robot_identity_path() -> Path:
    return config_dir() / "robot_identity.yaml"


def watchdog_env_path() -> Path:
    """systemd EnvironmentFile holding the ROS 2 environment captured at setup.

    The watchdog runs as a system service with no login shell, so the ROS
    environment the operator had sourced when running ``setup`` is snapshotted
    here and loaded by the unit (``EnvironmentFile=``). Without it ``ros2`` is
    not on PATH and the graph/description harvest fails. The unit references the
    default ``/etc/ros-fairy/watchdog.env`` (checked by
    ``tests/unit/test_packaging.py``).
    """
    return config_dir() / "watchdog.env"


def spool_dir() -> Path:
    return var_dir() / "spool"


def bags_dir() -> Path:
    return spool_dir() / "bags"


def harvest_json_path() -> Path:
    return spool_dir() / "harvest.json"


def harvest_lock_path() -> Path:
    """flock target serialising harvest.json updates."""
    return spool_dir() / ".harvest.lock"


def watchdog_log_path() -> Path:
    """The watchdog's log for the open mission; archived with it."""
    return spool_dir() / "watchdog.log"


def session_env_path() -> Path:
    """ROS environment of the live recording shell, refreshed at mission time.

    Written by ``mission_start`` / ``mission_record`` (which run as the
    operator); the watchdog adopts its DDS *discovery* keys
    (``ros_env.SESSION_ADOPT_KEYS``) at harvest time so the service lands on the
    same partition as the session actually recording, even if the frozen
    ``watchdog.env`` snapshot has drifted. Lives in the spool because that is
    group-writable; ``watchdog.env`` lives in root-owned ``/etc``. Because it is
    group-writable, the watchdog never adopts loader paths from it (privilege
    escalation); base PATH/overlay come only from the trusted ``watchdog.env``.
    """
    return spool_dir() / "session.env"


def mission_context_path() -> Path:
    return spool_dir() / "mission_context.json"


def archive_dir() -> Path:
    return var_dir() / "archive"


def staging_dir() -> Path:
    return archive_dir() / ".staging"


def index_db_path() -> Path:
    return var_dir() / "index.db"


def ros_log_dir() -> Path:
    """ROS client-library log directory for the HOME-less watchdog service.

    rcl logging expands ``~/.ros/log`` unless ``ROS_LOG_DIR`` is set; under
    systemd there is no ``$HOME``, so node creation fails (breaking
    ``ros2 param dump`` and the rclpy description capture). The watchdog
    points ``ROS_LOG_DIR`` here when it starts without a HOME.
    """
    return var_dir() / "log"


def watchdog_state_path() -> Path:
    return var_dir() / "watchdog.state"
