#!/usr/bin/env bash
# Cleanly removes ros_fairy from this system: the systemd watchdog service,
# /etc/ros-fairy, the ros-fairy group, the ephemeral parts of /var/ros-fairy
# (spool/state/log), and the ros_fairy Python package itself — the reverse of
# install.sh + `ros2 fairy setup`.
#
# Deliberately NOT removed: your saved missions. /var/ros-fairy/archive (the
# RO-Crate bags + metadata) and /var/ros-fairy/index.db are left in place.
# This script only reminds you where they are; delete them yourself if you
# really want a full wipe.
#
# Usage: ./uninstall.sh [-y|--yes]
#   -y, --yes   skip the confirmation prompt (for scripted use)
set -euo pipefail

# Deliberately not looked up from the installed package (e.g. `python3 -c
# "from ros_fairy.subcommands.setup import SERVICE_NAME"`): this script must
# keep working to clean up a broken/partial install, which is exactly when
# importing ros_fairy might not. tests/unit/test_packaging.py checks these
# against ros_fairy/subcommands/setup.py's SERVICE_NAME/GROUP_NAME and
# ros_fairy/utils/paths.py's DEFAULT_CONFIG_DIR/DEFAULT_VAR_DIR and the
# name of paths.yaml.
SERVICE_NAME="ros-fairy-watchdog.service"
GROUP_NAME="ros-fairy"
CONFIG_DIR="${ROS_FAIRY_CONFIG_DIR:-/etc/ros-fairy}"
# The data folder: ROS_FAIRY_VAR_DIR, else what `setup --var-dir` or
# `move_data` recorded in paths.yaml (read here without ros_fairy), else the
# default — the same order as ros_fairy/utils/paths.py.
DEFAULT_VAR_DIR="/var/ros-fairy"
CONFIGURED_VAR_DIR=""
if [ -f "$CONFIG_DIR/paths.yaml" ]; then
    CONFIGURED_VAR_DIR="$(sed -n "s/^var_dir:[[:space:]]*['\"]\{0,1\}\([^'\"]*\)['\"]\{0,1\}[[:space:]]*$/\1/p" "$CONFIG_DIR/paths.yaml" | head -1)"
fi
VAR_DIR="${ROS_FAIRY_VAR_DIR:-${CONFIGURED_VAR_DIR:-$DEFAULT_VAR_DIR}}"
ARCHIVE_DIR="$VAR_DIR/archive"
INDEX_DB="$VAR_DIR/index.db"
PYTHON="${PYTHON:-python3}"

ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes) ASSUME_YES=1 ;;
        *) echo "Unknown option: $arg (only -y/--yes is accepted)" >&2
           exit 1 ;;
    esac
done

echo "This will remove:"
echo "  - the $SERVICE_NAME systemd service"
echo "  - $CONFIG_DIR (robot identity, watchdog environment)"
echo "  - the '$GROUP_NAME' system group"
echo "  - $VAR_DIR/spool, $VAR_DIR/watchdog.state, $VAR_DIR/ros_processes.json, $VAR_DIR/log"
echo "  - the ros_fairy Python package"
echo
echo "It will NOT remove your saved missions:"
echo "  - $ARCHIVE_DIR"
echo "  - $INDEX_DB"
echo

if [ "$ASSUME_YES" -ne 1 ]; then
    read -r -p "Continue? [y/N] " reply
    case "$reply" in
        [yY]|[yY][eE][sS]) ;;
        *) echo "Cancelled — nothing was changed."
           exit 0 ;;
    esac
fi

if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        echo "Removing this needs root — you may be asked for your password."
        # sudo's env_reset would otherwise drop a relocated install's
        # ROS_FAIRY_CONFIG_DIR/VAR_DIR before the deletion logic below
        # re-derives CONFIG_DIR/VAR_DIR from the (now-empty) environment —
        # silently targeting the default paths instead of the ones just
        # shown in the confirmation banner above.
        exec sudo --preserve-env=ROS_FAIRY_CONFIG_DIR,ROS_FAIRY_VAR_DIR "$0" --yes
    fi
    echo "This needs root (it removes a system service, /etc, and a system" >&2
    echo "group) and sudo isn't available. Re-run this script as root." >&2
    exit 1
fi

echo "Stopping and disabling $SERVICE_NAME..."
systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
UNIT_PATH="/etc/systemd/system/$SERVICE_NAME"
if [ -f "$UNIT_PATH" ]; then
    rm -f "$UNIT_PATH"
    systemctl daemon-reload
fi

if [ -d "$CONFIG_DIR" ]; then
    echo "Removing $CONFIG_DIR..."
    rm -rf "$CONFIG_DIR"
fi

for sub in spool watchdog.state ros_processes.json log; do
    target="$VAR_DIR/$sub"
    if [ -e "$target" ]; then
        echo "Removing $target..."
        rm -rf "$target"
    fi
done

if getent group "$GROUP_NAME" >/dev/null 2>&1; then
    echo "Removing the '$GROUP_NAME' group..."
    groupdel "$GROUP_NAME" 2>/dev/null || \
        echo "Warning: couldn't remove group '$GROUP_NAME' (still someone's primary group?)." >&2
fi

echo "Uninstalling the ros_fairy Python package..."
if ! "$PYTHON" -m pip uninstall -y --break-system-packages ros_fairy 2>/dev/null; then
    "$PYTHON" -m pip uninstall -y ros_fairy || \
        echo "Warning: pip uninstall reported an issue (already uninstalled?)." >&2
fi

echo
echo "Done. ros_fairy has been removed from this system."
if [ -d "$ARCHIVE_DIR" ] || [ -f "$INDEX_DB" ]; then
    echo
    echo "Your saved missions were left untouched:"
    [ -d "$ARCHIVE_DIR" ] && echo "  $ARCHIVE_DIR"
    [ -f "$INDEX_DB" ] && echo "  $INDEX_DB"
    echo "Back them up or delete them yourself if you want a full wipe."
fi
