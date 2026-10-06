"""mission_close summary panel and save/discard confirmation."""

from rich.console import Console, Group
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

from ros_fairy.manifest import quality as quality_mod
from ros_fairy.manifest.quality import Quality
from ros_fairy.manifest.schema import MissionRecord
from ros_fairy.utils.topic_health import INFO_KINDS, humanize_duration

_QUALITY_LABEL = {
    quality_mod.DEGRADED: ("INCOMPLETE", "yellow"),
    quality_mod.POOR: ("POOR — important data is missing", "red"),
}

# Beyond this many distinct topics with a "gap" warning, itemising each one
# drowns the review (a flaky network mid-mission can drop a dozen topics at
# once). Collapse to a single count instead — the full per-topic detail
# still ends up in the mission record and RO-Crate metadata either way, this
# is only about what's worth reading right now.
GAP_SUMMARY_THRESHOLD = 3


def human_size(size_bytes: int) -> str:
    if size_bytes >= 1e9:
        return f"{size_bytes / 1e9:.1f} GB"
    if size_bytes >= 1e6:
        return f"{size_bytes / 1e6:.0f} MB"
    return f"{max(size_bytes, 0) / 1e3:.0f} kB"


def show_summary(record: MissionRecord, harvest_warnings: list[str],
                 console: Console | None = None,
                 quality: Quality | None = None,
                 duplicates: list[str] | None = None,
                 exact_duplicate: str | None = None) -> None:
    console = console or Console()
    facts = Table.grid(padding=(0, 2))
    facts.add_column(style="bold")
    facts.add_column()
    facts.add_row("Mission", record.intent.goal)
    facts.add_row("Where", record.intent.location_name)
    facts.add_row("When",
                  record.identity.created_at.astimezone().strftime(
                      "%A %d %B %Y, %H:%M"))
    facts.add_row("Operator", record.identity.operator_name)
    if record.robot:
        facts.add_row("Robot", f"{record.robot.name} "
                               f"({record.robot.platform})")
    total_s = sum(b.duration_s or 0 for b in record.bags)
    total_bytes = sum(b.size_bytes for b in record.bags)
    n = len(record.bags)
    # When no bag has a measurable duration, don't claim "0 seconds".
    length = (humanize_duration(total_s)
              if any(b.duration_s for b in record.bags) else "length unknown")
    facts.add_row("Recording",
                  f"{n} recording{'s' if n != 1 else ''}, "
                  f"{length}, {human_size(total_bytes)}")

    # INFO_KINDS (e.g. a camera recorded on its compressed stream) describe
    # something worth knowing, not something wrong — a sensor with only an
    # info-kind warning still reads as fine in the Sensors list.
    warned_sensors = {w.sensor_id for b in record.bags
                      for w in b.health_warnings
                      if w.sensor_id and w.kind not in INFO_KINDS}
    sensor_lines = []
    for sensor in record.sensors:
        ok = sensor.detected_at_start and sensor.sensor_id not in \
            warned_sensors
        glyph, style = ("✓", "green") if ok else ("⚠", "yellow")
        sensor_lines.append(Text(
            f" {glyph} {sensor.make_model} ({sensor.sensor_id})",
            style=style))

    # A multi-bag mission (a foreign recording adopted mid-mission, a
    # retry, ...) commonly repeats the *identical* warning per bag — e.g.
    # "Camera produced no data at all" once for each bag a disconnected
    # camera sat silent in. That's the same fact stated N times, not N
    # facts; collapse to one line each (order preserved) rather than
    # drowning the review in duplicates.
    health = [w for b in record.bags for w in b.health_warnings]

    # Gap warnings are collapsed per-topic already (topic_health.py); once
    # enough *different* topics are each affected, collapse across topics
    # too rather than printing one line per channel.
    gap_topics = list(dict.fromkeys(w.topic for w in health if w.kind == "gap"))
    if len(gap_topics) > GAP_SUMMARY_THRESHOLD:
        gap_lines = [f"{len(gap_topics)} recorded channels dropped data "
                     "during the recording — the full per-topic detail is "
                     "saved with the mission record."]
    else:
        gap_lines = list(dict.fromkeys(
            w.plain_text for w in health if w.kind == "gap"))
    other_lines = [w.plain_text for w in health
                   if w.kind not in INFO_KINDS and w.kind != "gap"]

    warnings = list(dict.fromkeys(harvest_warnings + gap_lines + other_lines))
    notes = list(dict.fromkeys(
        w.plain_text for w in health if w.kind in INFO_KINDS))
    body: list = []
    border = "cyan"
    if quality is not None and quality.level in _QUALITY_LABEL:
        label, color = _QUALITY_LABEL[quality.level]
        border = color
        body += [Text.from_markup(f"Data quality: [{color}]{label}[/{color}]",
                                  style="bold")]
        body += [Text(f" • {reason}", style=color)
                 for reason in quality.reasons]
        body += [Text("")]
    if exact_duplicate:
        # A content match, not a metadata guess — state it with more
        # confidence than the fuzzy location/time heuristic below, and let
        # it win the border color (it's the more certain signal).
        border = "red"
        body += [Text("Duplicate recording", style="bold red")]
        body += [Text(f" ✗ {exact_duplicate}", style="red")]
        body += [Text("")]
    if duplicates:
        if border == "cyan":
            border = "yellow"
        body += [Text("Possible duplicate", style="bold yellow")]
        body += [Text(f" ⚠ {d}", style="yellow") for d in duplicates]
        body += [Text("")]
    body += [facts]
    if sensor_lines:
        body += [Text(""), Text("Sensors", style="bold"), *sensor_lines]
    if warnings:
        body += [Text(""), Text("Things worth knowing", style="bold")]
        body += [Text(f" ⚠ {w}", style="yellow") for w in warnings]
    if notes:
        body += [Text("")]
        body += [Text(f" ℹ {n}", style="dim") for n in notes]
    console.print(Panel(Group(*body), title="Mission summary",
                        border_style=border))


def confirm_save(console: Console | None = None, *, risky: bool = False,
                 assume_yes: bool = False) -> str:
    """Returns 'save', 'discard', or 'keep' (leave spool untouched).

    When ``risky`` (the mission graded "poor"), the save prompt defaults to No
    and is worded as a caution, so an operator can't archive a near-empty
    recording by reflexively pressing Enter.
    """
    console = console or Console()
    if assume_yes:
        console.print("Save this mission? [dim]yes (--yes)[/dim]")
        return "save"
    if risky:
        saved = Confirm.ask(
            "This recording is missing important data (see above). "
            "Save it anyway?", default=False, console=console)
    else:
        saved = Confirm.ask("Save this mission?", default=True, console=console)
    if saved:
        return "save"
    if Confirm.ask("Throw away this recording and all its data?",
                   default=False, console=console):
        return "discard"
    return "keep"
