"""Wrong uses, as operators make them: commands before setup, in the wrong
order, during a recording, with wrong values. Each must finish, stop with
the expected exit code and say what's wrong in plain words: no traceback,
no "something went wrong that ros-fairy didn't expect", and nothing lost.

Run on a fresh robot (the first part is before setup):

    tools/sandbox/sandbox.sh reset && tools/sandbox/sandbox.sh install
    tools/sandbox/sandbox.sh run misuse.py
"""

import re
import subprocess
import sys
import time

from fairy_sandbox import (
    IDENTITY,
    expect,
    finish,
    setup_robot,
    sh,
    step,
    wait_until_idle,
)

UNEXPECTED = "didn't expect"


def wrong(user: str, command: str, code: int, says: str,
          timeout: int = 90) -> str:
    """Run ``command`` (no terminal); check it ends with ``code`` and says
    ``says`` (a regex, whitespace-insensitive), cleanly."""
    login = ["sudo", "-u", user, "-H", "bash", "-lc",
             f"cd && COLUMNS=200 NO_COLOR=1 {command}"]
    print(f"$ ({user}) {command}")
    try:
        result = subprocess.run(login, capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        expect(False, f"`{command}` hung for {timeout} s")
        return ""
    out = result.stdout + result.stderr
    flat = " ".join(re.sub(r"[│╭╮╰╯─]", " ", out).split())
    print("    " + flat[:300])
    ok = result.returncode == code and re.search(says, flat) is not None \
        and "Traceback" not in out and UNEXPECTED not in out
    expect(ok, f"`{command}` exits {code} and says /{says}/"
               + ("" if ok else f" (got {result.returncode})"))
    return flat


def fairy(user: str, args: str, code: int, says: str, **kw) -> str:
    return wrong(user, f"ros2 fairy {args}", code, says, **kw)


def record(user: str, seconds: int, args: str = "--yes") -> str:
    """mission_record, stopped like Ctrl-C after ``seconds``."""
    return f"timeout -s INT {seconds} ros2 fairy mission_record {args}"


START = "mission_start --json --operator Ana --location Lab --goal"

# --------------------------------------------------------------------------
if IDENTITY.is_file():
    sys.exit("misuse.py starts before setup: run it on a fresh robot "
             "(sandbox.sh reset && sandbox.sh install).")

step("before setup")
NOT_SET_UP = "isn't set up yet.*ros2 fairy setup"
fairy("ana", "mission_status", 0, NOT_SET_UP)
fairy("ana", f"{START} G", 1, NOT_SET_UP)
wrong("ana", record("ana", 8), 1, NOT_SET_UP)
fairy("ana", "mission_close --yes", 1, NOT_SET_UP)
fairy("ana", "list", 0, "No missions have been saved")
fairy("ana", "verify", 1, "No missions have been saved")
fairy("ana", "setup --robot-name x --yes", 1, "isn't set up yet")
fairy("ben", "add_operator cleo", 1, "doesn't exist yet.*setup")

step("setup")
expect(setup_robot() == 0, "setup")

# --------------------------------------------------------------------------
step("wrong order: nothing open")
fairy("ana", "mission_close --yes", 1, "no mission in progress")
fairy("ana", "mission_abort --yes", 0, "no mission in progress")
fairy("ana", "drop 1 --yes", 0, "no mission in progress")
fairy("ana", "diff", 1, "No missions have been saved")
wrong("ana", record("ana", 8, ""), 2,
      "No mission briefing yet.*no terminal to type it in")

step("wrong order: a briefing, then another")
fairy("ana", f"{START} First", 0, "mission_id")
fairy("ana", f"{START} Second", 0, "mission_id")  # replaces the empty one
fairy("ana", "mission_start", 2, "already an unfinished mission.*no terminal")
fairy("ana", "mission_close --yes", 1, "nothing recorded yet.*adopt")

step("during a recording")
background = subprocess.Popen(
    ["sudo", "-u", "ana", "-H", "bash", "-lc", "cd && " + record("ana", 30)],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(8)
STOP_FIRST = "still in progress|recording is in progress"
wrong("ana", record("ana", 8, ""), 2,
      "already running.*Start another one.*no terminal to type it in")
fairy("ana", "mission_close --yes", 1, STOP_FIRST)
fairy("ana", "mission_abort --yes", 1, STOP_FIRST)
fairy("ana", "drop 1 --yes", 1, STOP_FIRST)
fairy("ana", f"{START} Third", 1, STOP_FIRST)
fairy("ana", "move_data /srv/fairy --yes", 1, "A mission is open")
background.wait()
wait_until_idle()

step("after recording, before saving")
fairy("ana", f"{START} Fourth", 1, "still has 1 recording")
fairy("ana", "drop 7 --yes", 1, "no recording 7")
fairy("ana", "drop abc --yes", 1, "no recording abc")
fairy("ana", "move_data /srv/fairy --yes", 1, "A mission is open")
fairy("ana", "diff", 1, "No missions have been saved")
fairy("ana", "mission_close --yes --note misuse", 0, "Mission saved")
fairy("ana", "mission_close --yes", 1, "no mission in progress")

step("accounts that aren't operators")
NOT_OPERATOR = "isn't in the 'ros-fairy' group.*add_operator cleo"
fairy("cleo", f"{START} X", 1, NOT_OPERATOR)
wrong("cleo", record("cleo", 6), 1, NOT_OPERATOR)
fairy("cleo", "list", 1, "permission to read the mission index")
fairy("ben", "add_operator cleo", 1, "needs root")

# --------------------------------------------------------------------------
step("wrong values: finding missions")
fairy("ana", "verify 99", 1, "only 1 saved mission; 99 is out of range")
fairy("ana", "verify 0", 1, "must be 1 or higher")
fairy("ana", "verify m-20990101-000000-dead", 1, "Can't find a mission")
fairy("ana", "verify /etc", 1, "Can't find a mission")
fairy("ana", "verify /etc/passwd", 1, "isn't a .zip or .tar bundle")
fairy("ana", "diff", 1, "only one has been saved")
fairy("ana", "diff 1 1", 1, "the same mission")
fairy("ana", "mission_edit 99 --goal x --yes", 1, "out of range")
fairy("ana", "mission_delete m-20990101-000000-dead", 1, "no saved mission")
fairy("ana", "repair 99", 1, "out of range")

step("wrong values: list")
fairy("ana", "list --limit 0", 1, "--limit must be 1 or more")
fairy("ana", "list --limit abc", 2, "invalid int value")
fairy("ana", "list --since 2026-13-45", 1, "isn't a date")
fairy("ana", "list --since yesterday", 1, "isn't a date")
fairy("ana", "list --since 2027-01-01 --until 2026-01-01", 1,
      "is after --until")
fairy("ana", "list --quality bogus", 2, "invalid choice")

step("wrong values: export")
fairy("ana", "export 1 -o /root/x.zip", 1, "can't write to /root/x.zip")
fairy("ana", "export 1 -o ~/x.zip --format tar", 1,
      r"is a \.zip name, but the bundle would be tar")
fairy("ana", "export 1 -o ~/x.tar --compress", 1, "would be tar.zst")
fairy("ana", "export 1 --exclude m-x", 1, "--exclude only applies")
fairy("ana", "export --all --today", 1, "can't be used together")
fairy("ana", "export 1 -o ~/named.tar.gz", 0, "Exported")
wrong("ana", "python3 -c \"import tarfile; "
      "tarfile.open('named.tar.gz', 'r:gz').getnames(); print('gzip ok')\"",
      0, "gzip ok")

step("wrong values: editing and deleting")
fairy("ana", "mission_edit 1 --goal '' --yes", 1, "goal can't be empty")
fairy("ana", f"mission_edit 1 --goal {'x' * 300} --yes", 1,
      "at most 280 characters")
fairy("ana", "mission_edit 1 --redact --goal y --yes", 2,
      "--redact needs your name")
fairy("ana", "mission_edit 1 --yes", 2, "Say what to change")
fairy("ana", "mission_delete --all", 1, "needs an interactive terminal")

step("wrong values: recordings and folders")
fairy("ana", "adopt /nonexistent", 1, "no folder at /nonexistent")
fairy("ana", "adopt /etc", 1, "doesn't look like a recording")
fairy("ana", "move_data relative/dir --yes", 1, "isn't an absolute path")
fairy("ana", "move_data /etc/ros-fairy/inside --yes", 1,
      "inside the configuration folder")
fairy("ana", "move_data /etc --yes", 1, "isn't an empty folder")
fairy("ana", "add_operator nosuchuser", 1, "no account with that name")

step("wrong values: setup")
UNCHANGED = "Nothing was changed"
fairy("ana", "setup --add-sensor garbage --yes", 1, f"KEY=VALUE.*{UNCHANGED}")
fairy("ana", "setup --remove-sensor nope --yes", 1, "no sensor 'nope'")
fairy("ana", "setup --contact-email bad --yes", 1, "email address")
fairy("ana", f"setup --robot-name {'r' * 60} --yes", 1, "40 characters")
fairy("ana", "setup --storage bogus --yes", 1,
      f"no storage 'bogus' \\(it has mcap, sqlite3\\).*{UNCHANGED}")
fairy("ana", "setup --license 'not a licence' --yes", 1, "SPDX")
fairy("ana", "setup --var-dir relative --yes", 1, "isn't an absolute path")
fairy("ana", "setup --show --robot-name x", 1, "can't be combined")
fairy("ana", "setup --from-container nosuch", 1, "No running container")

step("wrong values: a new mission")
fairy("ana", "mission_start --json --operator A --location L", 2,
      "Missing answers: give --goal")
fairy("ana", f"mission_start --json --operator A --location L "
      f"--goal {'x' * 300}", 2, "at most 280 characters")
fairy("ana", f"{START} 'Recorder options'", 0, "mission_id")
wrong("ana", record("ana", 6, "-o /tmp/elsewhere --yes"), 1,
      "-o/--output can't be used")
wrong("ana", record("ana", 6, "--storage bogus --yes"), 2, "invalid choice")
fairy("ana", "mission_abort --yes", 0, "aborted")

step("nothing was lost")
listed = sh("ana", "ros2 fairy list --json", quiet=True)
expect('"goal": "Second"' in listed.stdout,
       "the one mission saved is still listed")
sh("ana", "ros2 fairy verify 1")
fairy("ana", "doctor", 0, "READY")
sys.exit(finish())
