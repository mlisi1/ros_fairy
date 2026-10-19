"""ros2cli command extension: the `ros2 fairy` verb family."""

try:
    from ros2cli.command import CommandExtension, add_subparsers_on_demand
except ImportError:  # pragma: no cover - exercised only outside ROS
    add_subparsers_on_demand = None

    class CommandExtension:  # type: ignore[no-redef]
        def add_arguments(self, parser, cli_name):
            pass


class FairyCommand(CommandExtension):
    """Make field mission data FAIR-compliant with zero friction."""

    def add_arguments(self, parser, cli_name):
        self._subparser = parser
        if add_subparsers_on_demand is None:
            return
        add_subparsers_on_demand(
            parser, cli_name, "_verb", "fairy.verb", required=False)

    def main(self, *, parser, args):
        if not hasattr(args, "_verb"):
            self._subparser.print_help()
            return 0
        extension = args._verb
        return extension.main(args=args)
