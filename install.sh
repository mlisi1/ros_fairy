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
# permissions reason — so on a setup that doesn't need it, no sudo prompt
# ever appears.
#
# Then it offers to install the optional tools the harvest uses (lsusb,
# lspci, v4l2-ctl, ...; the list is ros_fairy/utils/harvest_tools.py).
#
# Usage: ./install.sh [--dev] [--with-deps | --no-deps]
#   --dev         also install the dev/test extras (pytest, ruff, mypy, rocrate)
#   --with-deps   install the missing harvest tools without asking
#   --no-deps     don't install them, and don't ask
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TARGET="$REPO_DIR"
DEPS=ask
for arg in "$@"; do
    case "$arg" in
        --dev) TARGET="$REPO_DIR[dev]" ;;
        --with-deps) DEPS=yes ;;
        --no-deps) DEPS=no ;;
        -h|--help) sed -n '20,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg (see --help)" >&2; exit 2 ;;
    esac
done

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

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        echo "Not root and no sudo available — re-run this script as root." >&2
        return 1
    fi
}

# pip install "$@" system-wide, escalating only when that is the problem.
pip_install() {
    # --no-user: without write access to the system site-packages, pip may
    # quietly install into ~/.local instead of failing (when PEP 668 doesn't
    # stop it first). The watchdog service (root) can't use that copy, and
    # we'd never escalate.
    if python3 -m pip install --no-user "$@" >"$LOG" 2>&1; then
        cat "$LOG"
    elif grep -qiE "read-only file system|errno 30|EROFS" "$LOG"; then
        # sudo can't fix this — the filesystem itself refuses writes (common
        # on robot images with a read-only rootfs overlay). Retrying as root
        # would just fail the same way, so say so instead of retrying.
        echo "The filesystem pip is trying to write to is read-only, so sudo" >&2
        echo "won't help. Remount it read-write and run this script again." >&2
        cat "$LOG" >&2
        return 1
    elif grep -qiE "permission denied|errno 13|externally-managed-environment" "$LOG"; then
        echo "System-wide install needs root — retrying." >&2
        as_root python3 -m pip install --break-system-packages "$@" || {
            cat "$LOG" >&2
            return 1
        }
    else
        cat "$LOG" >&2
        return 1
    fi
}

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
pip_install "$TARGET" || exit 1

# A copy in ~/.local (left by an older install.sh) would shadow this one in
# this account's shell, while the service runs the new one.
SHADOW="$(cd / && python3 -c 'import os, ros_fairy; print(os.path.dirname(ros_fairy.__file__))' 2>/dev/null || true)"
case "$SHADOW" in
    "$HOME"/*)
        echo "" >&2
        echo "Warning: an older ros_fairy in $SHADOW is used in your shell" >&2
        echo "instead of the one just installed. Remove it (as $(id -un)," >&2
        echo "without sudo) with:" >&2
        echo "    python3 -m pip uninstall ros_fairy" >&2
        ;;
esac

# The optional tools the harvest uses. Asked about after the package itself,
# whose list of them this reads.
tools() { (cd / && python3 -m ros_fairy.utils.harvest_tools "$1"); }
MISSING_APT="$(tools apt)"
MISSING_PIP="$(tools pip)"
if [ -n "$MISSING_APT$MISSING_PIP" ]; then
    echo ""
    echo "Some tools the recording assistant uses to describe the robot are"
    echo "missing. Without them, these details are left out of every mission:"
    tools list
    if [ "$DEPS" = ask ]; then
        if [ -t 0 ]; then
            read -r -p "Install them now? [Y/n] " answer || answer=n
            case "${answer:-y}" in
                [Yy]*) DEPS=yes ;;
                *) DEPS=no ;;
            esac
        else
            echo "(No terminal to ask on: skipping. Use --with-deps to install them.)"
            DEPS=no
        fi
    fi
    if [ "$DEPS" = yes ]; then
        if [ -n "$MISSING_APT" ]; then
            if ! command -v apt-get >/dev/null 2>&1; then
                echo "apt-get isn't available here; install these with your" >&2
                echo "package manager: $MISSING_APT" >&2
            # word splitting of the package list is intended
            # shellcheck disable=SC2086
            elif ! as_root apt-get install -y $MISSING_APT; then
                echo "Retrying after refreshing the package lists..." >&2
                # shellcheck disable=SC2086
                as_root apt-get update && as_root apt-get install -y $MISSING_APT \
                    || echo "Couldn't install $MISSING_APT; ros_fairy works without them." >&2
            fi
        fi
        if [ -n "$MISSING_PIP" ]; then
            # shellcheck disable=SC2086
            pip_install $MISSING_PIP \
                || echo "Couldn't install $MISSING_PIP; ros_fairy works without it." >&2
        fi
    else
        echo "Skipped. Install them later with ./install.sh --with-deps;"
        echo "\`ros2 fairy doctor\` lists whatever is still missing."
    fi
else
    echo "The tools the recording assistant uses to describe the robot are"
    echo "all installed."
fi

cat <<'EOF'

Installed. Next:
    ros2 fairy setup      # configure this robot
    ros2 fairy doctor     # confirm it's ready to capture

setup runs as you — no sudo needed to start it. It only asks for your
password if/when it actually needs to write /etc/ros-fairy and install the
watchdog service.
EOF
