import io
import shutil
from types import SimpleNamespace
from unittest import mock

import pytest
from rich.console import Console

from ros_fairy.subcommands import add_operator
from ros_fairy.utils import operators

ACCOUNTS = {"ana": 1000, "ben": 1001, "cleo": 1002, "svc": 990}
GID = 990


@pytest.fixture
def machine():
    """ana in the group, svc with it as primary group; usermod recorded."""
    state = {"members": ["ana"], "calls": [], "fail": set()}

    def getpwnam(name):
        if name not in ACCOUNTS:
            raise KeyError(name)
        return SimpleNamespace(pw_name=name, pw_gid=1000)

    def getpwall():
        return [SimpleNamespace(pw_name=n,
                                pw_gid=GID if n == "svc" else 100)
                for n in ACCOUNTS]

    def getgrnam(name):
        return SimpleNamespace(gr_gid=GID, gr_mem=list(state["members"]))

    def run(command, check=False):
        state["calls"].append(command)
        name = command[-1]
        if name in state["fail"]:
            return SimpleNamespace(returncode=1)
        state["members"].append(name)
        return SimpleNamespace(returncode=0)

    with mock.patch.object(operators.pwd, "getpwnam", side_effect=getpwnam), \
            mock.patch.object(operators.pwd, "getpwall", side_effect=getpwall), \
            mock.patch.object(operators.grp, "getgrnam", side_effect=getgrnam), \
            mock.patch.object(operators.subprocess, "run", side_effect=run):
        yield state


def test_parse_names():
    assert operators.parse_names(" ben, cleo  ben\tana ") == \
        ["ben", "cleo", "ana"]
    assert operators.parse_names("") == []


def test_members_include_primary_group(machine):
    assert operators.members() == ["ana", "svc"]


def test_add_reports_each_account(machine):
    machine["fail"].add("cleo")
    outcome = operators.add(["ana", "ben", "cleo", "nobody"])
    assert outcome == {"ana": "already", "ben": "added", "cleo": "failed",
                       "nobody": "no_account"}
    assert machine["calls"] == [["usermod", "-aG", "ros-fairy", "ben"],
                                ["usermod", "-aG", "ros-fairy", "cleo"]]


def test_add_through_sudo(machine):
    operators.add(["ben"], use_sudo=True)
    assert machine["calls"] == [["sudo", "usermod", "-aG", "ros-fairy", "ben"]]


def _run(users, euid=1000, me="ana"):
    console = Console(file=io.StringIO(), width=200)
    with mock.patch.object(add_operator.os, "geteuid", return_value=euid), \
            mock.patch.object(add_operator.shutil, "which",
                              return_value="/usr/bin/sudo"), \
            mock.patch.object(operators.getpass, "getuser",
                              return_value=me), \
            mock.patch.dict(add_operator.os.environ, {}, clear=True):
        code = add_operator.run(SimpleNamespace(users=users),
                                console=console)
    return code, console.file.getvalue()


def test_verb_adds_through_sudo_and_explains_next_login(machine):
    code, out = _run(["ben", "ana"])
    assert code == 0
    assert machine["calls"] == [["sudo", "usermod", "-aG", "ros-fairy", "ben"]]
    assert "password" in out
    assert "✓ ben added" in out
    assert "ana was already an operator" in out
    assert "next login" in out
    assert "For you" not in out


def test_verb_tells_you_about_newgrp_when_adding_yourself(machine):
    code, out = _run(["ben"], me="ben")
    assert code == 0
    assert "newgrp ros-fairy" in out
    assert "For you" in out


def test_verb_as_root_doesnt_use_sudo(machine):
    code, out = _run(["ben"], euid=0)
    assert code == 0
    assert machine["calls"] == [["usermod", "-aG", "ros-fairy", "ben"]]
    assert "password" not in out


def test_verb_fails_for_unknown_or_failed_accounts(machine):
    machine["fail"].add("cleo")
    code, out = _run(["nobody", "cleo", "ben"])
    assert code == 1
    assert "nobody: there's no account" in out
    assert "cleo: `usermod` failed" in out
    assert "✓ ben added" in out


def test_verb_needs_setup_first():
    console = Console(file=io.StringIO(), width=200)
    with mock.patch.object(operators.grp, "getgrnam", side_effect=KeyError):
        assert add_operator.run(SimpleNamespace(users=["ben"]),
                                console=console) == 1
    assert "ros2 fairy setup" in console.file.getvalue()


def test_verb_without_sudo_says_run_as_root(machine):
    console = Console(file=io.StringIO(), width=200)
    with mock.patch.object(add_operator.os, "geteuid", return_value=1000), \
            mock.patch.object(add_operator.shutil, "which", return_value=None):
        assert add_operator.run(SimpleNamespace(users=["ben"]),
                                console=console) == 1
    assert machine["calls"] == []
    assert "as root" in console.file.getvalue()


def test_no_access_problem_with_access(fairy_dirs):
    assert operators.access_problem() is None  # writable spool


def test_before_setup_says_so(fairy_dirs):
    # The verbs used to go on and fail at the first folder they couldn't
    # create ("something went wrong that ros-fairy didn't expect").
    shutil.rmtree(operators.paths.spool_dir())
    assert "isn't set up yet" in operators.access_problem()
    # Set up, but the data folder isn't there (a disk not mounted): the
    # verbs say what is missing themselves.
    operators.paths.robot_identity_path().write_text("robot: {}\n")
    assert operators.access_problem() is None


def _locked_spool(fairy_dirs, monkeypatch, members, groups):
    monkeypatch.setattr(operators.os, "access", lambda p, mode: False)
    monkeypatch.setattr(operators.grp, "getgrnam",
                        lambda n: SimpleNamespace(gr_gid=GID, gr_mem=members))
    monkeypatch.setattr(operators.pwd, "getpwall", lambda: [])
    monkeypatch.setattr(operators.os, "getgroups", lambda: groups)
    monkeypatch.setattr(operators.getpass, "getuser", lambda: "dan")


def test_access_problem_outside_the_group(fairy_dirs, monkeypatch):
    _locked_spool(fairy_dirs, monkeypatch, members=["ana"], groups=[1004])
    problem = operators.access_problem()
    assert "(dan)" in problem
    assert "ros2 fairy add_operator dan" in problem


def test_access_problem_added_after_login(fairy_dirs, monkeypatch):
    _locked_spool(fairy_dirs, monkeypatch, members=["dan"], groups=[1004])
    assert "newgrp ros-fairy" in operators.access_problem()


def test_verbs_refuse_before_asking_anything(fairy_dirs, monkeypatch):
    from ros_fairy.subcommands import (
        adopt,
        mission_abort,
        mission_close,
        mission_record,
        mission_start,
    )
    monkeypatch.setattr(operators, "access_problem",
                        lambda: "not an operator")
    for verb in (mission_start, mission_record, mission_close, mission_abort):
        console = Console(file=io.StringIO(), width=200)
        assert verb.run(SimpleNamespace(), console=console) == 1, verb
        assert "not an operator" in console.file.getvalue()
    console = Console(file=io.StringIO(), width=200)
    assert adopt.run(SimpleNamespace(bagdir="/x"), console=console) == 1


@pytest.mark.parametrize("euid, sudo_user, expected", [
    (0, "ana", "ana"),      # sudo ros2 fairy ...: the person behind sudo
    (0, "root", "me"),      # sudo from a root shell
    (1000, "root", "me"),   # a shell started through sudo, not root now
    (1000, "", "me"),
])
def test_invoking_user(monkeypatch, euid, sudo_user, expected):
    monkeypatch.setattr(operators.os, "geteuid", lambda: euid)
    monkeypatch.setattr(operators.getpass, "getuser", lambda: "me")
    monkeypatch.setenv("SUDO_USER", sudo_user)
    assert operators.invoking_user() == expected
