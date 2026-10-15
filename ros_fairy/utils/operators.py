"""Operator accounts: the members of the ``ros-fairy`` group.

Recording and saving missions needs write access to the spool, the archive
and the index, which belong to this group. ``setup`` adds the accounts it is
given; ``ros2 fairy add_operator`` adds more later.
"""

import getpass
import grp
import os
import pwd
import re
import subprocess

from ros_fairy.utils import paths

GROUP_NAME = "ros-fairy"
ADD_HINT = "ros2 fairy add_operator <user>"
NEXT_LOGIN = (f"Group membership applies from each account's next login. In "
              f"a terminal that's already open, `newgrp {GROUP_NAME}` starts "
              "a shell that has it.")

ADDED, ALREADY, NO_ACCOUNT, FAILED = "added", "already", "no_account", "failed"


def invoking_user() -> str:
    """The person running the command: under sudo, the account behind it.
    SUDO_USER only counts when we really run as root; a shell that was
    itself started through sudo still carries it."""
    behind = os.environ.get("SUDO_USER", "")
    if os.geteuid() == 0 and behind and behind != "root":
        return behind
    return getpass.getuser()


def parse_names(text: str) -> list[str]:
    """Account names typed as a list: spaces or commas between them,
    duplicates dropped, order kept."""
    names = [n for n in re.split(r"[\s,]+", text.strip()) if n]
    return list(dict.fromkeys(names))


def account_exists(name: str) -> bool:
    try:
        pwd.getpwnam(name)
    except KeyError:
        return False
    return True


def group_exists() -> bool:
    try:
        grp.getgrnam(GROUP_NAME)
    except KeyError:
        return False
    return True


def members() -> list[str]:
    """Accounts in the group, including those whose primary group it is.
    Empty when the group doesn't exist yet."""
    try:
        group = grp.getgrnam(GROUP_NAME)
    except KeyError:
        return []
    primary = [p.pw_name for p in pwd.getpwall() if p.pw_gid == group.gr_gid]
    return sorted(set(group.gr_mem) | set(primary))


def add(names: list[str], *, use_sudo: bool = False) -> dict[str, str]:
    """Add each account to the group: name -> ADDED, ALREADY, NO_ACCOUNT or
    FAILED. ``use_sudo`` runs ``usermod`` through sudo (for a non-root
    caller); otherwise the caller must be root."""
    current = set(members())
    outcome: dict[str, str] = {}
    for name in names:
        if not account_exists(name):
            outcome[name] = NO_ACCOUNT
        elif name in current:
            outcome[name] = ALREADY
        else:
            command = ["usermod", "-aG", GROUP_NAME, name]
            result = subprocess.run(["sudo", *command] if use_sudo
                                    else command, check=False)
            outcome[name] = ADDED if result.returncode == 0 else FAILED
    return outcome


def access_problem() -> str | None:
    """Why this account can't record or save missions here, in plain words,
    or None. Checked only once the robot is set up (the spool exists)."""
    spool = paths.spool_dir()
    if not spool.is_dir() or os.access(spool, os.W_OK):
        return None
    try:
        gid = grp.getgrnam(GROUP_NAME).gr_gid
    except KeyError:
        return None
    me = getpass.getuser()
    if me in members() and gid not in os.getgroups():
        return (f"You were added to the '{GROUP_NAME}' group after this "
                "terminal was opened. Log out and back in, or run "
                f"`newgrp {GROUP_NAME}` here, then try again.")
    return (f"Your account ({me}) can't record missions on this robot yet: "
            f"it isn't in the '{GROUP_NAME}' group. Ask your engineer to "
            f"run: ros2 fairy add_operator {me}")
