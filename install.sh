#!/usr/bin/env bash
# Installs ros_fairy for whichever ROS 2 install is (or isn't) sourced in this
# shell right now.
#
# No colcon workspace needed: an ament_python package is really just a
# regular Python package plus an ament resource-index marker file, and
# `pip install` places that marker correctly on its own (this is how
# `ros2 fairy` finds the package after a plain `pip install`).
#
# This usually needs root, because it writes into the system Python's
# site-packages so both `ros2` and the systemd watchdog service can find the
# package. Rather than requiring `sudo ./install.sh` up front, this tries a
# plain install first and only escalates if that actually fails for a
# permissions reason — so on a setup that doesn't need it (e.g. an already
# writable venv), no sudo prompt ever appears.
#
# Usage: ./install.sh [--dev]
#   --dev   also install the dev/test extras (pytest, ruff, mypy, rocrate)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TARGET="$REPO_DIR"
if [ "${1:-}" = "--dev" ]; then
    TARGET="$REPO_DIR[dev]"
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required." >&2
    exit 1
fi
if ! command -v ros2 >/dev/null 2>&1; then
    echo "Note: ros2 isn't on PATH in this shell — that's fine for" >&2
    echo "installing the package, but source your ROS 2 setup.bash before" >&2
    echo "using 'ros2 fairy ...' afterwards." >&2
fi

LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT

# pip builds in-tree and reuses an existing build/ directory, so a module
# deleted from the source would be installed again from build/lib (it
# happened: harvest/ros_descriptions.py outlived its removal). Start clean.
# An earlier `sudo` install leaves these owned by root.
STALE=("$REPO_DIR/build" "$REPO_DIR"/*.egg-info)
if ! rm -rf "${STALE[@]}" 2>/dev/null; then
    if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
        echo "Removing an old build directory left by a root install." >&2
        sudo rm -rf "${STALE[@]}"
    else
        echo "Couldn't remove ${STALE[*]} — delete it and re-run." >&2
        exit 1
    fi
fi

echo "Installing ros_fairy..."
if python3 -m pip install "$TARGET" >"$LOG" 2>&1; then
    cat "$LOG"
elif grep -qiE "read-only file system|errno 30|EROFS" "$LOG"; then
    # sudo can't fix this — the filesystem itself refuses writes (common on
    # robot images with a read-only rootfs overlay). Retrying as root would
    # just fail the same way, so say so instead of retrying.
    echo "The filesystem pip is trying to write to is read-only, so sudo" >&2
    echo "won't help. Remount it read-write, or install into a venv on" >&2
    echo "writable storage: python3 -m venv ~/ros_fairy_venv && ~/ros_fairy_venv/bin/pip install $TARGET" >&2
    cat "$LOG" >&2
    exit 1
elif grep -qiE "permission denied|errno 13|externally-managed-environment" "$LOG"; then
    echo "System-wide install needs root — retrying." >&2
    if [ "$(id -u)" -eq 0 ]; then
        python3 -m pip install --break-system-packages "$TARGET"
    elif command -v sudo >/dev/null 2>&1; then
        sudo python3 -m pip install --break-system-packages "$TARGET"
    else
        echo "Not root and no sudo available — re-run this script as root." >&2
        cat "$LOG" >&2
        exit 1
    fi
else
    cat "$LOG" >&2
    exit 1
fi

cat <<'EOF'

Installed. Next:
    ros2 fairy setup      # configure this robot
    ros2 fairy doctor     # confirm it's ready to capture

setup runs as you — no sudo needed to start it. It only asks for your
password if/when it actually needs to write /etc/ros-fairy and install the
watchdog service.
EOF
