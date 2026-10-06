"""ros2 fairy mission_status — read-only status display."""

from rich.console import Console

from ros_fairy.manifest import builder
from ros_fairy.subcommands import (
    VerbExtension,
    _configure_logging,
    guarded_main,
    print_json,
)
from ros_fairy.ui import status as status_ui
from ros_fairy.watchdog import watchdog as wd


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()
    state = wd.read_state()
    context = builder.load_spool()[1]
    if getattr(args, "json", False):
        print_json(status_ui.status_as_dict(state, context))
        return 0
    status_ui.show_status(state, context, console=console)
    return 0


class MissionStatusVerb(VerbExtension):
    """Show what the recording assistant is doing right now."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument(
            "--json", action="store_true",
            help="machine-readable output for scripts")
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        return guarded_main(run, args)
