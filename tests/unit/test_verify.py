import io
import json
from types import SimpleNamespace

from rich.console import Console

from ros_fairy.archive import assembler, index
from ros_fairy.manifest import builder
from ros_fairy.subcommands import verify
from ros_fairy.utils import fsio, paths
from tests.unit.test_archive import _spool

FAIL, WARN, OK = verify.FAIL, verify.WARN, verify.OK


def _make_crate(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    return assembler.assemble(record, harvest)


def _statuses(checks):
    return [c["status"] for c in checks]


def _console():
    return Console(file=io.StringIO(), width=100, force_terminal=False)


def test_clean_archive_has_no_failures(fairy_dirs):
    crate = _make_crate(fairy_dirs)
    checks = verify.verify_archive(crate)
    assert FAIL not in _statuses(checks)
    assert verify._overall(checks) != FAIL
    # the substantive checks are present
    titles = " ".join(c["title"] for c in checks)
    assert "Mission record is valid" in titles
    assert "Calibration gps0_cal matches its checksum" in titles
    assert "matches its checksums" in titles  # the bag's per-file hashes
    assert "registered in the index" in titles


def test_run_returns_zero_for_clean_archive(fairy_dirs):
    _make_crate(fairy_dirs)
    console = _console()
    args = SimpleNamespace(mission="1", json=False, debug=False)
    assert verify.run(args, console=console) == 0
    assert "PASS" in console.file.getvalue()


def test_detects_modified_calibration(fairy_dirs):
    crate = _make_crate(fairy_dirs)
    cal = next(crate.glob("calibrations/*"))
    cal.write_text("tampered: true\n")
    checks = verify.verify_archive(crate)
    bad = [c for c in checks if c["status"] == FAIL]
    assert any("Calibration" in c["title"] and "modified" in c["title"]
               for c in bad)
    assert verify._overall(checks) == FAIL


def test_detects_missing_bag_data_file(fairy_dirs):
    crate = _make_crate(fairy_dirs)
    db = next(crate.glob("bags/*/*.db3"))
    db.unlink()
    checks = verify.verify_archive(crate)
    assert any(c["status"] == FAIL and "missing data files" in c["title"]
               for c in checks)


def test_assembled_bag_records_file_checksums(fairy_dirs):
    """The assembler pins every bag file with a sha256 matching the archived
    bytes."""
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    crate = assembler.assemble(record, harvest)
    bag = record.bags[0]
    assert bag.file_sha256, "expected per-file checksums to be recorded"
    bag_dir = crate / bag.path
    for rel, expected in bag.file_sha256.items():
        assert fsio.sha256_file(bag_dir / rel) == expected


def test_detects_modified_bag_data(fairy_dirs):
    """Byte-level tamper detection — a changed .db3 fails verification."""
    crate = _make_crate(fairy_dirs)
    db = next(crate.glob("bags/*/*.db3"))
    db.write_bytes(db.read_bytes() + b"tampered")
    checks = verify.verify_archive(crate)
    assert any(c["status"] == FAIL and "has been modified" in c["title"]
               for c in checks)


def test_pre_1_0_archive_without_checksums_warns(fairy_dirs):
    """An archive whose bags predate file_sha256 still verifies structurally."""
    crate = _make_crate(fairy_dirs)
    record_file = crate / "mission_record.json"
    data = json.loads(record_file.read_text())
    for bag in data["bags"]:
        bag["file_sha256"] = {}
    record_file.write_text(json.dumps(data))
    (crate / "checksums.sha256").unlink()  # old archives don't have it
    checks = verify.verify_archive(crate)
    assert FAIL not in _statuses(checks)
    assert any(c["status"] == WARN and "no checksums recorded" in c["title"]
               for c in checks)
    assert any(c["status"] == WARN and "can't be checked" in c["title"]
               for c in checks)


def test_edited_mission_record_fails(fairy_dirs):
    """S14: the record, README and harvest files are checksummed too."""
    crate = _make_crate(fairy_dirs)
    record_file = crate / "mission_record.json"
    data = json.loads(record_file.read_text())
    data["identity"]["operator_name"] = "Someone Else"
    record_file.write_text(json.dumps(data))
    checks = verify.verify_archive(crate)
    assert any(c["status"] == FAIL and "have been modified" in c["title"]
               and "mission_record.json" in c["detail"] for c in checks)


def test_file_added_after_saving_is_noted(fairy_dirs):
    crate = _make_crate(fairy_dirs)
    (crate / "harvest" / "extra.txt").write_text("hand-added")
    checks = verify.verify_archive(crate)
    assert FAIL not in _statuses(checks)
    assert any(c["status"] == WARN and "added to the archive" in c["title"]
               for c in checks)


def test_calibration_without_checksum_is_not_a_match(fairy_dirs):
    crate = _make_crate(fairy_dirs)
    record_file = crate / "mission_record.json"
    data = json.loads(record_file.read_text())
    data["calibrations"][0]["sha256"] = None
    record_file.write_text(json.dumps(data))
    (crate / "checksums.sha256").unlink()
    checks = verify.verify_archive(crate)
    assert any(c["status"] == WARN and "no checksum recorded" in c["title"]
               for c in checks)
    assert not any("matches its checksum" in c["title"]
                   and "Calibration" in c["title"] for c in checks)


def test_detects_missing_referenced_file(fairy_dirs):
    crate = _make_crate(fairy_dirs)
    (crate / "harvest" / "pip_freeze.txt").unlink()
    checks = verify.verify_archive(crate)
    assert any(c["status"] == FAIL
               and "referenced by the crate are missing" in c["title"]
               for c in checks)


def test_not_in_index_warns_but_passes(fairy_dirs):
    crate = _make_crate(fairy_dirs)
    paths.index_db_path().unlink()  # simulate a lost/rebuilt index
    checks = verify.verify_archive(crate)
    assert FAIL not in _statuses(checks)
    assert any(c["status"] == WARN and "not in the local index" in c["title"]
               for c in checks)
    assert verify._overall(checks) == WARN


def test_corrupt_mission_record_fails_fast(fairy_dirs):
    crate = _make_crate(fairy_dirs)
    (crate / "mission_record.json").write_text("{ not valid json")
    checks = verify.verify_archive(crate)
    assert len(checks) == 1
    assert checks[0]["status"] == FAIL


def test_json_output(fairy_dirs, capsys):
    _make_crate(fairy_dirs)
    args = SimpleNamespace(mission="1", json=True, debug=False)
    assert verify.run(args, console=_console()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] in (OK, WARN)
    assert payload["checks"] and "archive" in payload


def test_unknown_mission_errors(fairy_dirs):
    index._connect().close()  # ensure an (empty) index exists
    console = _console()
    args = SimpleNamespace(mission="does-not-exist", json=False, debug=False)
    assert verify.run(args, console=console) == 1
    assert "Can't find a mission" in console.file.getvalue()
