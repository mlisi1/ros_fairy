"""Where missions are kept: paths.yaml, setup --var-dir, move_data, doctor."""

import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from rich.console import Console

from ros_fairy.archive import assembler, index
from ros_fairy.manifest import builder
from ros_fairy.subcommands import doctor, guarded_main, move_data
from ros_fairy.subcommands import setup as setup_cmd
from ros_fairy.utils import paths
from tests.unit.test_archive import _spool


def _console():
    return Console(file=io.StringIO(), width=200)


@pytest.fixture
def configured(fairy_dirs, monkeypatch):
    """The data folder set by paths.yaml rather than the environment, as on
    a robot."""
    monkeypatch.delenv(paths.VAR_DIR_ENV)
    setup_cmd.write_paths_file(fairy_dirs["var"])
    return fairy_dirs


# --- paths -------------------------------------------------------------------

def test_var_dir_precedence(fairy_dirs, monkeypatch, tmp_path):
    assert paths.var_dir() == fairy_dirs["var"]
    assert paths.var_dir_source() == "environment"
    monkeypatch.delenv(paths.VAR_DIR_ENV)
    assert paths.var_dir() == Path("/var/ros-fairy")
    assert paths.var_dir_source() == "default"
    paths.paths_file().write_text("var_dir: /data/ros-fairy\n")
    assert paths.var_dir() == Path("/data/ros-fairy")
    assert paths.var_dir_source() == "paths.yaml"
    # A change to the file is seen at once (the cache follows it).
    paths.paths_file().write_text("var_dir: /data/elsewhere\n")
    os.utime(paths.paths_file(), ns=(1, 1))
    assert paths.var_dir() == Path("/data/elsewhere")
    monkeypatch.setenv(paths.VAR_DIR_ENV, str(tmp_path))
    assert paths.var_dir() == tmp_path


@pytest.mark.parametrize("text, says", [
    ("var_dir: data/fairy\n", "isn't an absolute path"),
    ("other: 1\n", "has no `var_dir"),
    ("var_dir: [\n", "can't be read"),
])
def test_broken_paths_file_is_an_error_not_the_default(
        fairy_dirs, monkeypatch, text, says):
    monkeypatch.delenv(paths.VAR_DIR_ENV)
    paths.paths_file().write_text(text)
    with pytest.raises(paths.PathsConfigError, match=says):
        paths.var_dir()


def test_verbs_show_the_paths_error_plainly(fairy_dirs, monkeypatch, capsys):
    monkeypatch.delenv(paths.VAR_DIR_ENV)
    paths.paths_file().write_text("var_dir: relative\n")

    def run(args):
        return len(str(paths.spool_dir()))
    assert guarded_main(run, SimpleNamespace()) == 1
    err = capsys.readouterr().err
    assert "isn't an absolute path" in err
    assert "something went wrong" not in err


# --- setup ---------------------------------------------------------------------

def test_write_paths_file(fairy_dirs):
    setup_cmd.write_paths_file(Path("/data/ros-fairy"))
    assert "var_dir: /data/ros-fairy" in paths.paths_file().read_text()
    assert paths.paths_file().stat().st_mode & 0o777 == 0o644
    setup_cmd.write_paths_file(Path("/var/ros-fairy"))  # the default
    assert not paths.paths_file().exists()


def test_data_dir_problem(fairy_dirs, tmp_path):
    current = fairy_dirs["var"]
    assert setup_cmd.data_dir_problem(str(current), current) is None
    assert setup_cmd.data_dir_problem(str(tmp_path / "new"), current) is None
    assert "absolute" in setup_cmd.data_dir_problem("data", current)
    assert "configuration folder" in setup_cmd.data_dir_problem(
        str(fairy_dirs["cfg"] / "x"), current)
    busy = tmp_path / "busy"
    (busy / "photos").mkdir(parents=True)
    problem = setup_cmd.data_dir_problem(str(busy), current)
    assert "photos" in problem and str(busy / "ros-fairy") in problem
    # A folder that only holds ros_fairy's own (empty) layout is fine.
    old = tmp_path / "old"
    (old / "spool").mkdir(parents=True)
    # Written by a running watchdog, even with no mission.
    (old / "watchdog.state").write_text("{}")
    (old / "ros_processes.json").write_text("{}")
    assert setup_cmd.data_dir_problem(str(old), current) is None


def test_data_dir_problem_points_at_move_data_when_missions_exist(
        fairy_dirs, tmp_path):
    harvest, context = _spool(fairy_dirs)
    assembler.assemble(builder.build(harvest, context), harvest)
    problem = setup_cmd.data_dir_problem(str(tmp_path / "new"),
                                         fairy_dirs["var"])
    assert f"ros2 fairy move_data {tmp_path / 'new'}" in problem


def test_ask_var_dir_reasks(fairy_dirs, tmp_path):
    answers = iter(["relative/path", str(tmp_path / "data/")])
    console = _console()
    with mock.patch.object(setup_cmd.Prompt, "ask",
                           side_effect=lambda *a, **k: next(answers)):
        assert setup_cmd.ask_var_dir(console, fairy_dirs["var"]) == \
            tmp_path / "data"
    assert "absolute" in console.file.getvalue()


def test_run_var_dir_refused_when_the_shell_overrides_it(fairy_dirs,
                                                          tmp_path):
    console = _console()
    args = SimpleNamespace(var_dir=str(tmp_path / "data"))
    with mock.patch.object(setup_cmd.os, "geteuid", return_value=1000), \
            mock.patch.object(setup_cmd, "_check_ros_visible",
                              return_value=True), \
            mock.patch.object(setup_cmd, "_choose_service_python",
                              return_value="/usr/bin/python3"), \
            mock.patch.object(setup_cmd, "_collect") as collect:
        assert setup_cmd.run(args, console=console) == 1
    collect.assert_not_called()
    assert "ROS_FAIRY_VAR_DIR is set" in console.file.getvalue()


def test_apply_writes_paths_file_before_the_folders(configured, tmp_path):
    new = tmp_path / "data"
    payload = {"config": {"robot": {"name": "X"}}, "env": {},
               "var_dir": str(new)}
    own_group = SimpleNamespace(gr_gid=os.getgid())  # no real ros-fairy group
    with mock.patch.object(setup_cmd, "write_identity"), \
            mock.patch.object(setup_cmd, "add_operators"), \
            mock.patch.object(setup_cmd.grp, "getgrnam",
                              return_value=own_group), \
            mock.patch.object(setup_cmd, "install_service",
                              return_value=True):
        assert setup_cmd._apply(_console(), payload) is True
    assert paths.var_dir() == new
    assert (new / "spool" / "bags").is_dir()


# --- move_data -----------------------------------------------------------------

def _files(root):
    """The tree without the index (the move rebuilds it) and the save lock
    (the move holds it)."""
    return {k: v for k, v in move_data._tree(root).items()
            if not k.startswith("index.db") and not k.endswith(".lock")}


def _saved_mission(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    record = builder.build(harvest, context)
    return record, assembler.assemble(record, harvest)


def _move(target, yes=True):
    console = _console()
    calls = []
    with mock.patch.object(move_data.os, "geteuid", return_value=0), \
            mock.patch.object(move_data, "_systemctl",
                              side_effect=lambda *a: calls.append(a) or True):
        code = move_data.run(SimpleNamespace(target=str(target), yes=yes),
                             console=console)
    return code, console.file.getvalue(), calls


def test_move_on_the_same_disk(configured, tmp_path):
    record, crate = _saved_mission(configured)
    target = tmp_path / "disk" / "ros-fairy"
    code, out, calls = _move(target)
    assert code == 0, out
    assert not configured["var"].exists()
    assert paths.var_dir() == target
    assert (target / "archive" / crate.name / "mission_record.json").is_file()
    [row], _ = index.query()
    assert row["archive_path"] == str(target / "archive" / crate.name)
    assert ("stop", move_data.SERVICE_NAME) in calls
    assert "now kept in" in out


def test_move_to_another_disk_copies_and_checks(configured, tmp_path,
                                                monkeypatch):
    _, crate = _saved_mission(configured)
    before = _files(configured["var"])
    # Pretend the target is on another filesystem.
    monkeypatch.setattr(move_data, "_same_disk", lambda a, b: False)
    target = tmp_path / "disk" / "ros-fairy"
    target.parent.mkdir()
    code, out, _ = _move(target)
    assert code == 0, out
    assert _files(target) == before
    assert index.query()[1] == 1
    assert not configured["var"].exists()
    assert not (target.parent / ".ros-fairy.moving").exists()
    assert paths.var_dir() == target


def test_failed_copy_leaves_everything_as_it_was(configured, tmp_path,
                                                 monkeypatch):
    _saved_mission(configured)
    before = _files(configured["var"])
    monkeypatch.setattr(move_data, "_same_disk", lambda a, b: False)
    real_tree = move_data._tree
    monkeypatch.setattr(move_data, "_tree", lambda root: {
        **real_tree(root), **({"extra": 1} if ".moving" in str(root)
                               else {})})
    target = tmp_path / "disk" / "ros-fairy"
    with mock.patch.object(move_data, "_start_watchdog") as start:
        code, out, calls = _move(target)
    start.assert_called_once()
    assert code == 1
    assert "doesn't match the original" in out
    assert _files(configured["var"]) == before
    assert index.query()[1] == 1
    assert not target.exists()
    assert not (target.parent / ".ros-fairy.moving").exists()
    assert paths.var_dir() == configured["var"]


def test_move_into_an_existing_empty_folder_stages_inside_it(
        configured, tmp_path, monkeypatch):
    """An empty target may be another disk's mount point: the disk check and
    the copy use the target itself, and the folder is kept, not replaced."""
    _saved_mission(configured)
    before = _files(configured["var"])
    target = tmp_path / "mnt"
    target.mkdir()
    compared = []
    monkeypatch.setattr(move_data, "_same_disk",
                        lambda a, b: compared.append(a) or False)
    real_run = move_data.subprocess.run
    copies = []

    def run(command, *args, **kwargs):
        if command[0] == "cp":
            copies.append(Path(command[-1]))
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(move_data.subprocess, "run", run)
    inode = target.stat().st_ino
    code, out, _ = _move(target)
    assert code == 0, out
    assert compared == [target]
    assert [c.parent for c in copies] == [target]
    assert target.stat().st_ino == inode  # the same folder, filled
    assert _files(target) == before
    assert not list(target.glob(".*.moving"))
    assert not list(tmp_path.glob(".*.moving"))
    assert paths.var_dir() == target


def test_move_restarts_the_watchdog_when_the_old_folder_stays(
        configured, tmp_path, monkeypatch):
    _saved_mission(configured)
    monkeypatch.setattr(move_data, "_same_disk", lambda a, b: False)
    real_rmtree = move_data.shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if Path(path) == configured["var"]:
            raise PermissionError(13, "Permission denied", str(path))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(move_data.shutil, "rmtree", rmtree)
    target = tmp_path / "disk" / "ros-fairy"
    with mock.patch.object(move_data, "_start_watchdog") as start:
        code, out, _ = _move(target)
    assert code == 0, out
    start.assert_called_once()
    assert "couldn't be removed" in out
    assert index.query()[1] == 1  # reindexed in the new place
    assert paths.var_dir() == target


def test_same_disk_move_is_undone_when_paths_file_fails(
        configured, tmp_path, monkeypatch):
    _saved_mission(configured)
    before = _files(configured["var"])
    target = tmp_path / "disk" / "ros-fairy"
    monkeypatch.setattr(setup_cmd, "write_paths_file",
                        mock.Mock(side_effect=OSError("read-only")))
    with mock.patch.object(move_data, "_start_watchdog") as start, \
            pytest.raises(OSError):
        _move(target)
    start.assert_called_once()
    assert _files(configured["var"]) == before
    assert not target.exists()


@pytest.mark.parametrize("alive, busy", [(True, True), (False, False)])
def test_finalising_counts_only_while_the_watchdog_lives(
        monkeypatch, alive, busy):
    from ros_fairy.subcommands import mission_close
    from ros_fairy.ui import status
    from ros_fairy.watchdog import watchdog as wd
    monkeypatch.setattr(mission_close, "_recording_in_progress",
                        lambda: False)
    monkeypatch.setattr(wd, "read_state",
                        lambda: {"state": "FINALISING", "pid": 4242})
    monkeypatch.setattr(status, "watchdog_alive", lambda state: alive)
    assert move_data._watchdog_busy() is busy


@pytest.mark.parametrize("setup_state, target, says", [
    ("env", "/data/x", "ROS_FAIRY_VAR_DIR is set"),
    (None, "data/x", "absolute"),
    (None, "SAME", "already kept"),
    (None, "INSIDE", "inside one another"),
    (None, "NONEMPTY", "isn't an empty folder"),
    ("open", "/data/x", "A mission is open"),
    ("staging", "/data/x", "save is unfinished"),
])
def test_move_refusals(configured, tmp_path, monkeypatch, setup_state,
                       target, says):
    var = configured["var"]
    if setup_state == "env":
        monkeypatch.setenv(paths.VAR_DIR_ENV, str(var))
    if setup_state == "open":
        paths.mission_context_path().write_text("{}")
    if setup_state == "staging":
        (paths.staging_dir() / "half").mkdir(parents=True)
    (paths.staging_dir()).mkdir(exist_ok=True)
    (paths.staging_dir() / ".lock").touch()  # always there; not a save
    nonempty = tmp_path / "full"
    (nonempty / "x").mkdir(parents=True)
    target = {"SAME": str(var), "INSIDE": str(var / "sub"),
              "NONEMPTY": str(nonempty)}.get(target, target)
    code, out, calls = _move(target)
    assert code == 1
    assert says in out
    assert calls == []
    assert paths.var_dir() == var


def test_move_asks_and_uses_sudo_when_not_root(configured, tmp_path):
    console = _console()
    with mock.patch.object(move_data.os, "geteuid", return_value=1000), \
            mock.patch.object(move_data.shutil, "which",
                              return_value="/usr/bin/sudo"), \
            mock.patch.object(move_data, "confirm", return_value=True), \
            mock.patch.object(move_data.subprocess, "run",
                              return_value=SimpleNamespace(returncode=0)
                              ) as run:
        assert move_data.run(SimpleNamespace(target=str(tmp_path / "d")),
                             console=console) == 0
    command = run.call_args.args[0]
    assert command[0] == "/usr/bin/sudo"
    assert command[-3:] == ["ros_fairy.subcommands.move_data",
                            str(tmp_path / "d"), "--confirmed"]


def test_move_declined_changes_nothing(configured, tmp_path):
    with mock.patch.object(move_data, "confirm", return_value=False):
        assert move_data.run(SimpleNamespace(target=str(tmp_path / "d")),
                             console=_console()) == 0
    assert paths.var_dir() == configured["var"]


# --- doctor --------------------------------------------------------------------

def test_doctor_data_dir(configured, monkeypatch, tmp_path):
    result = doctor._check_data_dir()
    assert result["status"] == doctor.OK
    assert str(configured["var"]) in result["title"]
    assert "paths.yaml" in result["detail"]
    monkeypatch.setenv(paths.VAR_DIR_ENV, str(tmp_path / "other"))
    result = doctor._check_data_dir()
    assert result["status"] == doctor.WARN
    assert "recording assistant uses" in result["detail"]


def test_doctor_data_dir_broken_file(fairy_dirs, monkeypatch):
    monkeypatch.delenv(paths.VAR_DIR_ENV)
    paths.paths_file().write_text("nothing: here\n")
    assert doctor._check_data_dir()["status"] == doctor.FAIL


def test_doctor_json_includes_data_dir(configured, capsys):
    doctor.run(SimpleNamespace(json=True), console=_console())
    titles = [c["title"] for c in json.loads(capsys.readouterr().out)["checks"]]
    assert any(t.startswith("Missions are kept in") for t in titles)
