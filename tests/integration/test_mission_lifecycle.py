"""End-to-end mission lifecycle without a live ROS graph.

Drives the real modules together: watchdog (fake inotify, fake harvest
pipeline) -> mission_start (mocked prompts) -> mission_close (save) ->
ros2 fairy list. Only subprocess-level ROS interaction is faked.
"""

import io
import json
from types import SimpleNamespace
from unittest import mock

import pytest
from inotify_simple import flags
from rich.console import Console

from ros_fairy.archive import assembler
from ros_fairy.manifest import builder
from ros_fairy.subcommands import list_missions, mission_close, mission_start
from ros_fairy.utils import paths
from ros_fairy.watchdog.watchdog import Watchdog
from tests.conftest import make_bag
from tests.unit.test_archive import _spool
from tests.unit.test_watchdog import (
    FakeClock,
    FakeINotify,
    good_pipeline,
    no_recorders,
)

T0 = 1_750_000_000.0


def _console():
    return Console(file=io.StringIO(), width=120, force_terminal=False)


def test_full_mission_lifecycle(fairy_dirs, tmp_path, capsys):
    # 1. watchdog comes up with an empty spool (boot)
    ino, clock = FakeINotify(), FakeClock()
    dog = Watchdog(inotify=ino, clock=clock, pipeline=good_pipeline,
                   harvest_in_thread=False,
                   scan_recorders=no_recorders)
    dog.start()

    # 2. operator runs the briefing
    answers = {"operator_name": "Jane Doe", "goal": "Survey eelgrass beds",
               "location_name": "Marsh Creek, north bank",
               "environment": "marine", "notes": None}
    with mock.patch.object(mission_start.briefing, "ask_briefing",
                           return_value=answers):
        assert mission_start.run(SimpleNamespace(), console=_console()) == 0

    # 3. a recording happens (rosbag2 simulated by the fixture); GPS drops
    #    out for ~4 minutes mid-mission
    fix = [t for t in
           [T0 + i / 10 for i in range(0, 12000)] if not
           (T0 + 240 < t < T0 + 480)]
    bag = make_bag(paths.bags_dir() / "rosbag2_mission",
                   {"/fix": fix},
                   types={"/fix": "sensor_msgs/msg/NavSatFix"})
    ino.emit_dir_created(paths.bags_dir(), "rosbag2_mission")
    dog.step(0)
    ino.emit_file(bag, "rosbag2_mission_0.db3", flags.CREATE)
    dog.step(0)
    assert dog.state == "RECORDING"
    ino.emit_file(bag, "metadata.yaml", flags.CLOSE_WRITE)
    dog.step(0)
    assert dog.state == "IDLE"

    # 4. operator closes the mission and saves
    console = _console()
    with mock.patch.object(mission_close.review, "confirm_save",
                           return_value="save"):
        assert mission_close.run(SimpleNamespace(), console=console) == 0
    out = console.file.getvalue()
    assert "GPS (gps0) signal was lost for 4 minutes" in out
    assert "Mission saved" in out

    # 5. the crate is complete and the spool is clean
    archives = [p for p in paths.archive_dir().iterdir()
                if p.is_dir() and p.name != ".staging"]
    assert len(archives) == 1
    crate = archives[0]
    assert crate.name.endswith("_marsh-creek-north-bank_jane-doe")
    assert (crate / "bags" / "rosbag2_mission" / "metadata.yaml").is_file()
    doc = json.loads((crate / "ro-crate-metadata.json").read_text())
    ids = {e["@id"] for e in doc["@graph"]}
    assert {"./", "#operator", "#place", "#robot", "#sensor-gps0",
            "#mission", "#ros2"} <= ids
    record = json.loads((crate / "mission_record.json").read_text())
    assert record["provenance"]["field_confidence"]["intent.goal"] == "user"
    assert record["bags"][0]["health_warnings"][0]["kind"] == "gap"
    assert not any(paths.bags_dir().iterdir())
    assert not paths.harvest_json_path().exists()

    # 6. the mission is findable
    console = _console()
    args = SimpleNamespace(operator=None, location=None, since=None,
                           until=None, limit=20, path=False)
    assert list_missions.run(args, console=console) == 0
    listing = console.file.getvalue()
    assert "Jane Doe" in listing
    assert "Marsh Creek" in listing
    assert "1" in listing  # the warning column

    # 7. the saved archive stands up to verification
    from ros_fairy.subcommands import verify
    v_args = SimpleNamespace(mission="1", json=True, debug=False)
    assert verify.run(v_args, console=_console()) == 0
    v = json.loads(capsys.readouterr().out)
    assert v["result"] != "fail"
    assert not any(c["status"] == "fail" for c in v["checks"])

    # 8. it exports to a portable bundle with a checksum sidecar
    from ros_fairy.subcommands import export
    e_args = SimpleNamespace(mission="1", output=str(tmp_path / "out"),
                             format="zip", force=False, json=True, debug=False)
    (tmp_path / "out").mkdir()
    assert export.run(e_args, console=_console()) == 0
    json.loads(capsys.readouterr().out)  # --json output must be valid JSON
    exported = list((tmp_path / "out").iterdir())
    assert any(f.suffix == ".zip" for f in exported)
    assert any(f.name.endswith(".sha256") for f in exported)

    # 9. a second mission (adopted bag, different goal) diffs against the first
    from ros_fairy.subcommands import adopt, mission_diff
    answers2 = {"operator_name": "Jane Doe", "goal": "Follow-up transect",
                "location_name": "Marsh Creek, north bank",
                "environment": "marine", "notes": None}
    with mock.patch.object(mission_start.briefing, "ask_briefing",
                           return_value=answers2):
        assert mission_start.run(SimpleNamespace(), console=_console()) == 0
    outside = make_bag(tmp_path / "handheld_run",
                       {"/fix": [T0 + i / 10 for i in range(600)]},
                       types={"/fix": "sensor_msgs/msg/NavSatFix"})
    with mock.patch.object(adopt.watchdog, "run_pipeline", good_pipeline):
        assert adopt.run(SimpleNamespace(bagdir=str(outside), json=False,
                                         debug=False),
                         console=_console()) == 0
    with mock.patch.object(mission_close.review, "confirm_save",
                           return_value="save"):
        assert mission_close.run(SimpleNamespace(), console=_console()) == 0
    d_args = SimpleNamespace(mission_a="2", mission_b="1", json=True,
                             debug=False)
    assert mission_diff.run(d_args, console=_console()) == 0
    d = json.loads(capsys.readouterr().out)
    goal_change = [c for c in d["changes"]["mission_context"]
                   if c["label"] == "Goal"]
    assert goal_change and goal_change[0]["b"] == "Follow-up transect"

    # 10. a lost index is rebuilt from the archives on disk
    from ros_fairy.archive import index
    from ros_fairy.subcommands import reindex
    paths.index_db_path().unlink()
    assert reindex.run(SimpleNamespace(json=False, debug=False),
                       console=_console()) == 0
    rows, total = index.query()
    assert total == 2


def test_crate_loads_with_rocrate_library(fairy_dirs):
    """The generated crate must be loadable by the rocrate library.

    Skipped when the optional rocrate dependency is not installed.
    """
    ROCrate = pytest.importorskip("rocrate.rocrate").ROCrate

    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    crate_dir = assembler.assemble(record, harvest)

    crate = ROCrate(str(crate_dir))  # raises if the JSON-LD is malformed
    ids = {e.id for e in crate.get_entities()}
    # contextual + flattened PropertyValue entities all present
    assert {"#python-runtime", "#ros2", "#robot", "#sensor-gps0", "#mission",
            "#confidence-user", "#sensor-gps0-topic"} <= ids
    assert crate.root_dataset["name"] == "Survey eelgrass beds"
