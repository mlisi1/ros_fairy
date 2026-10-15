"""mission_start without questions: from a script (no terminal), with
--json, with --yes, and from a ros2 launch file."""

import json
import subprocess
import sys
from pathlib import Path

from fairy_sandbox import (
    IDENTITY,
    data_dir,
    ensure_talkers,
    expect,
    finish,
    interact,
    setup_robot,
    sh,
    step,
    wait_until_idle,
)

ANSWERS = ("--operator Ana --goal 'Scripted survey' --location 'North pier' "
           "--environment marine --notes 'started by a script'")


def context() -> dict:
    return json.loads((data_dir() / "spool" / "mission_context.json")
                      .read_text())


ensure_talkers()
if not IDENTITY.is_file():
    step("setup")
    expect(setup_robot() == 0, "setup")
sh("ana", "ros2 fairy mission_abort --yes", check=False, quiet=True)

step("a script starts a mission: no terminal, no questions")
result = sh("ana", f"ros2 fairy mission_start {ANSWERS}", check=False)
expect(result.returncode == 0, f"exit 0 (got {result.returncode})")
intent = context()["intent"]
expect(intent["goal"] == "Scripted survey"
       and intent["environment"] == "marine"
       and intent["notes"] == "started by a script", f"briefing: {intent}")
mission_id = context()["identity"]["mission_id"]

step("it records and saves like any other")
status = interact("ana", "ros2 fairy mission_record", {}, interrupt_after=5)
expect(status == 0, f"mission_record exited {status}")
wait_until_idle()
result = sh("ana", "ros2 fairy mission_close --yes", check=False, quiet=True)
expect(result.returncode == 0, f"mission_close exit {result.returncode}")
listed = json.loads(sh("ana", "ros2 fairy list --json", quiet=True).stdout)
expect(any(m["mission_id"] == mission_id and m["notes"] ==
           "started by a script" for m in listed["missions"]),
       "saved with its briefing")

step("a missing answer is refused, naming the option")
result = sh("ana", "ros2 fairy mission_start --operator Ana --goal x",
            check=False)
expect(result.returncode == 2, f"exit 2 (got {result.returncode})")
expect("--location" in result.stdout, "names --location")

step("--json gives the mission ID")
result = sh("ana", f"ros2 fairy mission_start {ANSWERS} --json",
            check=False, quiet=True)
printed = json.loads(result.stdout)
expect(printed["mission_id"] == context()["identity"]["mission_id"],
       f"printed {printed['mission_id']}")

step("an unfinished mission (no recordings): refused without --yes")
result = sh("ana", "ros2 fairy mission_start --operator Ben --goal y "
            "--location z", check=False)
expect(result.returncode == 2, f"exit 2 (got {result.returncode})")
expect(context()["identity"]["operator_name"] == "Ana", "kept the old one")
result = sh("ana", "ros2 fairy mission_start --operator Ben --goal y "
            "--location z --yes", check=False)
expect(result.returncode == 0, f"--yes: exit 0 (got {result.returncode})")
expect(context()["identity"]["operator_name"] == "Ben", "replaced")

step("from a ros2 launch file")
launch = Path("/home/ana/start_mission.launch.py")
launch.write_text('''from launch import LaunchDescription
from launch.actions import ExecuteProcess


def generate_launch_description():
    return LaunchDescription([ExecuteProcess(
        cmd=["ros2", "fairy", "mission_start", "--yes",
             "--operator", "Launch", "--goal", "Started from launch",
             "--location", "Lab"],
        output="screen")])
''')
subprocess.run(["chown", "ana:ana", str(launch)], check=True)
result = sh("ana", f"timeout 60 ros2 launch {launch}", check=False)
expect("process has finished cleanly" in result.stdout + result.stderr,
       "the launch file's process finished cleanly")
expect(context()["intent"]["goal"] == "Started from launch",
       "mission started by the launch file")
sh("ana", "ros2 fairy mission_abort --yes", check=False, quiet=True)
sys.exit(finish())
