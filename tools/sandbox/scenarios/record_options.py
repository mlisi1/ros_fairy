"""mission_record with ros2 bag record's options: split recordings,
another topic selection, file compression; -o and unknown options refused
before anything is recorded; the mission still saves and verifies."""

import json
import sys
from pathlib import Path

import yaml
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


def bags() -> list[Path]:
    return sorted(p for p in (data_dir() / "spool" / "bags").iterdir()
                  if p.is_dir())


def topics(bag: Path) -> set[str]:
    info = yaml.safe_load((bag / "metadata.yaml").read_text())
    return {t["topic_metadata"]["name"] for t in
            info["rosbag2_bagfile_information"]["topics_with_message_count"]}


def record(options: str, seconds: float = 6) -> Path | None:
    before = set(bags())
    status = interact("ana", f"ros2 fairy mission_record --yes {options}",
                      {}, interrupt_after=seconds)
    expect(status == 0, f"mission_record {options}: exit {status}")
    wait_until_idle()
    new = sorted(set(bags()) - before)
    return new[0] if new else None


ensure_talkers()
if not IDENTITY.is_file():
    step("setup")
    expect(setup_robot() == 0, "setup")
sh("ana", "ros2 fairy mission_abort --yes", check=False, quiet=True)

step("robot_identity.yaml records /fix only")
identity = yaml.safe_load(IDENTITY.read_text())
identity["recording"] = {"topics": ["/fix"], "storage": "mcap"}
IDENTITY.write_text(yaml.safe_dump(identity, sort_keys=False))
result = sh("ana", "ros2 fairy mission_start --operator Ana --goal "
            "'Recorder options' --location Lab --yes", check=False)
expect(result.returncode == 0, "mission started")

step("options are passed through: split every 2 s")
bag = record("--max-bag-duration 2")
expect(bag is not None and len(list(bag.glob("*.mcap"))) >= 2,
       f"split into {len(list(bag.glob('*.mcap'))) if bag else 0} files")
expect(bag is not None and topics(bag) == {"/fix"},
       "still robot_identity.yaml's topics")

step("a topic selection of your own replaces the configured one")
bag = record("/scan")
expect(bag is not None and topics(bag) == {"/scan"},
       f"only /scan ({topics(bag) if bag else None})")

step("file compression: saved as .mcap.zstd")
bag = record("--compression-mode file --compression-format zstd")
expect(bag is not None and list(bag.glob("*.mcap.zstd")),
       "a .mcap.zstd file")

step("-o is refused before anything is recorded")
before = bags()
result = sh("ana", "ros2 fairy mission_record --yes -o /tmp/elsewhere",
            check=False)
expect(result.returncode == 1 and "-o/--output can't be used"
       in result.stdout, "refused, saying why")
expect(bags() == before and not Path("/tmp/elsewhere").exists(),
       "nothing recorded")

step("an option rosbag2 doesn't have is refused up front")
result = sh("ana", "ros2 fairy mission_record --yes --frobnicate",
            check=False)
expect(result.returncode == 2 and "unrecognized arguments: --frobnicate"
       in result.stderr, "argparse's usage message")

step("-h lists rosbag2's options with mission_record's own")
result = sh("ana", "ros2 fairy mission_record -h", check=False, quiet=True)
expect(result.returncode == 0 and "--max-bag-size" in result.stdout
       and "--yes" in result.stdout, "one help for both")

step("the mission saves and verifies, compressed recording noted")
result = sh("ana", "ros2 fairy mission_close --yes", check=False)
expect(result.returncode == 0, f"mission_close exit {result.returncode}")
expect("wasn't checked for gaps" in result.stdout,
       "the compressed recording is noted in the review")
listed = json.loads(sh("ana", "ros2 fairy list --json", quiet=True).stdout)
expect(listed["missions"][0]["bag_count"] == 3, "three recordings saved")
sh("ana", "ros2 fairy verify 1")
sys.exit(finish())
