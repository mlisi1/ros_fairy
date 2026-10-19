"""install.sh offers the harvest's optional tools; doctor warns while they
are missing. Run right after `sandbox.sh reset && sandbox.sh install` (that
install has no terminal, so it skips them): the image leaves them out."""

import json
import sys

from fairy_sandbox import expect, finish, interact, sh, step

INSTALL = "cd ~/ros_fairy && ./install.sh"


def missing() -> str:
    return sh("ana", "cd / && python3 -m fairy.utils.harvest_tools list",
              quiet=True).stdout


def doctor_check() -> dict:
    out = sh("ana", "ros2 fairy doctor --json", check=False, quiet=True).stdout
    checks = json.loads(out)["checks"]
    return next(c for c in checks if "Harvest tools" in c["title"])


step("a fresh robot lacks them")
before = missing()
print(before)
for tool in ("lsusb", "lspci", "v4l2-ctl", "rocrate"):
    expect(tool in before, f"{tool} reported missing")
check = doctor_check()
expect(check["status"] == "warn", "doctor warns")
expect("sudo apt install" in check["hint"], f"doctor hint: {check['hint']}")

step("no terminal: skipped, with a hint")
result = sh("ana", INSTALL, quiet=True)
expect("No terminal to ask on" in result.stdout, "says why it didn't ask")
expect("--with-deps" in result.stdout, "says how to install them")
expect(missing() == before, "nothing installed")

step("--no-deps: skipped without asking")
result = sh("ana", f"{INSTALL} --no-deps", quiet=True)
expect("Skipped" in result.stdout, "skipped")
expect("Install them now" not in result.stdout, "didn't ask")

step("on a terminal, Enter installs them (default yes)")
status = interact("ana", INSTALL, {r"Install them now\? \[Y/n\]": ""},
                  timeout=600)
expect(status == 0, f"install.sh exited {status}")
expect(missing() == "", f"nothing missing any more (left: {missing()!r})")
expect(doctor_check()["status"] == "ok", "doctor is happy")

step("nothing missing: no question")
result = sh("ana", INSTALL, quiet=True)
expect("missing" not in result.stdout, "no list")
sys.exit(finish())
