"""ros2 fairy verb implementations.

Each module exposes a plain ``run(args, console) -> int`` (unit-testable
without ROS) plus a thin ros2cli VerbExtension wrapper. The shim below lets
the modules import in environments without ros2cli (CI, unit tests).
"""

import json
import logging
import sys
from typing import Any

try:
    from ros2cli.verb import VerbExtension
except ImportError:  # pragma: no cover - exercised only outside ROS
    class VerbExtension:  # type: ignore[no-redef]
        """Stand-in with the same interface as ros2cli's VerbExtension."""

        def add_arguments(self, parser, cli_name):
            pass


def _configure_logging(debug: bool) -> None:
    if debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
            stream=sys.stderr,
        )


def print_json(document: Any) -> None:
    """Write ``document`` as JSON on stdout — and nothing else.

    Never through rich: it wraps long strings at the terminal width and eats
    ``[...]`` as markup, which made `--json` output unparseable.
    """
    sys.stdout.write(json.dumps(document, indent=2, default=str) + "\n")
    sys.stdout.flush()


def json_error(message: str, code: int = 1, **extra: Any) -> int:
    """The `--json` form of a plain-language failure."""
    print_json({"status": "error", "error": message, **extra})
    return code


def confirm(question: str, *, default: bool, console,
            assume_yes: bool = False) -> bool:
    """Ask a yes/no question; ``assume_yes`` (a verb's --yes) answers it."""
    if assume_yes:
        console.print(f"{question} [dim]yes (--yes)[/dim]")
        return True
    from rich.prompt import Confirm
    return Confirm.ask(question, default=default, console=console)


NO_TERMINAL = ("This step needs an answer, but there's no terminal to type "
               "it in. Run the command in a terminal, or use its --yes "
               "option to accept the defaults.")


def guarded_main(run, args) -> int:
    """The verb boundary: nothing past here may show an operator a traceback.

    Verbs handle their *known* failures with plain language; this catches the
    unexpected rest (no stack traces in normal flow) and turns
    Ctrl-C into the mandated exit 130. ``--debug`` still gets the full
    traceback on stderr — that flag marks an engineer. With ``--json``, every
    outcome is JSON on stdout.
    """
    as_json = getattr(args, "json", False)
    try:
        return run(args)
    except KeyboardInterrupt:
        if as_json:
            return json_error("cancelled", 130)
        print("\nCancelled — nothing was changed.", file=sys.stderr)
        return 130
    except EOFError:
        # A prompt with no terminal behind stdin (a script, a pipe, ssh -T).
        if as_json:
            return json_error(NO_TERMINAL, 2)
        print(f"\n{NO_TERMINAL}", file=sys.stderr)
        return 2
    except Exception as exc:
        if getattr(args, "debug", False):
            raise
        if as_json:
            return json_error("unexpected error: "
                              f"{type(exc).__name__}: {exc}")
        print("Sorry — something went wrong that ros-fairy didn't expect.\n"
              "Nothing is lost: your recordings and saved missions are not\n"
              "touched by this error. Re-run the same command with --debug\n"
              "and share the output with your robot engineer.",
              file=sys.stderr)
        return 1
