"""The arrow keys edit the answer in every prompt (setup, the briefing, …)."""

import os
import pty
import sys
import time

CHILD = """
from types import SimpleNamespace
from rich.prompt import Prompt
from ros_fairy.subcommands import guarded_main

def run(args):
    print("ANSWER", repr(Prompt.ask("Goal")))
    return 0

guarded_main(run, SimpleNamespace())
"""


def _type_into_prompt(keys: bytes) -> str:
    """Run a prompt behind guarded_main in a pseudo-terminal, as an operator
    would see it, type ``keys`` and return what came out."""
    pid, fd = pty.fork()
    if pid == 0:  # the child: a fresh interpreter, nothing loaded yet
        os.execvp(sys.executable, [sys.executable, "-c", CHILD])
    out = b""
    deadline = time.monotonic() + 20
    sent = False
    while time.monotonic() < deadline:
        try:
            chunk = os.read(fd, 4096)
        except OSError:  # the child exited
            break
        if not chunk:
            break
        out += chunk
        if not sent and b"Goal" in out:
            os.write(fd, keys)
            sent = True
    os.waitpid(pid, 0)
    return out.decode(errors="replace")


def test_arrow_keys_move_the_cursor_in_prompts():
    # type "ab", Left, Left, "X", Enter
    out = _type_into_prompt(b"ab\x1b[D\x1b[DX\r")
    assert "ANSWER 'Xab'" in out, out
