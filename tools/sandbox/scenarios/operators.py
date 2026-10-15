"""Operator accounts on a fresh robot: setup adds the ones it's told about,
an account outside the group is refused with a way out, add_operator adds
more later. Run on a fresh robot (`sandbox.sh reset && sandbox.sh install`).
"""

import subprocess
import sys

from fairy_sandbox import (
    BRIEFING,
    IDENTITY,
    expect,
    finish,
    group_members,
    interact,
    setup_robot,
    sh,
    step,
    wait_until_idle,
)

if IDENTITY.is_file():
    print("This scenario needs a fresh robot: sandbox.sh reset, then install.")
    sys.exit(2)

step("setup asks for other operators (one typo first)")
status = setup_robot({r"Other accounts that will record missions":
                      ["ben nobdy", "ben"]})
expect(status == 0, f"setup exited {status}")
expect(group_members() == {"ana", "ben"},
       f"ros-fairy group is ana and ben (got {sorted(group_members())})")

step("setup --operator with an unknown account stops at once")
result = sh("ana", "ros2 fairy setup --operator no_such_user", check=False)
expect(result.returncode == 1, f"exit 1 (got {result.returncode})")
expect("no account named no_such_user" in result.stdout, "says why")

step("cleo isn't an operator yet: refused before any question")
result = sh("cleo", "ros2 fairy mission_start", check=False)
expect(result.returncode == 1, f"exit 1 (got {result.returncode})")
expect("What's your name" not in result.stdout, "no briefing question asked")
expect("ros2 fairy add_operator cleo" in result.stdout,
       "tells her what to ask for")

step("ben records a mission")
status = interact("ben", "ros2 fairy mission_start",
                  {**BRIEFING, r"What's your name\?": "Ben"})
expect(status == 0, f"mission_start exited {status}")
status = interact("ben", "ros2 fairy mission_record", {}, interrupt_after=5)
expect(status == 0, f"mission_record exited {status}")
wait_until_idle()
status = interact("ben", "ros2 fairy mission_close --yes", {}, timeout=180)
expect(status == 0, f"mission_close exited {status}")

step("ana adds cleo with add_operator")
result = sh("ana", "ros2 fairy add_operator cleo ben", check=False)
expect(result.returncode == 0, f"exit 0 (got {result.returncode})")
expect("cleo added" in result.stdout, "cleo added")
expect("ben was already an operator" in result.stdout, "ben already there")
expect("next login" in result.stdout, "explains when it applies")
expect("cleo" in group_members(), "cleo is in the group")

step("cleo, at her next login, can see the missions")
result = sh("cleo", "ros2 fairy list --json", check=False, quiet=True)
expect(result.returncode == 0 and '"total": 1' in result.stdout,
       "list works for cleo")

step("a terminal opened before add_operator is told about newgrp")
# A login shell without the new group, like a terminal cleo opened before
# she was added.
stale = subprocess.run(
    ["setpriv", "--reuid=cleo", "--regid=cleo", "--clear-groups",
     "env", "-i", "HOME=/home/cleo", "USER=cleo", "LOGNAME=cleo",
     "bash", "-lc", "COLUMNS=160 ros2 fairy mission_start"],
    capture_output=True, text=True, stdin=subprocess.DEVNULL, cwd="/")
print(stale.stdout + stale.stderr)
expect(stale.returncode == 1, f"exit 1 (got {stale.returncode})")
expect("newgrp ros-fairy" in stale.stdout, "suggests newgrp")

step("add_operator with a name that doesn't exist")
result = sh("ana", "ros2 fairy add_operator no_such_user", check=False)
expect(result.returncode == 1, f"exit 1 (got {result.returncode})")
expect("no account with that name" in result.stdout, "says why")
sys.exit(finish())
