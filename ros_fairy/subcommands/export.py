"""ros2 fairy export — package a saved mission as one portable, checksummed file.

A mission archive is an RO-Crate *directory*; sharing it means an operator
hand-zips it (and hopes the transfer didn't corrupt anything). This makes that a
first-class, safe step: it bundles the crate into a single ``.zip`` (or ``.tar``)
with a top-level folder, uncompressed unless ``--compress`` is given, writes a
``sha256sum``-compatible sidecar so the recipient can prove the transfer, and
refuses to clobber an existing file. The crate's own per-file checksums still
let the recipient run ``ros2 fairy verify`` after unpacking.

Read-only with respect to the archive and index.
"""

import logging
import os
import shutil
import tarfile
import zipfile
import zlib
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
)
from rich.prompt import Confirm  # noqa: F401 (tests patch Confirm.ask)

from ros_fairy.archive import index, locate
from ros_fairy.subcommands import (
    VerbExtension,
    _configure_logging,
    confirm,
    guarded_main,
    json_error,
    print_json,
)
from ros_fairy.ui.review import human_size
from ros_fairy.utils import fsio

log = logging.getLogger("ros_fairy.subcommands.export")

FORMATS = ("zip", "tar", "tar.gz", "tar.zst")
_EXT = {fmt: "." + fmt for fmt in FORMATS}

# Files already compressed are stored in a compressed zip as they are:
# deflating them again costs time and saves nothing. Anything else is
# probed (see _worth_deflating).
_COMPRESSED_SUFFIXES = {
    ".zst", ".zstd", ".gz", ".xz", ".bz2", ".lz4", ".zip", ".7z",
    ".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mkv", ".webm"}
_PROBE_BYTES = 4 << 20
_CHUNK = 1 << 20


def choose_format(fmt: str, compress: bool) -> tuple[str, str]:
    """(format, compression) from --format and --compress.

    ``--compress`` deflates a zip, so it still opens everywhere, and turns a
    tar into .tar.zst. Naming tar.gz or tar.zst asks for that directly.
    """
    if fmt == "zip":
        return "zip", ("deflate" if compress else "none")
    if fmt == "tar" and compress:
        fmt = "tar.zst"
    return fmt, {"tar": "none", "tar.gz": "gzip", "tar.zst": "zstd"}[fmt]


def _resolve_output(crate: Path, output: str | None, fmt: str) -> Path:
    """Where to write the bundle, from --output (file or dir) and format.

    A path that doesn't exist yet is a folder unless it names a bundle file
    (ends in .zip, .tar, .tar.gz or .tar.zst): `-o /media/usb/missions`
    makes that folder rather than writing a file called "missions".
    """
    default_name = crate.name + _EXT[fmt]
    if not output:
        return Path.cwd() / default_name
    out = Path(output).expanduser()
    names_a_bundle = output.lower().endswith(tuple(_EXT.values()))
    if out.is_dir() or output.endswith(os.sep) or \
            (not out.exists() and not names_a_bundle):
        return out / default_name
    return out


def _bundle_files(crate: Path) -> list[tuple[Path, str]]:
    """(absolute path, arcname) for every file, under a top-level crate folder."""
    return [(f, f"{crate.name}/{f.relative_to(crate).as_posix()}")
            for f in sorted(crate.rglob("*")) if f.is_file()]


def _worth_deflating(path: Path) -> bool:
    """Whether deflate would shrink this file, judged on a sample.

    Bags recorded with rosbag2's defaults are plain MCAP or sqlite3 and
    shrink a lot; an MCAP with compressed chunks, or a file compressed
    whole, doesn't. The sample comes from the middle, past an MCAP's
    uncompressed header and schemas.
    """
    if path.suffix.lower() in _COMPRESSED_SUFFIXES:
        return False
    size = path.stat().st_size
    with open(path, "rb") as f:
        f.seek(max(0, size // 2 - _PROBE_BYTES // 2))
        sample = f.read(_PROBE_BYTES)
    if len(sample) < 4096:
        return True
    return len(zlib.compress(sample, 1)) < 0.9 * len(sample)


class _Counted:
    """A file read through to the progress bar, so a multi-GB bag that is
    being compressed doesn't leave the bar still for minutes."""

    def __init__(self, f, advance):
        self._f = f
        self._advance = advance

    def read(self, size=-1):
        data = self._f.read(size)
        self._advance(len(data))
        return data


def _pack_zip(part: Path, files, compress: bool, advance) -> None:
    with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED,
                         allowZip64=True) as zf:
        for src, arc in files:
            info = zipfile.ZipInfo.from_file(src, arc)
            if compress and _worth_deflating(src):
                info.compress_type = zipfile.ZIP_DEFLATED
            with open(src, "rb") as f, zf.open(info, "w") as out:
                shutil.copyfileobj(_Counted(f, advance), out, _CHUNK)


def _pack_tar(tf: tarfile.TarFile, files, advance) -> None:
    for src, arc in files:
        info = tf.gettarinfo(str(src), arcname=arc)
        with open(src, "rb") as f:
            tf.addfile(info, _Counted(f, advance))


def _write_bundle(files: list[tuple[Path, str]], dest: Path, fmt: str,
                  compression: str, console: Console) -> None:
    """Pack files into dest atomically (.part then rename)."""
    total = sum(f.stat().st_size for f, _ in files) or 1
    part = dest.with_name(dest.name + ".part")
    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(), DownloadColumn(), TimeRemainingColumn(),
        console=console, transient=True)
    try:
        with progress:
            task = progress.add_task(
                "Compressing" if compression != "none" else "Packaging",
                total=total)

            def advance(n: int) -> None:
                progress.advance(task, n)

            if fmt == "zip":
                _pack_zip(part, files, compression == "deflate", advance)
            elif fmt == "tar.zst":
                import zstandard
                # Level 3 (zstd's default) on every core: measured on real
                # bags at 1-2.5 GB/s, faster than the disk being written.
                cctx = zstandard.ZstdCompressor(level=3, threads=-1)
                with open(part, "wb") as raw, \
                        cctx.stream_writer(raw, closefd=False) as zw, \
                        tarfile.open(fileobj=zw, mode="w|") as tf:
                    _pack_tar(tf, files, advance)
            elif fmt == "tar.gz":
                # Level 6 like gzip's own default; tarfile's 9 is much
                # slower for a few percent.
                with tarfile.open(part, "w:gz", compresslevel=6) as tf:
                    _pack_tar(tf, files, advance)
            else:
                with tarfile.open(part, "w") as tf:
                    _pack_tar(tf, files, advance)
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def _local_date(iso: str):
    try:
        return datetime.fromisoformat(iso).astimezone().date()
    except ValueError:
        return None


def _export_one(crate: Path, output_arg: str | None, fmt: str,
                compression: str, force: bool, console: Console,
                quiet: bool = False) -> dict:
    """Export one archive. Never raises — failures come back as {"ok": False,
    "error": ...} so both the single-mission and batch paths can report them
    without a bundle failure aborting the whole batch."""
    try:
        record = locate.load_record(crate)
    except locate.LocateError as exc:
        return {"mission_id": None, "source": str(crate), "ok": False,
                "error": str(exc)}

    dest = _resolve_output(crate, output_arg, fmt)
    if dest.exists() and not force:
        return {"mission_id": record.identity.mission_id, "source": str(crate),
                "ok": False,
                "error": f"{dest} already exists (use --force to overwrite)"}

    # Integrity gate: warn loudly if the source archive doesn't verify, but let
    # the operator export it anyway (they asked, and a flawed copy can still be
    # worth shipping for diagnosis). verify must never block the export, so a
    # broken index or any other hiccup degrades to "unknown".
    try:
        from ros_fairy.subcommands.verify import _overall, verify_archive
        verify_result = _overall(verify_archive(crate))
    except Exception:
        verify_result = "unknown"
    if verify_result == "fail" and not quiet:
        console.print(f"[yellow]Warning: {record.identity.mission_id} failed "
                      "integrity checks (run `ros2 fairy verify` for "
                      "details). Exporting anyway.[/yellow]")

    files = _bundle_files(crate)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _write_bundle(files, dest, fmt, compression, console)
    except OSError as exc:
        return {"mission_id": record.identity.mission_id, "source": str(crate),
                "ok": False,
                "error": f"couldn't write the bundle: {exc.strerror or exc}"}

    checksum_path = dest.with_name(dest.name + ".sha256")
    try:
        digest = fsio.sha256_file(dest)
        # sha256sum-compatible: recipient runs `sha256sum -c <file>.sha256`.
        fsio.atomic_write_text(checksum_path, f"{digest}  {dest.name}\n")
    except OSError as exc:
        # A bundle the recipient can't check isn't a finished export.
        dest.unlink(missing_ok=True)
        return {"mission_id": record.identity.mission_id, "source": str(crate),
                "ok": False,
                "error": f"couldn't write the checksum file "
                         f"({exc.strerror or exc}); the bundle was removed"}

    # Only reached once the bundle file actually exists on disk — this is
    # what `export --all` treats as "already exported".
    try:
        index.mark_exported(record.identity.mission_id, dest, fmt, digest)
    except Exception:
        log.warning("couldn't record export of %s in the index",
                   record.identity.mission_id, exc_info=True)

    return {
        "mission_id": record.identity.mission_id,
        "source": str(crate),
        "ok": True,
        "bundle": str(dest),
        "format": fmt,
        "compression": compression,
        "size_bytes": dest.stat().st_size,
        "sha256": digest,
        "checksum_file": str(checksum_path),
        "verify_result": verify_result,
    }


def _print_result(result: dict, console: Console) -> None:
    console.print(
        f"[green]Exported[/green] {result['mission_id']} → "
        f"{result['bundle']} [dim]({human_size(result['size_bytes'])})[/dim]")
    console.print(f"[dim]sha256: {result['sha256']}[/dim]")
    console.print(f"[dim]checksum saved to "
                  f"{Path(result['checksum_file']).name} — the recipient can "
                  "run `ros2 fairy verify` after unpacking.[/dim]")


def _run_batch(args, console: Console, candidates: list[dict], what: str,
               fmt: str, compression: str) -> int:
    """Shared body of --all/--today: confirm, check disk space, export each."""
    yes = getattr(args, "yes", False) or getattr(args, "json", False)
    exclude = set(getattr(args, "exclude", None) or [])
    candidates = [r for r in candidates if r["mission_id"] not in exclude]
    candidates.sort(key=lambda r: r["created_at"])

    as_json = getattr(args, "json", False)
    if not candidates:
        if as_json:
            print_json({"exported": 0, "failed": 0, "results": []})
        else:
            console.print("Nothing to export.")
        return 0

    output_arg = getattr(args, "output", None)
    out_dir = Path(output_arg).expanduser() if output_arg else Path.cwd()
    if out_dir.exists() and not out_dir.is_dir():
        message = (f"{out_dir} is not a directory — --output must be a "
                   f"directory when exporting {what}.")
        if as_json:
            return json_error(message)
        console.print(f"[red]{message}[/red]")
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    # Batch output names one folder; each bundle goes in it.
    output_arg = str(out_dir) + os.sep

    total_bytes = sum(r["size_bytes"] for r in candidates)
    if not as_json:
        console.print(f"{len(candidates)} mission(s) to export {what}, "
                      f"~{human_size(total_bytes)} total.")

    free = shutil.disk_usage(out_dir).free
    if free < total_bytes:
        proceed = confirm(
            f"Only {human_size(free)} free at {out_dir}, but this needs "
            f"about {human_size(total_bytes)}. Continue anyway?",
            default=False, console=console, assume_yes=yes)
        if not proceed:
            return 0

    proceed = confirm(
        f"This will export {len(candidates)} mission(s) and could take a "
        "while. Continue?", default=True, console=console, assume_yes=yes)
    if not proceed:
        return 0

    force = getattr(args, "force", False)
    results = []
    for row in candidates:
        result = _export_one(Path(row["archive_path"]), output_arg, fmt,
                             compression, force, console, quiet=as_json)
        results.append(result)
        if not as_json:
            if result["ok"]:
                console.print(f"[green]Exported[/green] {result['mission_id']}"
                              f" → {result['bundle']}")
            else:
                console.print(f"[red]Failed[/red] {row['mission_id']}: "
                              f"{result['error']}")

    ok_count = sum(1 for r in results if r["ok"])
    if as_json:
        print_json({"exported": ok_count, "failed": len(results) - ok_count,
                    "results": results})
    else:
        console.print(f"Done: {ok_count} exported, "
                      f"{len(results) - ok_count} failed.")
    return 0 if ok_count == len(results) else 1


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    as_json = getattr(args, "json", False)
    # In --json mode stdout carries the JSON only; the progress bar and any
    # notes go to stderr.
    console = console or Console(stderr=as_json)

    def fail(message: str) -> int:
        if as_json:
            return json_error(message)
        console.print(f"[red]{message}[/red]")
        return 1

    all_mode = getattr(args, "all", False)
    today_mode = getattr(args, "today", False)
    mission_arg = getattr(args, "mission", None)

    if all_mode and today_mode:
        return fail("--all and --today can't be used together.")
    if (all_mode or today_mode) and mission_arg:
        return fail("Give a mission, or --all/--today — not both.")

    fmt, compression = choose_format(
        getattr(args, "format", "zip"), getattr(args, "compress", False))

    if all_mode or today_mode:
        try:
            rows, _total = index.query(limit=10_000)
        except index.IndexUnavailableError as exc:
            return fail(str(exc))
        if all_mode:
            already = index.exported_mission_ids()
            candidates = [r for r in rows if r["mission_id"] not in already]
            what = "not already exported"
        else:
            today = datetime.now().astimezone().date()
            candidates = [r for r in rows
                         if _local_date(r["created_at"]) == today]
            what = "recorded today"
        return _run_batch(args, console, candidates, what, fmt,
                          compression)

    try:
        crate = locate.resolve_archive(mission_arg or "1")
    except locate.LocateError as exc:
        return fail(str(exc))

    result = _export_one(crate, getattr(args, "output", None), fmt,
                         compression, getattr(args, "force", False), console,
                         quiet=as_json)
    if not result["ok"]:
        if as_json:
            return json_error(result["error"], result=result)
        console.print(f"[red]{result['error']}[/red]")
        return 1

    if as_json:
        print_json(result)
    else:
        _print_result(result, console)
    return 0


class ExportVerb(VerbExtension):
    """Package a saved mission into one portable file for sharing."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument(
            "mission", nargs="?",
            help="mission to export: number (1 = newest), archive path, or "
                 "mission ID. Defaults to the most recent mission. Not used "
                 "with --all/--today.")
        parser.add_argument(
            "--all", action="store_true",
            help="export every saved mission that hasn't been successfully "
                 "exported yet")
        parser.add_argument(
            "--today", action="store_true",
            help="export every mission recorded today")
        parser.add_argument(
            "--exclude", "-e", action="append", metavar="MISSION_ID",
            help="mission ID to skip; repeatable. Only applies with "
                 "--all/--today")
        parser.add_argument(
            "--output", "-o",
            help="output file (single mission), or a directory to write "
                 "<mission>.<ext> into — required to be a directory when "
                 "exporting more than one mission (default: current "
                 "directory)")
        parser.add_argument(
            "--format", choices=FORMATS, default="zip",
            help="bundle format (default: zip). tar.gz and tar.zst are "
                 "compressed tar files; tar.gz for recipients without zstd")
        parser.add_argument(
            "--compress", "-z", action="store_true",
            help="compress the bundle: a zip is deflated (files that are "
                 "already compressed are stored as they are), a tar becomes "
                 ".tar.zst. "
                 "Bags recorded with rosbag2's defaults often shrink by half or "
                 "more")
        parser.add_argument(
            "--force", action="store_true",
            help="overwrite output files that already exist")
        parser.add_argument(
            "--yes", "-y", action="store_true",
            help="export --all/--today without asking (implied by --json)")
        parser.add_argument(
            "--json", action="store_true",
            help="machine-readable output for scripts")
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        return guarded_main(run, args)
