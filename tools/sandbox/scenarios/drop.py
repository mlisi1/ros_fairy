"""`ros2 fairy drop`: take recordings out of an open mission by the number
mission_status shows; spool ones deleted, one made elsewhere only detached;
numbers stay put; adopt and drop undo each other; the rest saves."""

import json
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

OUTSIDE = Path("/home/ana/outside_run")


def spool_bags() -> list[Path]:
    return sorted(p for p in (data_dir() / "spool" / "bags").iterdir()
                  if p.is_dir())


def listed() -> list[str]:
    harvest = json.loads((data_dir() / "spool" / "harvest.json").read_text())
    return [b["path"] for b in harvest.get("bags", [])]


ensure_talkers()
if not IDENTITY.is_file():
    step("setup")
    expect(setup_robot() == 0, "setup")
sh("ana", "ros2 fairy mission_abort --yes", check=False, quiet=True)
sh("ana", f"rm -rf {OUTSIDE}", check=False, quiet=True)

step("a mission with three recordings, and one made elsewhere")
sh("ana", "ros2 fairy mission_start --operator Ana --goal 'Drop test' "
   "--location Lab --yes", quiet=True)
for _ in range(3):
    interact("ana", "ros2 fairy mission_record --yes", {}, interrupt_after=4)
    wait_until_idle()
interact("ana", f"ros2 bag record -o {OUTSIDE} /fix", {}, interrupt_after=4,
         interrupt="ros2 bag record")
wait_until_idle()
spool = spool_bags()
expect(len(spool) == 3 and str(OUTSIDE) in listed(),
       f"{len(spool)} in the spool, the outside one detected")

step("mission_status numbers them #1 to #4")
result = sh("ana", "ros2 fairy mission_status", check=False, quiet=True)
expect(all(f" #{n}  " in result.stdout for n in range(1, 5)),
       "#1..#4 shown")
expect("ros2 fairy drop <#>" in result.stdout, "says how to take one out")

step("without saying which, and no terminal: the list, exit 2")
result = sh("ana", "ros2 fairy drop", check=False)
expect(result.returncode == 2, f"exit 2 (got {result.returncode})")
expect("#4" in result.stdout and "outside_run" in result.stdout,
       "lists all four")

step("drop #2: deleted, and the others keep their numbers")
result = sh("ana", "ros2 fairy drop 2 --yes", check=False)
expect(result.returncode == 0, f"exit 0 (got {result.returncode})")
expect(not spool[1].exists(), "folder deleted")
expect(str(spool[1]) not in listed(), "record removed")
result = sh("ana", "ros2 fairy mission_status", check=False, quiet=True)
expect(" #2  " not in result.stdout and " #3  " in result.stdout
       and " #4  " in result.stdout, "#1, #3, #4 left")

step("choose interactively, and confirm")
status = interact("ana", "ros2 fairy drop",
                  {r"Which recording should be taken out\?": "#1",
                   r"Delete recording #1 .* permanently\?": "y"})
expect(status == 0, f"exit {status}")
expect(not spool[0].exists(), "#1 deleted")

step("drop #4, made elsewhere: it stays on disk")
result = sh("ana", "ros2 fairy drop 4 --yes", check=False)
expect(result.returncode == 0, f"exit 0 (got {result.returncode})")
expect(OUTSIDE.is_dir(), "still on disk")
expect(str(OUTSIDE) not in listed(), "no longer in the mission")
expect("ros2 fairy adopt" in result.stdout, "says how to attach it again")

step("adopt it back: a new number, and drop undoes it")
result = sh("ana", f"ros2 fairy adopt {OUTSIDE}", check=False)
expect(result.returncode == 0 and "as #5" in result.stdout,
       "adopted as #5 (4 isn't reused)")
expect("ros2 fairy drop 5" in result.stdout, "says how to undo it")
result = sh("ana", "ros2 fairy drop 5 --yes", check=False, quiet=True)
expect(result.returncode == 0 and OUTSIDE.is_dir()
       and str(OUTSIDE) not in listed(), "dropped again, still on disk")

step("the mission saves with the one recording left")
result = sh("ana", "ros2 fairy mission_close --yes", check=False, quiet=True)
expect(result.returncode == 0, f"mission_close exit {result.returncode}")
crate = max((data_dir() / "archive").glob("2*"))
saved = sorted(p.name for p in (crate / "bags").iterdir())
expect(saved == [spool[2].name], f"saved recordings: {saved}")
record = json.loads((crate / "mission_record.json").read_text())
expect(all("recording_id" not in b for b in record["bags"]),
       "the saved record has no open-mission numbers")
sh("ana", "ros2 fairy verify 1")
sys.exit(finish())
