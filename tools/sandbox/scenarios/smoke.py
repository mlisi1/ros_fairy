"""One mission end to end, as on a real robot: setup (if needed), doctor,
briefing, recording, save, list, verify, export."""

import json
import sys

from fairy_sandbox import (
    BRIEFING,
    IDENTITY,
    ensure_talkers,
    expect,
    finish,
    interact,
    setup_robot,
    sh,
    step,
    wait_until_idle,
)

ensure_talkers()
if not IDENTITY.is_file():
    step("setup")
    expect(setup_robot() == 0, "setup")

step("doctor")
sh("ana", "ros2 fairy doctor", check=False)

step("mission_start")
status = interact("ana", "ros2 fairy mission_start",
                  {**BRIEFING, r"replace it\?": "y",
                   r"Include (them|it) in this new mission\?": "n"})
expect(status == 0, f"mission_start exited {status}")

step("mission_record (6 s, then Ctrl-C)")
status = interact("ana", "ros2 fairy mission_record", {},
                  interrupt_after=6)
expect(status == 0, f"mission_record exited {status}")

# The watchdog needs a moment to notice the recording ended; mission_close
# run in that window refuses ("still in progress").
wait_until_idle()

step("mission_close")
status = interact("ana", "ros2 fairy mission_close --yes --note 'smoke'", {},
                  timeout=180)
expect(status == 0, f"mission_close exited {status}")

step("list, verify, export")
listed = sh("ana", "ros2 fairy list --search 'smoke test' --json", quiet=True)
missions = json.loads(listed.stdout)["missions"]
expect(any(m["goal"] == "Sandbox smoke test" for m in missions),
       "list --search finds it")
sh("ana", "ros2 fairy verify 1")
sh("ana", "out=$(mktemp -d) && ros2 fairy export 1 -o $out/")
sys.exit(finish())
