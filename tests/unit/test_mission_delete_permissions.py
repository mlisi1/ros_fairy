"""mission_delete decides on sudo before deleting any part of a mission."""

import os
from unittest import mock

import pytest

from ros_fairy.archive import index
from ros_fairy.subcommands import mission_delete
from tests.unit.test_subcommands import _delete, _saved

pytestmark = pytest.mark.skipif(os.geteuid() == 0,
                                reason="root ignores folder permissions")


def _lock_part_of(crate):
    """A sub-folder this account can't empty, as when another account's
    files sit inside the mission."""
    locked = crate / "locked"
    locked.mkdir()
    (locked / "theirs.txt").write_text("x")
    locked.chmod(0o555)
    return locked


def test_declining_sudo_leaves_the_mission_whole(fairy_dirs):
    [(mission_id, crate)] = _saved(fairy_dirs,
                                   "2026-06-10T09:00:00+00:00").items()
    locked = _lock_part_of(crate)
    before = sorted(p.relative_to(crate) for p in crate.rglob("*"))
    try:
        with mock.patch.object(mission_delete.shutil, "rmtree") as rmtree:
            rc, out, calls, _ = _delete({"mission_id": mission_id},
                                        confirm=(True, False))
        rmtree.assert_not_called()
        assert rc == 1
        assert "Not deleted" in out and "permission denied" in out
        assert sorted(p.relative_to(crate) for p in crate.rglob("*")) == before
        assert [r["mission_id"] for r in index.query(limit=None)[0]] == \
            [mission_id]
    finally:
        locked.chmod(0o755)


def test_accepting_sudo_deletes_it_all(fairy_dirs):
    [(mission_id, crate)] = _saved(fairy_dirs,
                                   "2026-06-10T09:00:00+00:00").items()
    locked = _lock_part_of(crate)
    locked.chmod(0o755)  # the simulated `sudo rm -rf` is this account
    with mock.patch.object(mission_delete, "_can_remove", return_value=False):
        rc, out, calls, _ = _delete({"mission_id": mission_id},
                                    confirm=(True, True))
    assert rc == 0, out
    assert ["sudo", "rm", "-rf", "--", str(crate)] in calls
    assert not crate.exists()
    assert index.query(limit=None)[0] == []


def test_can_remove_sees_the_whole_tree(fairy_dirs, tmp_path):
    crate = tmp_path / "crate"
    (crate / "a" / "b").mkdir(parents=True)
    (crate / "a" / "b" / "f").write_text("x")
    assert mission_delete._can_remove(crate)
    (crate / "a" / "b").chmod(0o555)
    try:
        assert not mission_delete._can_remove(crate)
    finally:
        (crate / "a" / "b").chmod(0o755)
