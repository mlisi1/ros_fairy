#!/usr/bin/env bash
# Take the documentation's terminal screenshots on a fresh sandbox robot and
# copy them to docs/assets/screenshots/:
#
#   tools/sandbox/sandbox.sh reset && tools/sandbox/sandbox.sh install \
#       && tools/sandbox/screenshots/take.sh
#
# It sets the robot up and records missions, so start from a fresh one; run
# `tools/sandbox/sandbox.sh reset` afterwards for a clean robot again.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
NAME=fairy-sandbox-jazzy
OUT=/var/tmp/fairy-screenshots
DEST="$REPO/docs/assets/screenshots"

docker exec "$NAME" rm -rf "$OUT"
docker exec -i -e SHOTS_OUT="$OUT" "$NAME" \
    python3 /src/tools/sandbox/screenshots/take_screenshots.py
mkdir -p "$DEST"
docker exec "$NAME" tar -C "$OUT" -c . | tar -x -C "$DEST"
echo "Screenshots copied to $DEST"
