"""Validate the core against REAL rosbag2 output.

The synthetic fixtures in tests/conftest hand-write metadata.yaml and the
storage file, so they only prove the code is self-consistent — not that it
matches what `ros2 bag record` actually produces (Jazzy writes metadata
version 9 with QoS / compression / type-hash fields the synthetic fixtures
omit). Drop real bags into tests/fixtures/ (tools/record_real_fixtures.sh
records them) and these tests exercise the real parse -> health -> assemble
path.

They skip cleanly when tests/fixtures/ has no bags, so CI stays green until
fixtures are added; each test then runs once per discovered bag.
"""

import shutil
from pathlib import Path

import pytest

from fairy.archive import assembler
from fairy.manifest import builder
from fairy.utils import bag_storage, fsio, paths, topic_health
from fairy.watchdog import watchdog

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _real_bags() -> list[Path]:
    if not FIXTURES.is_dir():
        return []
    return sorted(p.parent for p in FIXTURES.glob("*/metadata.yaml"))


# One skipped placeholder when there are no fixtures yet; one case per bag once
# they exist. Keeps collection clean (no empty-parametrize warning).
_BAGS = _real_bags() or [pytest.param(
    None, marks=pytest.mark.skip(
        reason="no real bags in tests/fixtures/ (record them with "
               "tools/record_real_fixtures.sh)"))]


def _bag_id(p) -> str:
    return getattr(p, "name", "none")


@pytest.mark.parametrize("bag", _BAGS, ids=_bag_id)
def test_parse_real_metadata(bag):
    meta = topic_health.parse_bag_metadata(bag)
    assert meta is not None, f"could not parse {bag}/metadata.yaml"
    assert meta["storage_identifier"] in ("sqlite3", "mcap")
    assert meta["start_s"] > 0
    assert meta["duration_s"] >= 0
    assert meta["topics"], "expected at least one recorded topic"
    for topic in meta["topics"]:
        assert topic["name"] and topic["type"]
        assert topic["message_count"] >= 0
    assert meta["relative_file_paths"], "metadata should list storage files"


@pytest.mark.parametrize("bag", _BAGS, ids=_bag_id)
def test_health_runs_on_real_bag(bag):
    warnings = topic_health.analyse_bag(bag, sensors=[])
    assert isinstance(warnings, list)
    for warning in warnings:
        assert {"topic", "kind", "plain_text"} <= set(warning)
        assert warning["kind"] in ("gap", "low_rate", "never_published")
        assert warning["plain_text"]


@pytest.mark.parametrize("bag", _BAGS, ids=_bag_id)
def test_storage_reader_reads_real_timestamps(bag):
    meta = topic_health.parse_bag_metadata(bag)
    reader = bag_storage.get_reader(meta["storage_identifier"])
    if reader is None or not reader.supported:
        pytest.skip(f"no supported reader for {meta['storage_identifier']!r}")
    series = reader.topic_timestamps(bag, meta["relative_file_paths"])
    assert any(series.values()), "reader extracted no timestamps from real bag"
    for stamps in series.values():
        assert stamps == sorted(stamps), "timestamps must be ascending"


@pytest.mark.parametrize("bag", _BAGS, ids=_bag_id)
def test_assemble_crate_from_real_bag(bag, fairy_dirs):
    """Full path: real metadata -> Bag record -> MissionRecord -> RO-Crate."""
    rocrate = pytest.importorskip("rocrate.rocrate")

    # Copy the fixture into the spool so assemble() can move it without
    # touching the committed fixture.
    spool_bag = paths.bags_dir() / bag.name
    shutil.copytree(bag, spool_bag)
    watchdog.append_bag_record(spool_bag)

    harvest, _ = builder.load_spool()
    context = builder.new_mission_context(
        operator_name="Tester", goal="Real bag check",
        location_name="Lab")
    fsio.atomic_write_json(paths.mission_context_path(), context)

    record = builder.build(harvest, context)
    assert record.bags and record.bags[0].storage_format in ("sqlite3", "mcap")

    crate_dir = assembler.assemble(record, harvest)
    crate = rocrate.ROCrate(str(crate_dir))  # raises if JSON-LD is malformed
    assert crate.root_dataset["name"] == "Real bag check"
    assert (crate_dir / "bags" / bag.name / "metadata.yaml").is_file()
