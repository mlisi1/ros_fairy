"""`ros2 fairy verify <bundle>`: a real mission exported in every format is
checked without unpacking it, also by an account outside the ros-fairy group
(a recipient elsewhere, with no mission list); a bundle damaged in transfer,
or cut short, fails and says so."""

import json
import sys

from fairy_sandbox import (
    IDENTITY,
    ensure_talkers,
    expect,
    finish,
    record_mission,
    setup_robot,
    sh,
    step,
)

OUT = "/home/ana/bundles"
RECIPIENT = "dana"   # not in the ros-fairy group
INBOX = f"/home/{RECIPIENT}/inbox"


def verify(user: str, path: str) -> tuple[int, dict]:
    result = sh(user, f"ros2 fairy verify {path} --json", check=False,
                quiet=True)
    return result.returncode, json.loads(result.stdout or "{}")


def titles(data: dict) -> dict[str, str]:
    return {c["title"]: c["status"] for c in data.get("checks", [])}


ensure_talkers()
if not IDENTITY.is_file():
    step("setup")
    expect(setup_robot() == 0, "setup")
sh("root", f"id {RECIPIENT} >/dev/null 2>&1 || useradd -m -s /bin/bash "
   f"{RECIPIENT}", quiet=True)
sh("ana", f"rm -rf {OUT}", quiet=True)
sh("root", f"rm -rf {INBOX} && mkdir -p {INBOX} && chown {RECIPIENT} {INBOX}",
   quiet=True)

step("a mission, exported in every format")
record_mission(goal="Verify bundle test", seconds=6)
bundles = {}
for name, options in [("zip", ""), ("deflated zip", "--compress"),
                      ("tar", "--format tar"), ("tar.gz", "--format tar.gz"),
                      ("tar.zst", "--format tar --compress")]:
    sub = name.replace(" ", "-")
    result = sh("ana", f"ros2 fairy export 1 -o {OUT}/{sub}/ --json "
                f"{options}", quiet=True)
    bundles[name] = json.loads(result.stdout)["bundle"]

step("each verifies on the robot, without unpacking")
folder_code, folder = verify("ana", "1")
expected = {t: s for t, s in titles(folder).items() if "index" not in t}
for name, path in bundles.items():
    code, data = verify("ana", path)
    got = titles(data)
    expect(code == 0 and data.get("bundle") is True, f"{name}: exit {code}")
    expect(got.get("The bundle matches its checksum file") == "ok",
           f"{name}: checksum file matches")
    got.pop("The bundle matches its checksum file", None)
    expect(got == expected, f"{name}: the same checks as the folder"
           + ("" if got == expected else f" (differs: "
              f"{set(got.items()) ^ set(expected.items())})"))
expect(any(t.startswith("Recording ") and s == "ok"
           for t, s in expected.items()), "the recording was hashed")
result = sh("ana", f"ros2 fairy verify {bundles['tar.zst']}", check=False,
            quiet=True)
print(result.stdout)
expect("PASS" in result.stdout, "PASS on the terminal")

step("a recipient outside the group verifies the copy they were sent")
for name in ("deflated zip", "tar.zst"):
    path = bundles[name]
    sh("root", f"cp {path} {path}.sha256 {INBOX}/ && chown {RECIPIENT} "
       f"{INBOX}/*", quiet=True)
    copy = f"{INBOX}/{path.rsplit('/', 1)[-1]}"
    code, data = verify(RECIPIENT, copy)
    expect(code == 0, f"{name}: exit {code} as {RECIPIENT} "
           f"({data.get('error') or ''})")
    expect(not any("index" in t for t in titles(data)),
           f"{name}: no index check")

step("damaged in transfer: one byte changed")
copy = f"{INBOX}/{bundles['tar'].rsplit('/', 1)[-1]}"
sh("root", f"cp {bundles['tar']} {bundles['tar']}.sha256 {INBOX}/ && "
   f"chown {RECIPIENT} {INBOX}/*", quiet=True)
# Flip a byte in the middle of the recording.
sh(RECIPIENT, "python3 -c \"import sys,tarfile; p=sys.argv[1]; "
   "m=next(i for i in tarfile.open(p) if i.name.endswith('.mcap')); "
   "f=open(p,'r+b'); f.seek(m.offset_data+m.size//2); b=f.read(1); "
   "f.seek(-1,1); f.write(bytes([b[0]^255]))\" " + copy, quiet=True)
code, data = verify(RECIPIENT, copy)
got = titles(data)
expect(code == 1, f"exit 1 (got {code})")
expect(got.get("The bundle doesn't match its checksum file") == "fail",
       "the checksum file says so")
expect(any(t.startswith("Recording ") and "modified" in t
           for t in got), f"names the recording: {got}")

step("cut short: a copy that stopped halfway")
copy = f"{INBOX}/{bundles['tar.zst'].rsplit('/', 1)[-1]}"
sh(RECIPIENT, f"truncate -s $(( $(stat -c %s {copy}) / 2 )) {copy}",
   quiet=True)
code, data = verify(RECIPIENT, copy)
expect(code == 1 and "cut short" in json.dumps(data),
       f"exit 1, damaged or cut short: {data.get('checks')}")
sys.exit(finish())
