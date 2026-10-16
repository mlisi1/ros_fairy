"""ros2 fairy verify — re-check that a saved mission archive is intact.

Answers the question a data consumer actually has months later: "is this
archive still complete and unmodified, and does its metadata still parse?"
Checks, in plain language:

  - the mission record loads and validates against the schema;
  - ro-crate-metadata.json is well-formed JSON-LD (deep-loaded with the rocrate
    library when it is installed);
  - every file the crate references exists on disk;
  - each bag directory has its metadata and the storage files it lists;
  - each calibration file still matches the sha256 recorded at archive time;
  - the mission is registered in the local index.

Read-only: verify never modifies the archive or the index.
"""

import json
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ros_fairy.archive import bundle, index, locate
from ros_fairy.archive.assembler import CHECKSUMS_FILE
from ros_fairy.subcommands import (
    VerbExtension,
    _configure_logging,
    guarded_main,
    json_error,
    print_json,
)
from ros_fairy.utils import topic_health

OK, WARN, FAIL = "ok", "warn", "fail"
_GLYPH = {OK: "[green]✓[/green]", WARN: "[yellow]![/yellow]",
          FAIL: "[red]✗[/red]"}


def _is_external(ref: str) -> bool:
    return ref.startswith(("http://", "https://", "urn:", "#"))


def verify_archive(crate: Path) -> list[dict]:
    """Return a list of {status, title, detail} checks for one archive dir."""
    return verify_tree(bundle.Folder(crate))


def verify_bundle(path: Path, advance=lambda n: None) -> list[dict]:
    """The checks for an exported bundle, read without unpacking it: its
    checksum file first, then everything `verify_archive` checks except the
    index (a bundle is a copy, often on another machine)."""
    checks: list[dict] = []

    def add(status: str, title: str, detail: str = "") -> None:
        checks.append({"status": status, "title": title, "detail": detail})

    try:
        tree = bundle.read_bundle(path, advance)
    except bundle.BundleError as exc:
        add(FAIL, "The bundle can't be read", str(exc))
        return checks

    sidecar = path.with_name(path.name + ".sha256")
    try:
        listed = sidecar.read_text(errors="replace").split()
    except FileNotFoundError:
        add(WARN, "No checksum file next to the bundle",
            f"{sidecar.name}; the files inside are still checked")
    except OSError as exc:
        add(WARN, "The bundle's checksum file can't be read",
            f"{sidecar.name}: {exc.strerror or exc}")
    else:
        if listed and listed[0].lower() == tree.digest:
            add(OK, "The bundle matches its checksum file", sidecar.name)
        else:
            add(FAIL, "The bundle doesn't match its checksum file",
                "it was changed or not copied completely; the checks below "
                "say which files are affected")
    if tree.unreadable:
        add(FAIL, "Some files in the bundle can't be read",
            ", ".join(tree.unreadable))
    if tree.odd:
        add(WARN, "The bundle has entries that aren't plain files",
            ", ".join(tree.odd[:10]))
    return checks + verify_tree(tree)


def verify_tree(tree) -> list[dict]:
    """The checks for a mission folder or bundle (see archive/bundle.py)."""
    checks: list[dict] = []

    def add(status: str, title: str, detail: str = "") -> None:
        checks.append({"status": status, "title": title, "detail": detail})

    # 1. Mission record loads + validates (schema enforces required fields).
    try:
        if not tree.is_file("mission_record.json"):
            raise locate.LocateError(
                f"{tree.path} doesn't look like a mission archive "
                "(no mission_record.json found).")
        record, set_aside = locate.record_from_bytes(
            tree.read_bytes("mission_record.json"), tree.path)
        add(OK, "Mission record is valid",
            f"mission {record.identity.mission_id}")
    except locate.LocateError as exc:
        add(FAIL, "Mission record is unreadable or invalid", str(exc))
        return checks  # nothing else is meaningful without the record
    if set_aside:
        add(WARN, "Saved by a newer ros-fairy: some details can't be read "
                  "by this version", ", ".join(set_aside))
    from ros_fairy.archive import edit
    if any(tree.exists(name) for name in edit.leftovers()):
        add(FAIL, "An edit of the briefing was interrupted",
            "run `ros2 fairy mission_edit "
            f"{record.identity.mission_id}` to finish or undo it"
            if not tree.is_bundle else
            "it was exported halfway through; export it again")
    edits = record.provenance.edits
    if edits:
        fields = sorted({f for e in edits for f in e.changes})
        add(OK, f"Briefing edited after saving: {len(edits)} time"
                f"{'s' if len(edits) != 1 else ''}, last "
                f"{edits[-1].at.astimezone().strftime('%Y-%m-%d %H:%M')}"
                + (f" by {edits[-1].account}" if edits[-1].account else ""),
            ", ".join(f.replace("_", " ") for f in fields))
    built = record.provenance.assembled_by
    if built is not None:
        from ros_fairy.build_info import short
        add(OK, f"Saved by ros-fairy {short(built.model_dump())}",
            f"code {built.code_id}" if built.code_id else "")

    # 2. RO-Crate metadata well-formed (deep-load with rocrate if present).
    graph: list[dict] = []
    if not tree.is_file("ro-crate-metadata.json"):
        add(FAIL, "RO-Crate metadata is missing", "ro-crate-metadata.json")
    else:
        try:
            graph = json.loads(tree.read_bytes("ro-crate-metadata.json")
                               ).get("@graph", [])
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            add(FAIL, "RO-Crate metadata is not valid JSON", str(exc))
        else:
            try:
                from rocrate.rocrate import ROCrate
                with tree.rocrate_dir() as folder:
                    ROCrate(str(folder))
                add(OK, "RO-Crate metadata is valid JSON-LD")
            except ImportError:
                add(WARN, "RO-Crate metadata is valid JSON",
                    "install 'rocrate' for full JSON-LD validation")
            except Exception as exc:  # malformed JSON-LD
                add(FAIL, "RO-Crate metadata failed JSON-LD validation",
                    str(exc))

    # 3. Core files present.
    for rel in ("README.md", "harvest/harvest.json"):
        if tree.is_file(rel):
            add(OK, f"{rel} present")
        else:
            add(WARN, f"{rel} is missing")

    # 4. Bags. When per-file checksums were recorded (>= 1.0) re-hash and
    #    compare for byte-level integrity; otherwise fall back to a structural
    #    check (metadata + the storage files metadata lists) for older archives.
    for bag in record.bags:
        name = Path(bag.path).name
        if not tree.is_dir(bag.path):
            add(FAIL, f"Recording {bag.path} is missing")
            continue
        if bag.file_sha256:
            missing, modified = [], []
            for rel, expected in bag.file_sha256.items():
                f = f"{bag.path}/{rel}"
                if not tree.is_file(f):
                    missing.append(rel)
                elif tree.sha256(f) != expected:
                    modified.append(rel)
            if missing:
                add(FAIL, f"Recording {name} is missing data files",
                    ", ".join(missing))
            elif modified:
                add(FAIL, f"Recording {name} has been modified",
                    ", ".join(modified))
            else:
                add(OK, f"Recording {name} matches its checksums",
                    f"{len(bag.file_sha256)} file(s)")
            continue
        # Pre-1.0 archive: structural check only.
        meta_file = f"{bag.path}/metadata.yaml"
        meta = topic_health.parse_bag_metadata_text(
            tree.read_bytes(meta_file).decode(errors="replace")) \
            if tree.is_file(meta_file) else None
        if meta is None:
            add(FAIL, f"Recording {bag.path} has no readable metadata")
            continue
        missing = [f for f in meta["relative_file_paths"]
                   if not tree.is_file(f"{bag.path}/{f}")]
        if missing:
            add(FAIL, f"Recording {bag.path} is missing data files",
                ", ".join(missing))
        else:
            add(WARN, f"Recording {name} is present (no checksums recorded)",
                f"{len(meta['relative_file_paths'])} storage file(s)")

    # 5. Calibrations: re-hash and compare to the recorded sha256.
    for cal in record.calibrations:
        if not cal.archived_path:
            continue
        if not tree.is_file(cal.archived_path):
            add(FAIL, f"Calibration {cal.name} is missing", cal.archived_path)
        elif not cal.sha256:
            add(WARN, f"Calibration {cal.name} is present (no checksum "
                      "recorded)")
        elif tree.sha256(cal.archived_path) != cal.sha256:
            add(FAIL, f"Calibration {cal.name} has been modified",
                "sha256 does not match the archived value")
        else:
            add(OK, f"Calibration {cal.name} matches its checksum")

    # 5b. Everything else in the crate (record, README, RO-Crate metadata,
    #     harvest files): checksums.sha256, written when it was saved.
    _check_descriptive_files(tree, add)

    # 6. Every File entity the crate references exists.
    missing_refs = []
    for entity in graph:
        types = entity.get("@type", [])
        types = types if isinstance(types, list) else [types]
        ref = entity.get("@id", "")
        # @ids are percent-encoded URI paths ("my%20run/") since 2026-10-06.
        if "File" in types and not _is_external(ref) \
                and not tree.exists(unquote(ref)):
            missing_refs.append(ref)
    if missing_refs:
        add(FAIL, "Some files referenced by the crate are missing",
            ", ".join(missing_refs))
    elif graph:
        add(OK, "All files referenced by the crate are present")

    # 7. Index registration (a cache, so only a warning when absent). Not
    #    for a bundle: it is a copy, and the archive it came from may be
    #    on another robot.
    if tree.is_bundle:
        return checks
    crate = tree.path
    try:
        rows, _ = index.query(limit=None)
    except index.IndexUnavailableError as exc:
        add(WARN, "Couldn't check the mission index", str(exc))
        return checks
    match = next((r for r in rows
                  if r["mission_id"] == record.identity.mission_id), None)
    if match is None:
        add(WARN, "Mission is not in the local index",
            "it won't appear in `ros2 fairy list`; run `ros2 fairy reindex` "
            "to rebuild the list from the archives on disk")
    elif Path(match["archive_path"]).resolve() != crate.resolve():
        add(WARN, "Index points at a different path for this mission",
            match["archive_path"])
    else:
        add(OK, "Mission is registered in the index")

    return checks


def _check_descriptive_files(tree, add) -> None:
    if not tree.is_file(CHECKSUMS_FILE):
        add(WARN, "The mission's descriptive files can't be checked",
            "this archive was saved before ros-fairy recorded their "
            "checksums; only the recordings and calibrations are checked")
        return
    listed: dict[str, str] = {}
    text = tree.read_bytes(CHECKSUMS_FILE).decode(errors="replace")
    for line in text.splitlines():
        digest, _, rel = line.partition("  ")
        if rel:
            listed[rel] = digest
    missing = [rel for rel in listed if not tree.is_file(rel)]
    modified = [rel for rel, digest in listed.items()
                if tree.is_file(rel) and tree.sha256(rel) != digest]
    added = sorted(rel for rel in tree.files()
                   if not rel.startswith("bags/") and rel not in listed
                   and PurePosixPath(rel).name != CHECKSUMS_FILE)
    if missing:
        add(FAIL, "Some of the mission's files are missing",
            ", ".join(missing))
    if modified:
        add(FAIL, "Some of the mission's files have been modified",
            ", ".join(modified))
    if added:
        add(WARN, "Files were added to the archive after it was saved",
            ", ".join(added))
    if not missing and not modified:
        add(OK, "The mission's descriptive files match their checksums",
            f"{len(listed)} file(s)")


def _overall(checks: list[dict]) -> str:
    if any(c["status"] == FAIL for c in checks):
        return FAIL
    if any(c["status"] == WARN for c in checks):
        return WARN
    return OK


def _render(console: Console, crate: Path, checks: list[dict]) -> None:
    table = Table.grid(padding=(0, 1))
    table.add_column(width=1)
    table.add_column()
    for check in checks:
        detail = f" [dim]({check['detail']})[/dim]" if check["detail"] else ""
        table.add_row(_GLYPH[check["status"]], check["title"] + detail)
    overall = _overall(checks)
    title = {OK: "[green]PASS[/green]", WARN: "[yellow]PASS (with notes)[/yellow]",
             FAIL: "[red]FAIL[/red]"}[overall]
    border = {OK: "green", WARN: "yellow", FAIL: "red"}[overall]
    console.print(Panel(table, title=f"{title} — {crate.name}",
                        border_style=border))


def _verify_bundle_with_progress(path: Path, console: Console) -> list[dict]:
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        TextColumn,
        TimeRemainingColumn,
    )
    size = path.stat().st_size
    total = size * 2 if bundle.kind(path) == "zip" else size
    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(), DownloadColumn(), TimeRemainingColumn(),
        console=console, transient=True)
    with progress:
        task = progress.add_task("Reading the bundle", total=total or 1)
        return verify_bundle(path, lambda n: progress.advance(task, n))


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()

    as_json = getattr(args, "json", False)
    identifier = getattr(args, "mission", None) or "1"
    given = Path(identifier).expanduser()
    if given.is_file():
        crate = given
        checks = _verify_bundle_with_progress(
            given, Console(stderr=True) if as_json else console)
    else:
        try:
            crate = locate.resolve_archive(identifier)
        except locate.LocateError as exc:
            if as_json:
                return json_error(str(exc))
            console.print(f"[red]{exc}[/red]")
            return 1
        checks = verify_archive(crate)
    overall = _overall(checks)

    if as_json:
        print_json({"archive": str(crate), "bundle": given.is_file(),
                    "result": overall, "checks": checks})
    else:
        _render(console, crate, checks)

    return 1 if overall == FAIL else 0


class VerifyVerb(VerbExtension):
    """Check that a saved mission archive is complete and unmodified."""
    # Recordings and calibrations by their recorded checksums; every other
    # file by checksums.sha256 (archives saved since 2026-10-06).

    def add_arguments(self, parser, cli_name):
        parser.add_argument(
            "mission", nargs="?",
            help="mission to verify: number (1 = newest), archive path, "
                 "mission ID, or an exported bundle (.zip, .tar, .tar.gz, "
                 ".tar.zst; checked without unpacking it). Defaults to the "
                 "most recent mission.")
        parser.add_argument(
            "--json", action="store_true",
            help="machine-readable output for scripts")
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        return guarded_main(run, args)
