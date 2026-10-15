"""mission_edit: change a saved mission's briefing; the crate stays
consistent (verify, with the full RO-Crate check), the mission list and the
export status follow, an archive changed by hand is refused."""

import json
import re
import sqlite3
import subprocess
import sys

from fairy_sandbox import (
    IDENTITY,
    data_dir,
    ensure_talkers,
    expect,
    finish,
    interact,
    record_mission,
    setup_robot,
    sh,
    step,
)


def record() -> dict:
    crate = max((data_dir() / "archive").glob("2*"))
    return json.loads((crate / "mission_record.json").read_text())


ensure_talkers()
if not IDENTITY.is_file():
    step("setup")
    expect(setup_robot() == 0, "setup")
subprocess.run(["python3", "-m", "pip", "install", "-q",
                "--break-system-packages", "rocrate"], capture_output=True)

step("a saved mission, exported")
record_mission(goal="Survey the nrth pier")
mission_id = record()["identity"]["mission_id"]
sh("ana", "mkdir -p ~/out && ros2 fairy export 1 -o ~/out/", quiet=True)

step("fix the place and add notes, from options")
result = sh("ana", "ros2 fairy mission_edit 1 --goal 'Survey the north pier' "
            "--notes 'gusty wind after 11:00' --yes", check=False)
expect(result.returncode == 0, f"exit 0 (got {result.returncode})")
expect("no longer matches" in result.stdout, "the old export is stale")
suggested = re.search(r"ros2 fairy export \S+ -o \S+ --force",
                      " ".join(result.stdout.split()))
expect(suggested is not None, "says how to export it again")
saved = record()
expect(saved["intent"]["goal"] == "Survey the north pier", "goal changed")
[made] = saved["provenance"]["edits"]
goal = made["changes"]["goal"]
expect((goal["before"], goal["after"]) == ("Survey the nrth pier",
                                           "Survey the north pier"),
       "the edit is recorded with before and after")

step("verify, with the full RO-Crate check")
result = sh("ana", "ros2 fairy verify 1", check=False, quiet=True)
expect(result.returncode == 0, f"verify exit 0 (got {result.returncode})")
expect("Briefing edited after saving: 1 time" in result.stdout,
       "verify reports the edit")
expect("install 'rocrate'" not in result.stdout,
       "the RO-Crate was deep-checked with rocrate")

step("the mission list and the export status follow")
listed = json.loads(sh("ana", "ros2 fairy list --search 'gusty' --json",
                       quiet=True).stdout)["missions"]
expect([m["mission_id"] for m in listed] == [mission_id], "found by notes")
con = sqlite3.connect(data_dir() / "index.db")
exported = {r[0] for r in con.execute("SELECT mission_id FROM exports")}
con.close()
expect(mission_id not in exported, "export --all will pick it up again")
command = suggested.group(0) if suggested else "false"
result = sh("ana", command, check=False)
expect(result.returncode == 0, f"the suggested command works: {command}")

step("interactively: Enter keeps, - clears")
status = interact("ana", "ros2 fairy mission_edit 1", {
    r"What's your name\?": "",
    r"what is this mission trying to do\?": "Survey the north pier at dawn",
    r"Where are you\?": "",
    r"environment like\?": "",
    r"Anything else worth noting\?": "-",
    r"Save these changes to the mission\?": "y"})
expect(status == 0, f"exit {status}")
saved = record()
expect(saved["intent"]["goal"] == "Survey the north pier at dawn"
       and saved["intent"]["notes"] is None
       and len(saved["provenance"]["edits"]) == 2, "second edit saved")
sh("ana", "ros2 fairy verify 1")

step("--redact: a sudoer with a password, a wrong password first")
subprocess.run("id dora >/dev/null 2>&1 || useradd -m -s /bin/bash "
               "-G sudo,ros-fairy dora; echo dora:s3cret | chpasswd",
               shell=True, check=True)
before = record()
status = interact("dora", "ros2 fairy mission_edit 1 --notes 'cleaned up' "
                  "--redact", {r"Your full name": "Dora Admin",
                               r"Redact and save\?": "y",
                               r"\[sudo\] password for dora": "wrong"})
expect(status != 0, f"refused with a wrong password (exit {status})")
expect(record() == before, "nothing changed")

step("--redact with the right password")
crate = max((data_dir() / "archive").glob("2*"))
old_goal_text = "nrth pier"  # in the history of the first edit's goal
status = interact("dora", "ros2 fairy mission_edit 1 --notes 'cleaned up' "
                  "--redact --by 'Dora Admin'",
                  {r"Redact and save\?": "y",
                   r"\[sudo\] password for dora": "s3cret"})
expect(status == 0, f"exit {status}")
saved = record()
last = saved["provenance"]["edits"][-1]
expect(last["redacted"] and last["by"] == "Dora Admin"
       and last["account"] == "dora", f"recorded: {last['by']}, "
       f"{last['account']}")
expect(saved["intent"]["notes"] == "cleaned up", "new notes saved")
crate = max((data_dir() / "archive").glob("2*"))
text = "".join((crate / n).read_text() for n in
               ("mission_record.json", "README.md", "ro-crate-metadata.json"))
index_bytes = (data_dir() / "index.db").read_bytes()
expect("gusty" not in text and b"gusty" not in index_bytes,
       "the old notes are gone from the crate and the index")
expect(old_goal_text in text, "fields not redacted keep their history")
log = subprocess.run(["journalctl", "_COMM=sudo", "--no-pager", "-o", "cat"],
                     capture_output=True, text=True).stdout
expect("mission_edit" in log and "--redact" in log and "dora" in log,
       "sudo's log has the command and the account")
result = sh("ana", "ros2 fairy verify 1", check=False, quiet=True)
expect(result.returncode == 0, "verify passes after the redaction")

step("an archive changed by hand is not edited")
crate = max((data_dir() / "archive").glob("2*"))
(crate / "harvest" / "harvest.json").write_text("{}")
result = sh("ana", "ros2 fairy mission_edit 1 --notes x --yes", check=False)
expect(result.returncode == 1 and "changed after it was saved"
       in result.stdout, "refused, pointing at verify")
expect(record()["intent"]["notes"] == "cleaned up", "nothing edited")
sys.exit(finish())
