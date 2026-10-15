#!/usr/bin/env bash
# Try ROS F.A.I.R.y on a throwaway robot: a container with ROS 2, systemd and
# three accounts (ana: engineer with sudo; ben, cleo: operators).
#
# Usage: tools/sandbox/sandbox.sh <command> [args]
#
#   up            build the image if needed and start the container
#   install       copy this checkout in and run ./install.sh as ana
#   setup         run `ros2 fairy setup` as ana with scripted answers
#   run SCENARIO  run tools/sandbox/scenarios/SCENARIO (as root; it switches
#                 user itself), e.g. `run smoke.py`
#   shell [USER]  a login shell (default: ana)
#   exec USER CMD...  run one command in a login shell as USER
#   talker        start the test publishers (/fix, /scan) as systemd units
#   logs          the watchdog's journal
#   down          stop and remove the container (the image is kept)
#   reset         down, then up: a fresh robot
#
# The checkout is mounted read-only at /src; `install` copies it to
# /home/ana/ros_fairy, so the real install path is exercised. Re-run
# `install` after changing the code.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
# The supported distro. Others come after the first release.
DISTRO=jazzy
IMAGE="fairy-sandbox:$DISTRO"
NAME="fairy-sandbox-$DISTRO"

usage() { sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

running() { [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" = true ]; }

need_running() {
    running || { echo "The sandbox isn't running: $0 up" >&2; exit 1; }
}

as_user() {  # as_user USER CMD...: a login shell, so ROS is sourced
    local user="$1"
    shift
    docker exec -i -w "/home/$user" "$NAME" su - "$user" -c "$*"
}

wait_for_boot() {
    for _ in $(seq 60); do
        state="$(docker exec "$NAME" systemctl is-system-running 2>/dev/null || true)"
        case "$state" in
            running|degraded) return 0 ;;
        esac
        sleep 0.5
    done
    echo "systemd didn't finish booting (state: ${state:-unknown})" >&2
    return 1
}

cmd_up() {
    if running; then
        echo "$NAME is already running."
        return
    fi
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker build -q -t "$IMAGE" --build-arg "BASE=ros:$DISTRO-ros-base" "$HERE" >/dev/null
    # systemd needs its own cgroup tree and a writable /run.
    docker run -d --name "$NAME" --hostname "fairy-$DISTRO" \
        --privileged --cgroupns=private \
        --tmpfs /run --tmpfs /run/lock --tmpfs /tmp:exec \
        -v "$REPO:/src:ro" \
        "$IMAGE" >/dev/null
    wait_for_boot
    echo "$NAME is up (ROS 2 $DISTRO). Next: $0 install"
}

cmd_install() {
    need_running
    docker exec "$NAME" rsync -a --delete \
        --exclude build --exclude '*.egg-info' --exclude site \
        --exclude __pycache__ --exclude .pytest_cache \
        /src/ /home/ana/ros_fairy/
    docker exec "$NAME" chown -R ana:ana /home/ana/ros_fairy
    as_user ana "cd ~/ros_fairy && ./install.sh"
}

cmd_down() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    echo "$NAME removed."
}

[ $# -ge 1 ] || usage 1
command="$1"
shift
case "$command" in
    up) cmd_up ;;
    install) cmd_install ;;
    setup)
        need_running
        docker exec -i "$NAME" python3 /src/tools/sandbox/scenarios/setup_robot.py "$@" ;;
    run)
        need_running
        [ $# -ge 1 ] || usage 1
        script="$1"
        shift
        docker exec -i "$NAME" python3 "/src/tools/sandbox/scenarios/$script" "$@" ;;
    shell)
        need_running
        docker exec -it -w "/home/${1:-ana}" "$NAME" su - "${1:-ana}" ;;
    exec)
        need_running
        [ $# -ge 2 ] || usage 1
        as_user "$@" ;;
    talker)
        need_running
        docker exec -w /src/tools/sandbox/scenarios "$NAME" python3 -c \
            "import fairy_sandbox; fairy_sandbox.ensure_talkers()" ;;
    logs)
        need_running
        docker exec "$NAME" journalctl -u ros-fairy-watchdog --no-pager "$@" ;;
    down) cmd_down ;;
    reset) cmd_down; cmd_up ;;
    -h|--help|help) usage ;;
    *) usage 1 ;;
esac
