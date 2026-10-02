"""Saving a mission safely: interrupted saves, locking, copy failures, and
the crate details fixed for FAILURE_CASES A1-A16."""

import errno
import io
import json
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from rich.console import Console

from ros_fairy.archive import assembler, duplicates, index
from ros_fairy.archive.assembler import AssemblyError
from ros_fairy.manifest import builder
from ros_fairy.subcommands import mission_close
from ros_fairy.utils import fsio, paths
from tests.conftest import make_bag
from tests.unit.test_archive import T0, _spool

ARGS = SimpleNamespace()


def _console():
    return Console(file=io.StringIO(), width=200, force_terminal=False)


def _crates():
    return [p for p in paths.archive_dir().iterdir()
            if p.is_dir() and not p.name.startswith(".")]


def _interrupt_mid_move(record, harvest):
    """Leave a save cut off after its first spool bag was moved, the way a
    power cut would (no rollback)."""
    real_move = assembler._move_bag
    calls = {"n": 0}

    def move(src, dest, *rest):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(errno.EIO, "Input/output error")
        real_move(src, dest, *rest)

    def no_rollback(src, dest):
        raise OSError(errno.EIO, "power cut")

    with mock.patch.object(assembler, "_move_bag", move), \
            mock.patch.object(assembler, "_move_back", no_rollback), \
            pytest.raises(AssemblyError, match="safe in"):
        assembler.assemble(record, harvest)


@pytest.fixture
def cross_device(monkeypatch):
    """Make every rename out of (or back into) the spool fail with EXDEV."""
    real_rename = Path.rename

    def rename(self, target):
        bags = str(paths.bags_dir())
        if str(self).startswith(bags) or str(target).startswith(bags):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)


# -- A1 / A2 / A14: resuming an interrupted save ------------------------------

def test_interrupted_save_is_finished_and_spool_cleared(fairy_dirs):
    harvest, context = _spool(fairy_dirs, n_bags=2)
    record = builder.build(harvest, context)
    _interrupt_mid_move(record, harvest)

    [pending] = assembler.pending_saves()
    assert pending.kind == assembler.RESUME
    assert pending.goal == "Survey eelgrass beds"
    final = assembler.finish_pending(pending)

    assert (final / "bags" / "rosbag2_0").is_dir()
    assert (final / "bags" / "rosbag2_1").is_dir()
    assert not any(paths.bags_dir().iterdir())
    # A2: the resumed save clears the spool like a normal one.
    assert not paths.harvest_json_path().exists()
    assert not paths.mission_context_path().exists()
    assert assembler.pending_saves() == []
    assert index.query()[1] == 1


def test_declining_resume_keeps_the_interrupted_save(fairy_dirs):
    """A1: answering No used to start a new save that deleted the staging
    tree holding the moved recordings."""
    harvest, context = _spool(fairy_dirs, n_bags=2)
    record = builder.build(harvest, context)
    _interrupt_mid_move(record, harvest)
    staging = paths.staging_dir() / assembler.pending_saves()[0].name
    moved = staging / "bags" / "rosbag2_0"
    assert moved.is_dir()

    console = _console()
    with mock.patch.object(mission_close.Confirm, "ask", return_value=False), \
            mock.patch.object(mission_close.review, "confirm_save") as save:
        assert mission_close.run(ARGS, console=console) == 0
    save.assert_not_called()
    assert moved.is_dir()
    assert "Nothing was changed" in console.file.getvalue()
    assert [p.kind for p in assembler.pending_saves()] == [assembler.RESUME]


def test_resume_via_mission_close(fairy_dirs):
    harvest, context = _spool(fairy_dirs, n_bags=2)
    record = builder.build(harvest, context)
    _interrupt_mid_move(record, harvest)
    console = _console()
    with mock.patch.object(mission_close.Confirm, "ask", return_value=True):
        assert mission_close.run(ARGS, console=console) == 0
    assert "Mission saved" in console.file.getvalue()
    assert len(_crates()) == 1


def test_new_save_never_reuses_an_interrupted_name(fairy_dirs):
    harvest, context = _spool(fairy_dirs, n_bags=2)
    record = builder.build(harvest, context)
    _interrupt_mid_move(record, harvest)
    taken = assembler.pending_saves()[0].name
    assert assembler.archive_name(record) == taken + "_2"


def test_building_leftover_is_discarded(fairy_dirs):
    """A14: a save cut off before any spool bag moved holds only copies."""
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    name = assembler.archive_name(record)
    (paths.staging_dir() / name / "bags").mkdir(parents=True)
    assembler._write_plan({"name": name, "mission_id": "m", "state":
                           assembler.BUILDING, "bag_sources": [], "moves": []})
    [pending] = assembler.pending_saves()
    assert pending.kind == assembler.DISCARD

    with mock.patch.object(mission_close.review, "confirm_save",
                           return_value="save"):
        assert mission_close.run(ARGS, console=_console()) == 0
    assert assembler.pending_saves() == []
    assert len(_crates()) == 1


def test_legacy_staging_without_record_is_discarded(fairy_dirs):
    junk = paths.staging_dir() / "2026-09-29_17-04-31_x"
    (junk / "harvest").mkdir(parents=True)
    [pending] = assembler.pending_saves()
    assert pending.kind == assembler.DISCARD
    assembler.discard_pending(pending)
    assert not junk.exists()


def test_legacy_staging_missing_bags_is_not_committed(fairy_dirs):
    """A14: an old-style partial save must not be renamed into the archive."""
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    record.bags[0].path = "bags/rosbag2_0"
    staging = paths.staging_dir() / "old"
    staging.mkdir(parents=True)
    fsio.atomic_write_json(staging / "mission_record.json",
                           record.model_dump(mode="json"))
    [pending] = assembler.pending_saves()
    assert pending.kind == assembler.STUCK
    with pytest.raises(AssemblyError):
        assembler.finish_pending(pending)
    assert staging.is_dir() and _crates() == []


# -- A3: Ctrl-C -----------------------------------------------------------------

def test_ctrl_c_while_staging_leaves_nothing_behind(fairy_dirs, monkeypatch):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)

    def interrupt(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(assembler.ro_crate, "write", interrupt)
    with pytest.raises(KeyboardInterrupt):
        assembler.assemble(record, harvest)
    assert assembler.pending_saves() == []
    assert (paths.bags_dir() / "rosbag2_0").is_dir()
    assert paths.harvest_json_path().exists()


def test_ctrl_c_while_moving_puts_bags_back(fairy_dirs, monkeypatch):
    harvest, context = _spool(fairy_dirs, n_bags=2)
    record = builder.build(harvest, context)
    real_move = assembler._move_bag
    calls = {"n": 0}

    def move(src, dest, *rest):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        real_move(src, dest, *rest)
    monkeypatch.setattr(assembler, "_move_bag", move)
    with pytest.raises(KeyboardInterrupt):
        assembler.assemble(record, harvest)
    assert (paths.bags_dir() / "rosbag2_0").is_dir()
    assert (paths.bags_dir() / "rosbag2_1").is_dir()
    assert assembler.pending_saves() == []
    assert _crates() == []


# -- A4 / A8 / A10: after the commit ----------------------------------------------

def test_crash_after_commit_is_tidied_up(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)

    def crash(*a, **k):
        raise KeyboardInterrupt  # stands in for a power cut
    with mock.patch.object(assembler, "_post_commit", crash):
        final = assembler.assemble(record, harvest)
    assert paths.mission_context_path().exists()  # not cleared yet

    [pending] = assembler.pending_saves()
    assert pending.kind == assembler.TIDY
    console = _console()
    assert mission_close.run(ARGS, console=console) == 1  # nothing else left
    assert f"tidying up after saving {final.name}" in console.file.getvalue()
    assert not paths.mission_context_path().exists()
    assert index.find_mission(record.identity.mission_id) is not None
    assert assembler.pending_saves() == []


def test_already_saved_spool_is_cleared_not_saved_twice(fairy_dirs):
    """A4: a spool left behind by a save from an older version."""
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    final = assembler.assemble(record, harvest)
    # Put the old spool back as an interrupted clean-up would have left it.
    fsio.atomic_write_json(paths.mission_context_path(), context)
    fsio.atomic_write_json(paths.harvest_json_path(), harvest)

    console = _console()
    with mock.patch.object(mission_close.review, "confirm_save") as save:
        assert mission_close.run(ARGS, console=console) == 0
    save.assert_not_called()
    assert f"already saved as {final.name}" in console.file.getvalue()
    assert not paths.mission_context_path().exists()
    assert not paths.harvest_json_path().exists()
    assert len(_crates()) == 1


def test_assemble_refuses_a_mission_id_already_saved(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    final = assembler.assemble(record, harvest)
    harvest2, _ = _spool(fairy_dirs)
    again = builder.build(harvest2, context)
    with pytest.raises(AssemblyError, match=f"already saved as {final.name}"):
        assembler.assemble(again, harvest2)


def test_spool_of_a_newer_mission_is_not_cleared(fairy_dirs):
    """Resume only clears the spool when it still belongs to that mission."""
    harvest, context = _spool(fairy_dirs, n_bags=2)
    record = builder.build(harvest, context)
    _interrupt_mid_move(record, harvest)
    newer = builder.new_mission_context("Bob", "Other", "Elsewhere")
    fsio.atomic_write_json(paths.mission_context_path(), newer)
    assembler.finish_pending(assembler.pending_saves()[0])
    assert json.loads(paths.mission_context_path().read_text()) == newer


def test_bags_recorded_during_the_save_keep_their_records(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    late = make_bag(paths.bags_dir() / "rosbag2_late", {"/fix": [T0, T0 + 1]})
    spool_doc = json.loads(paths.harvest_json_path().read_text())
    spool_doc["bags"].append({**spool_doc["bags"][0], "path": str(late)})
    fsio.atomic_write_json(paths.harvest_json_path(), spool_doc)

    assembler.assemble(record, harvest)
    left = json.loads(paths.harvest_json_path().read_text())
    assert [b["path"] for b in left["bags"]] == [str(late)]
    assert not paths.mission_context_path().exists()


def test_index_failure_is_reported_not_silent(fairy_dirs, monkeypatch):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)

    def locked(*a, **k):
        raise index.sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(index, "insert", locked)
    notices = []
    final = assembler.assemble(record, harvest, warn=notices.append)
    assert (final / "mission_record.json").is_file()
    assert len(notices) == 1 and "reindex" in notices[0]
    assert not paths.mission_context_path().exists()


def test_resume_reports_index_failure_instead_of_failing(fairy_dirs,
                                                         monkeypatch):
    """A8: errors after the commit must not look like a failed save."""
    harvest, context = _spool(fairy_dirs, n_bags=2)
    record = builder.build(harvest, context)
    _interrupt_mid_move(record, harvest)
    monkeypatch.setattr(index, "insert", mock.Mock(side_effect=OSError("ro")))
    notices = []
    final = assembler.finish_pending(assembler.pending_saves()[0],
                                     warn=notices.append)
    assert final.is_dir() and "reindex" in notices[0]


# -- A5 / A11 / A13: foreign recordings -------------------------------------------

def _foreign_record(tmp_path, *names):
    harvest, context = None, None
    from tests.unit.test_foreign_bags import _bag_entry, good_pipeline
    bags = [make_bag(tmp_path / n, {"/fix": [T0, T0 + 1]}) for n in names]
    harvest = good_pipeline()
    harvest["bags"] = [_bag_entry(b, "detected") for b in bags]
    context = builder.new_mission_context("Op", "Goal", "Loc")
    fsio.atomic_write_json(paths.harvest_json_path(), harvest)
    fsio.atomic_write_json(paths.mission_context_path(), context)
    return builder.build(harvest, context), harvest, bags


def test_foreign_copy_failure_aborts_the_save(fairy_dirs, tmp_path,
                                              monkeypatch):
    record, harvest, (bag,) = _foreign_record(tmp_path, "ext")

    def broken(src, dst, *a, **k):
        raise shutil.Error([(str(src), str(dst), "[Errno 5] I/O error")])
    monkeypatch.setattr(assembler.shutil, "copytree", broken)
    with pytest.raises(AssemblyError, match="couldn't be copied"):
        assembler.assemble(record, harvest)
    assert bag.is_dir() and _crates() == []
    assert paths.harvest_json_path().exists()
    assert assembler.pending_saves() == []


def test_foreign_copy_out_of_space_says_so(fairy_dirs, tmp_path, monkeypatch):
    record, harvest, _ = _foreign_record(tmp_path, "ext")

    def full(src, dst, *a, **k):
        raise shutil.Error([(str(src), str(dst),
                             "[Errno 28] No space left on device")])
    monkeypatch.setattr(assembler.shutil, "copytree", full)
    with pytest.raises(AssemblyError, match="enough disk space"):
        assembler.assemble(record, harvest)


def test_not_enough_space_is_caught_before_copying(fairy_dirs, tmp_path,
                                                   monkeypatch):
    record, harvest, _ = _foreign_record(tmp_path, "ext")
    monkeypatch.setattr(assembler.shutil, "disk_usage",
                        lambda p: SimpleNamespace(free=1024))
    copy = mock.Mock()
    monkeypatch.setattr(assembler.shutil, "copytree", copy)
    with pytest.raises(AssemblyError, match="needs about"):
        assembler.assemble(record, harvest)
    copy.assert_not_called()


def test_foreign_checksums_describe_the_archived_copy(fairy_dirs, tmp_path,
                                                      monkeypatch):
    """A11: the source changing after the copy must not break verify."""
    record, harvest, (bag,) = _foreign_record(tmp_path, "ext")
    real_copytree = shutil.copytree

    def copy_then_change(src, dst, *a, **k):
        real_copytree(src, dst, *a, **k)
        (Path(src) / "metadata.yaml").write_text("changed afterwards\n")
    monkeypatch.setattr(assembler.shutil, "copytree", copy_then_change)
    final = assembler.assemble(record, harvest)
    [saved] = record.bags
    for rel, digest in saved.file_sha256.items():
        assert fsio.sha256_file(final / saved.path / rel) == digest


def test_dropped_recording_is_reported_and_regraded(fairy_dirs, tmp_path,
                                                    monkeypatch):
    """A5/A13: a foreign bag vanishing mid-save is told to the operator and
    the quality verdict describes what was actually saved."""
    record, harvest, (gone, kept) = _foreign_record(tmp_path, "a", "b")
    record.provenance.data_quality = "stale"
    real_copytree = shutil.copytree

    def vanish_first(src, dst, *a, **k):
        if Path(src) == gone:
            shutil.rmtree(src)
            raise FileNotFoundError(src)
        return real_copytree(src, dst, *a, **k)
    monkeypatch.setattr(assembler.shutil, "copytree", vanish_first)
    notices = []
    final = assembler.assemble(record, harvest, warn=notices.append)
    assert "a" in notices[0] and "not in it" in notices[0]
    assert record.provenance.data_quality != "stale"
    assert "not in it" in (final / "README.md").read_text()


# -- A6: archive on another filesystem ------------------------------------------

def test_cross_device_move_verifies_and_resumes(fairy_dirs, cross_device,
                                                monkeypatch):
    harvest, context = _spool(fairy_dirs, n_bags=2)
    record = builder.build(harvest, context)
    real_rmtree = shutil.rmtree
    calls = {"n": 0}

    def crash_deleting_original(path, *a, **k):
        if str(path).startswith(str(paths.bags_dir())) and \
                not k.get("ignore_errors"):
            calls["n"] += 1
            if calls["n"] == 1:
                # half-deleted original, then the power goes
                (Path(path) / "metadata.yaml").unlink()
                raise OSError(errno.EIO, "power cut")
        return real_rmtree(path, *a, **k)

    def no_rollback(src, dest):
        raise OSError(errno.EIO, "power cut")
    monkeypatch.setattr(assembler.shutil, "rmtree", crash_deleting_original)
    monkeypatch.setattr(assembler, "_move_back", no_rollback)
    with pytest.raises(AssemblyError, match="safe in"):
        assembler.assemble(record, harvest)

    [pending] = assembler.pending_saves()
    plan = assembler._read_plan(pending.name)
    assert plan["moves"][0]["copied"] is True
    monkeypatch.setattr(assembler.shutil, "rmtree", real_rmtree)
    final = assembler.finish_pending(pending)
    assert not (paths.bags_dir() / "rosbag2_0").exists()
    assert (final / "bags" / "rosbag2_0" / "metadata.yaml").is_file()
    assert (final / "bags" / "rosbag2_1" / "metadata.yaml").is_file()


def test_cross_device_corrupt_copy_is_rejected(fairy_dirs, cross_device,
                                               monkeypatch):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    real_copytree = shutil.copytree

    def corrupting(src, dst, *a, **k):
        real_copytree(src, dst, *a, **k)
        (Path(dst) / "metadata.yaml").write_text("garbled")
    monkeypatch.setattr(assembler.shutil, "copytree", corrupting)
    with pytest.raises(AssemblyError, match="untouched in the spool"):
        assembler.assemble(record, harvest)
    assert (paths.bags_dir() / "rosbag2_0" / "metadata.yaml").is_file()


def test_cross_device_rollback_copies_back(fairy_dirs, cross_device,
                                           monkeypatch):
    harvest, context = _spool(fairy_dirs, n_bags=2)
    record = builder.build(harvest, context)
    real_move = assembler._move_bag
    calls = {"n": 0}

    def move(src, dest, *rest):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(errno.EIO, "Input/output error")
        real_move(src, dest, *rest)
    monkeypatch.setattr(assembler, "_move_bag", move)
    with pytest.raises(AssemblyError, match="back in the spool"):
        assembler.assemble(record, harvest)
    assert (paths.bags_dir() / "rosbag2_0" / "metadata.yaml").is_file()
    assert assembler.pending_saves() == []


# -- A7: one save at a time --------------------------------------------------------

def test_second_save_is_refused_while_one_runs(fairy_dirs):
    _spool(fairy_dirs)
    with assembler.save_lock():
        with pytest.raises(assembler.SaveInProgressError):
            with assembler.save_lock():
                pass
        console = _console()
        assert mission_close.run(ARGS, console=console) == 1
        assert "already running" in console.file.getvalue()
    with assembler.save_lock():  # released again
        pass


# -- A9: exact duplicates ------------------------------------------------------------

def test_exact_duplicate_found_for_the_same_mission_id(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    index.insert(record, paths.archive_dir() / "x")
    retry = builder.build(harvest, context)  # same mission_id, same bags
    assert duplicates.find_exact_duplicate(retry) is not None


# -- A12 / A15 / A16: crate contents --------------------------------------------------

def test_calibrations_with_the_same_file_name_both_kept(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    other = fairy_dirs["cfg"] / "cam"
    other.mkdir()
    (other / "gps0.yaml").write_text("fx: 2\n")
    harvest["calibrations"].append(
        {"name": "cam_cal", "source_path": str(other / "gps0.yaml"),
         "format": "yaml"})
    record = builder.build(harvest, context)
    final = assembler.assemble(record, harvest)
    stored = sorted(c.archived_path for c in record.calibrations)
    assert stored == ["calibrations/gps0.yaml", "calibrations/gps0_2.yaml"]
    for cal in record.calibrations:
        assert fsio.sha256_file(final / cal.archived_path) == cal.sha256


def test_crate_folders_are_group_writable(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    final = assembler.assemble(builder.build(harvest, context), harvest)
    for d in [final, final / "bags", final / "harvest"]:
        assert d.stat().st_mode & stat.S_IWGRP


def test_every_compose_file_is_archived(fairy_dirs, tmp_path):
    harvest, context = _spool(fairy_dirs)
    a = tmp_path / "a" / "compose.yml"
    b = tmp_path / "b" / "compose.yml"
    c = tmp_path / "c.yml"
    for f in (a, b, c):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"# {f}\n")
    containers = harvest["software"]["docker_containers"]
    containers[0].update(compose_project="jo-zotac",
                         compose_file=f"{a},{b}")
    containers.append({**containers[0], "name": "other",
                       "compose_project": "jo_zotac", "compose_file": str(c)})
    final = assembler.assemble(builder.build(harvest, context), harvest)
    compose = final / "docker" / "compose"
    assert sorted(p.relative_to(compose).as_posix()
                  for p in compose.rglob("*.yml")) == [
        "jo-zotac/compose.yml", "jo-zotac/compose_2.yml",
        "jo-zotac_2/c.yml"]


def test_no_archive_access_is_explained(fairy_dirs, monkeypatch):
    def denied(*a, **k):
        raise PermissionError(errno.EACCES, "Permission denied")
    monkeypatch.setattr(assembler.os, "open", denied)
    console = _console()
    assert mission_close.run(ARGS, console=console) == 1
    assert "ros-fairy' group" in console.file.getvalue()
