"""`ros2 fairy export --compress`: a real mission bundled every way (plain
zip, deflated zip, .tar.gz, .tar.zst). Every bundle passes `sha256sum -c`
and unpacks into a folder `verify` accepts; zstd comes with the install
(mcap needs it)."""

import json
import sys
from pathlib import Path

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

OUT = "/home/ana/exports"
# Unpacks any bundle with Python alone (the image has no unzip or zstd), and
# prints the zip's compression methods for the recordings.
UNPACK = Path("/home/ana/unpack.py")
UNPACK_CODE = """
import sys, tarfile, zipfile
path, dest = sys.argv[1], sys.argv[2]
if path.endswith('.zip'):
    with zipfile.ZipFile(path) as zf:
        assert zf.testzip() is None
        print(' '.join(sorted({str(i.compress_type) for i in zf.infolist()
                               if i.filename.endswith('.mcap')})))
        zf.extractall(dest)
elif path.endswith('.tar.zst'):
    import zstandard
    with open(path, 'rb') as raw, \
            zstandard.ZstdDecompressor().stream_reader(raw) as zr, \
            tarfile.open(fileobj=zr, mode='r|') as tf:
        tf.extractall(dest, filter='data')
else:
    with tarfile.open(path) as tf:
        tf.extractall(dest, filter='data')
"""


def export(options: str) -> dict:
    result = sh("ana", f"ros2 fairy export 1 -o {OUT}/ --force --json "
                f"{options}", check=False, quiet=True)
    return json.loads(result.stdout or "{}") | {"_exit": result.returncode}


def checked(data: dict, fmt: str, compression: str) -> None:
    """The bundle is what was asked, its sidecar checks, and it unpacks
    into a mission that verifies."""
    expect(data["_exit"] == 0, f"exit 0 (got {data['_exit']}: "
           f"{data.get('error')})")
    expect((data.get("format"), data.get("compression")) ==
           (fmt, compression), f"{fmt}, {compression}: got "
           f"{data.get('format')}, {data.get('compression')}")
    bundle = data["bundle"]
    expect(bundle.endswith("." + fmt), f"named {bundle}")
    result = sh("ana", f"cd {OUT} && sha256sum -c {bundle}.sha256",
                check=False, quiet=True)
    expect(result.returncode == 0, "sha256sum -c passes")
    sh("ana", f"rm -rf {OUT}/unpacked", quiet=True)
    sh("ana", f"python3 {UNPACK} {bundle} {OUT}/unpacked")
    folder = f"{OUT}/unpacked/{data['source'].rsplit('/', 1)[-1]}"
    result = sh("ana", f"ros2 fairy verify {folder}", check=False,
                quiet=True)
    expect(result.returncode == 0, "the unpacked mission verifies")
    print(f"  {bundle}: {data['size_bytes']} bytes")


ensure_talkers()
if not IDENTITY.is_file():
    step("setup")
    expect(setup_robot() == 0, "setup")
sh("ana", f"rm -rf {OUT}", quiet=True)
UNPACK.write_text(UNPACK_CODE)
UNPACK.chmod(0o644)

step("a mission to export")
record_mission(goal="Export compression test", seconds=8)

step("by default: an uncompressed zip, as before")
plain = export("")
checked(plain, "zip", "none")

step("--compress: a deflated zip, smaller")
deflated = export("--compress")
checked(deflated, "zip", "deflate")
expect(deflated["size_bytes"] < plain["size_bytes"] * 0.8,
       f"{deflated['size_bytes']} < 80% of {plain['size_bytes']}")
methods = sh("ana", f"python3 {UNPACK} {deflated['bundle']} "
             f"{OUT}/unpacked", quiet=True).stdout.split()
expect(methods == ["8"], f"the recording is deflated (methods {methods})")

step("--format tar.gz")
gz = export("--format tar.gz")
checked(gz, "tar.gz", "gzip")

step("--format tar --compress: a .tar.zst, the smallest")
zst = export("--format tar --compress")
checked(zst, "tar.zst", "zstd")
expect(zst["size_bytes"] < deflated["size_bytes"],
       f"{zst['size_bytes']} < {deflated['size_bytes']} (deflated zip)")

step("export --today --compress fills the folder")
sh("ana", f"rm -rf {OUT}", quiet=True)
result = sh("ana", f"ros2 fairy export --today --force --compress --format "
            f"tar -o {OUT} --json", check=False, quiet=True)
batch = json.loads(result.stdout or "{}")
expect(result.returncode == 0 and batch.get("failed") == 0,
       f"exported {batch.get('exported')}, failed {batch.get('failed')}")
expect(batch.get("exported", 0) >= 1 and
       all(r["bundle"].endswith(".tar.zst") for r in batch["results"]),
       "every bundle is a .tar.zst")
sys.exit(finish())
