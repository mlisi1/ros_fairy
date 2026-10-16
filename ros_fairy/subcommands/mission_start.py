"""ros2 fairy mission_start — the briefing wizard.

Each answer can also be given as an option (--operator, --goal, --location,
--environment, --notes), so a mission can be started from a script or a
launch file: given answers skip their questions, and with every required one
given nothing is asked. --yes replaces an unfinished mission that has no
recordings; --json prints the new mission's ID.
"""

import sys
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm

from ros_fairy.manifest import builder, validator
from ros_fairy.subcommands import (
    VerbExtension,
    _configure_logging,
    guarded_main,
    json_error,
    print_json,
    refuse_without_access,
)
from ros_fairy.ui import briefing
from ros_fairy.utils import clock, fsio, paths, ros_env
from ros_fairy.watchdog import watchdog as wd


def _watchdog_alive() -> bool:
    from ros_fairy.ui.status import watchdog_alive
    return watchdog_alive(wd.read_state())


def _last_operator() -> str | None:
    try:
        from ros_fairy.archive import index
        rows, _ = index.query(limit=1)
        return rows[0]["operator"] if rows else None
    except Exception:
        return None


def _blocks_replacing(has_mission: bool = True) -> str | None:
    """Why what the spool holds can't be put aside without losing data, or
    None.

    A live recording belongs to whichever mission is open, and recordings
    ros-fairy saved into the spool exist nowhere else; both must be resolved
    with mission_close first rather than silently mixed into the new mission.
    """
    state = wd.read_state()
    if state and state.get("state") in ("RECORDING", "FINALISING"):
        from ros_fairy.ui.status import watchdog_alive
        if watchdog_alive(state):
            return "A recording is in progress. Stop it first, then run " \
                "this again."
    bags = paths.bags_dir()
    spool_bags = [p for p in bags.iterdir() if p.is_dir()] \
        if bags.is_dir() else []
    if spool_bags:
        n = len(spool_bags)
        whose = "The unfinished mission still has" if has_mission \
            else "There are"
        extra = "" if has_mission else " made while no mission was open"
        return (f"{whose} {n} recording{'s' if n != 1 else ''} saved by "
                f"ros-fairy{extra}. Save or discard "
                f"{'it' if has_mission else 'them'} first with "
                "ros2 fairy mission_close.")
    return None


def _drop_previous_harvest(console: Console) -> None:
    """Forget the replaced mission's harvest (graph snapshot + bag list).

    Without this the new mission inherits recordings and a ROS-graph capture
    from before it started. Recordings referenced in place (made outside
    ros-fairy) stay on disk and can be attached again with ``adopt``.
    """
    harvest = builder.load_spool()[0]
    foreign = [b["path"] for b in (harvest or {}).get("bags", [])]
    if foreign:
        console.print("These recordings are no longer attached to a mission "
                      "(they are still on disk; attach one with "
                      "[bold]ros2 fairy adopt <folder>[/bold]):")
        for path in foreign:
            console.print(f"  {path}")
    paths.harvest_json_path().unlink(missing_ok=True)
    paths.watchdog_log_path().unlink(missing_ok=True)


# Briefing field -> the option that answers it.
OPTIONS = {"operator_name": "--operator", "goal": "--goal",
           "location_name": "--location", "environment": "--environment",
           "notes": "--notes"}
_ARGS = {"operator_name": "operator", "goal": "goal",
         "location_name": "location", "environment": "environment",
         "notes": "notes"}


def _given_answers(args) -> dict[str, str | None]:
    """The answers given as options (stripped; empty optional ones as
    None). Empty required ones are left out, to be asked or refused."""
    given: dict[str, str | None] = {}
    for field, _prompt, required in briefing.QUESTIONS:
        value = getattr(args, _ARGS[field], None)
        if value is None:
            continue
        value = value.strip()
        if value or not required:
            given[field] = value or None
    return given


def _answers(console: Console, args, as_json: bool
             ) -> dict[str, str | None] | int:
    """The briefing: the full wizard when no answer was given as an option,
    otherwise only the missing required questions (refused, exit 2, when
    there's no terminal to ask them on)."""
    given = _given_answers(args)
    for field, value in given.items():
        error = validator.length_error(field, value)
        if error:
            message = f"{error} Shorten {OPTIONS[field]}."
            if as_json:
                return json_error(message, 2)
            console.print(f"[red]{message}[/red]")
            return 2
    if all(getattr(args, a, None) is None for a in _ARGS.values()):
        if as_json:
            message = ("--json starts a mission without questions: give "
                       "--operator, --goal and --location")
            return json_error(message, 2)
        return briefing.ask_briefing(console=console,
                                     default_operator=_last_operator())
    missing = [field for field, _p, required in briefing.QUESTIONS
               if required and field not in given]
    if missing:
        flags = ", ".join(OPTIONS[f] for f in missing)
        if as_json or not sys.stdin.isatty():
            message = f"Missing answers: give {flags} (no terminal to ask on)."
            if as_json:
                return json_error(message, 2)
            console.print(f"[red]{message}[/red]")
            return 2
        given.update(briefing.ask_missing(missing, console=console))
    return {field: given.get(field) for field, _p, _r in briefing.QUESTIONS}


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    as_json = getattr(args, "json", False)
    yes = getattr(args, "yes", False) or as_json
    # With --json only the result goes to stdout; everything said goes to
    # stderr.
    console = console or Console(stderr=as_json)
    refused = refuse_without_access(console, as_json)
    if refused is not None:
        return refused

    if not _watchdog_alive():
        console.print("[yellow]Background recording assistant isn't running "
                      "— your answers will still be saved.[/yellow]")

    if clock.is_synchronized() is False:
        console.print(f"[yellow]{clock.WARNING}[/yellow]")

    context_path = paths.mission_context_path()
    harvest, existing = builder.load_spool()
    # Whatever the spool holds is only put aside once the new briefing has
    # been answered: a Ctrl-C during the questions must change nothing.
    drop_previous = False
    def blocked(reason: str) -> int:
        if as_json:
            return json_error(reason)
        console.print(f"[yellow]{reason}[/yellow]")
        return 1

    if context_path.is_file() and existing:
        reason = _blocks_replacing()
        if reason:
            return blocked(reason)
        identity = existing.get("identity", {})
        when = identity.get("created_at", "")
        try:
            when = datetime.fromisoformat(when).astimezone().strftime(
                "%d %B, %H:%M")
        except ValueError:
            pass
        question = (f"There's already an unfinished mission from {when} by "
                    f"{identity.get('operator_name', 'someone')}. Start a new "
                    "one and replace it?")
        if yes:
            console.print(f"{question} [dim]yes (--yes)[/dim]")
        elif not Confirm.ask(question, default=False, console=console):
            return 0
        drop_previous = True
    elif harvest is not None:
        # No mission open, but the watchdog captured something: recordings
        # made with no mission (an engineer's `record_all test`). They must
        # not slip silently into this mission.
        reason = _blocks_replacing(has_mission=False)
        if reason:
            return blocked(reason)
        earlier = [b["path"] for b in harvest.get("bags", [])]
        if earlier:
            n = len(earlier)
            console.print(f"{n} recording{'s were' if n != 1 else ' was'} "
                          "made while no mission was open:")
            for path in earlier:
                console.print(f"  {path}")
            question = (f"Include {'them' if n != 1 else 'it'} in this new "
                        "mission?")
            if yes:
                # Not taken in unasked: they stay on disk, for `adopt`.
                console.print(f"{question} [dim]no (--yes keeps them out; "
                              "attach one with ros2 fairy adopt)[/dim]")
                drop_previous = True
            else:
                drop_previous = not Confirm.ask(question, default=False,
                                                console=console)
        else:
            drop_previous = True  # context of no recording: stale
    else:
        # Nothing captured yet (an unbriefed recording's first minute, or
        # the watchdog is down): a recording or spool bags still block.
        reason = _blocks_replacing(has_mission=False)
        if reason:
            return blocked(reason)

    answers = _answers(console, args, as_json)
    if isinstance(answers, int):
        return answers
    if drop_previous:
        _drop_previous_harvest(console)
    # The required answers are never None here: _answers asks for them or
    # refuses.
    context = builder.new_mission_context(
        operator_name=answers["operator_name"] or "",
        goal=answers["goal"] or "",
        location_name=answers["location_name"] or "",
        environment=answers["environment"],
        notes=answers["notes"])
    paths.spool_dir().mkdir(parents=True, exist_ok=True)
    # Hand the live recording shell's ROS environment to the watchdog so its
    # harvest sees the same DDS partition / overlay as this session, even if the
    # frozen watchdog.env snapshot has drifted (issue #29).
    ros_env.write_file(paths.session_env_path(), ros_env.capture(),
                       mode=0o664)
    fsio.atomic_write_json(context_path, context)
    if as_json:
        print_json({"mission_id": context["identity"]["mission_id"],
                    "operator": context["identity"]["operator_name"],
                    "goal": context["intent"]["goal"],
                    "location": context["intent"]["location_name"]})
        return 0
    console.print(Panel("Mission briefing saved. Start recording with: "
                        "[bold]ros2 fairy mission_record[/bold]",
                        border_style="green"))
    return 0


class MissionStartVerb(VerbExtension):
    """Answer five quick questions to describe the mission you're starting."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument("--operator", metavar="NAME",
                            help="your name (skips that question)")
        parser.add_argument("--goal", metavar="TEXT",
                            help="what the mission is trying to do")
        parser.add_argument("--location", metavar="PLACE",
                            help="where you are, e.g. 'Marsh Creek, north "
                                 "bank'")
        parser.add_argument("--environment", metavar="TEXT",
                            help="e.g. outdoor, indoor, marine (optional)")
        parser.add_argument("--notes", metavar="TEXT",
                            help="anything else worth noting (optional)")
        parser.add_argument(
            "--yes", "-y", action="store_true",
            help="replace an unfinished mission that has no recordings "
                 "without asking")
        parser.add_argument(
            "--json", action="store_true",
            help="no questions; print the new mission's ID as JSON (needs "
                 "--operator, --goal, --location; implies --yes)")
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        return guarded_main(run, args)
