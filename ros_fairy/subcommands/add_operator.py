"""ros2 fairy add_operator — let more accounts record missions.

Adds accounts to the ``ros-fairy`` group, which owns the spool, the archive
and the index. Uses sudo for the one ``usermod`` call when not run as root.
"""

import os
import shutil

from rich.console import Console

from ros_fairy.subcommands import VerbExtension, _configure_logging, guarded_main
from ros_fairy.utils import operators

_LINES = {
    operators.ADDED: ("green", "✓ {n} added"),
    operators.ALREADY: ("dim", "– {n} was already an operator"),
    operators.NO_ACCOUNT: ("red", "✗ {n}: there's no account with that name"),
    operators.FAILED: ("red", "✗ {n}: `usermod` failed"),
}


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()
    names = list(dict.fromkeys(getattr(args, "users", None) or []))
    group = operators.GROUP_NAME

    if not operators.group_exists():
        console.print(f"[red]The '{group}' group doesn't exist yet: set the "
                      "robot up first with `ros2 fairy setup`.[/red]")
        return 1

    as_root = os.geteuid() == 0
    current = set(operators.members())
    to_add = [n for n in names
              if n not in current and operators.account_exists(n)]
    if to_add and not as_root:
        if shutil.which("sudo") is None:
            console.print("[red]Adding accounts to a group needs root, and "
                          "`sudo` isn't available. Run this command as root."
                          "[/red]")
            return 1
        console.print("[dim]Adding accounts to a group needs root: you may "
                      "be asked for your password.[/dim]")

    outcome = operators.add(names, use_sudo=not as_root)
    for name in names:
        style, text = _LINES[outcome[name]]
        console.print(f"[{style}]{text.format(n=name)}[/{style}]")

    added = [n for n in names if outcome[n] == operators.ADDED]
    if added:
        console.print(operators.NEXT_LOGIN)
        if operators.invoking_user() in added:
            console.print(f"For you: log out and back in, or run "
                          f"[bold]newgrp {group}[/bold] in this terminal.")
    return 0 if all(outcome[n] in (operators.ADDED, operators.ALREADY)
                    for n in names) else 1


class AddOperatorVerb(VerbExtension):
    """Let more accounts record missions (adds them to the ros-fairy group)."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument(
            "users", nargs="+", metavar="USER",
            help="account name(s) to add")
        parser.add_argument("--debug", action="store_true",
                            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        return guarded_main(run, args)
