"""The mission_start briefing wizard.

Exactly five questions, plain language, under two minutes. Also reused by
mission_close to fill in required answers the operator skipped.
"""

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from ros_fairy.manifest import validator

# (field, prompt text, required) — order and wording are fixed
QUESTIONS = [
    ("operator_name", "What's your name?", True),
    ("goal", "In one sentence, what is this mission trying to do?", True),
    ("location_name",
     "Where are you? (place name, e.g. 'Marsh Creek, north bank')", True),
    ("environment",
     "What's the environment like? (e.g. outdoor, indoor, marine — press "
     "Enter to skip)", False),
    ("notes", "Anything else worth noting? (press Enter to skip)", False),
]


class _Question(Prompt):
    """No ": " after a question mark (Rich's suffix would make "?:")."""
    prompt_suffix = " "


def ask(prompt: str, console: Console, **kwargs: Any) -> str:
    """Prompt.ask, with the suffix that fits what ends the line: the
    question, or the "(default)" Rich shows after it."""
    shows_default = kwargs.get("default", ...) not in (..., "") and \
        kwargs.get("show_default", True)
    kind = _Question if prompt.endswith("?") and not shows_default \
        else Prompt
    return kind.ask(prompt, console=console, **kwargs)


def _ask_one(console: Console, field: str, prompt: str, required: bool,
             default: str | None) -> str | None:
    while True:
        kwargs: dict[str, Any] = {"default": default} if default else {}
        answer = ask(prompt, console, **kwargs)
        answer = (answer or "").strip()
        error = validator.length_error(field, answer)
        if error:
            console.print(f"[yellow]{error} Please shorten it.[/yellow]")
            continue
        if answer:
            return answer
        if not required:
            return None
        console.print("[yellow]This one I really need — it's what makes the "
                      "mission findable later.[/yellow]")


def ask_briefing(console: Console | None = None,
                 default_operator: str | None = None) -> dict:
    """Run the full five-question briefing; returns field -> answer."""
    console = console or Console()
    console.print(Panel("Quick mission briefing — five questions, "
                        "under two minutes.", title="ros-fairy",
                        border_style="cyan"))
    answers = {}
    for field, prompt, required in QUESTIONS:
        default = default_operator if field == "operator_name" else None
        answers[field] = _ask_one(console, field, prompt, required, default)
    return answers


def ask_missing(fields: list[str], console: Console | None = None) -> dict:
    """Ask only the named required questions (gap-fill at mission_close)."""
    console = console or Console()
    by_field = {f: (p, r) for f, p, r in QUESTIONS}
    console.print("A few details are missing before this mission can be "
                  "saved:")
    answers = {}
    for field in fields:
        prompt, required = by_field[field]
        answers[field] = _ask_one(console, field, prompt, required, None)
    return answers


def ask_notes(console: Console | None = None,
              default: str | None = None) -> str | None:
    """Ask Q5 (notes) standalone — used at mission_close for post-mission annotation.

    Existing notes are offered as the default so the operator can keep or
    replace them. Returns None when the operator presses Enter with no input
    and no default.
    """
    console = console or Console()
    _, prompt, _ = next(q for q in QUESTIONS if q[0] == "notes")
    return _ask_one(console, "notes", prompt, False, default)
