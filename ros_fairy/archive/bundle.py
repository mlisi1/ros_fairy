"""A mission archive as `verify` reads it: a folder, or an exported bundle.

An exported bundle (``.zip``, ``.tar``, ``.tar.gz``, ``.tar.zst``) is read
in one pass without unpacking it: every member is hashed as it streams by,
and only the small descriptive files (the record, the RO-Crate metadata,
``checksums.sha256``, recordings' ``metadata.yaml``) are kept in memory.
The bundle's own sha256 comes from the same pass for a tar; a zip, read by
random access, is hashed separately.

Both kinds answer the same questions (which files there are, their
contents and sha256), so `verify` runs one set of checks on either.
"""

import contextlib
import gzip
import hashlib
import io
import tarfile
import tempfile
import zipfile
import zlib
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any

from ros_fairy.utils import fsio

_CHUNK = 1 << 20
# Kept in memory while reading a bundle; everything else is only hashed.
_KEEP_NAMES = {"mission_record.json", "ro-crate-metadata.json",
               "checksums.sha256", "metadata.yaml"}
_KEEP_MAX = 64 << 20

_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")
_GZIP_MAGIC = b"\x1f\x8b"
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


class BundleError(Exception):
    """The file isn't a bundle, or can't be read to the end."""


class Folder:
    """An archive folder on disk."""

    is_bundle = False

    def __init__(self, path: Path):
        self.path = path
        self.name = path.name

    def is_file(self, rel: str) -> bool:
        return (self.path / rel).is_file()

    def is_dir(self, rel: str) -> bool:
        return (self.path / rel).is_dir()

    def exists(self, rel: str) -> bool:
        return (self.path / rel).exists()

    def read_bytes(self, rel: str) -> bytes:
        return (self.path / rel).read_bytes()

    def sha256(self, rel: str) -> str | None:
        return fsio.sha256_file(self.path / rel)

    def files(self) -> Iterator[str]:
        for f in self.path.rglob("*"):
            if f.is_file():
                yield f.relative_to(self.path).as_posix()

    @contextlib.contextmanager
    def rocrate_dir(self) -> Iterator[Path]:
        yield self.path


class Bundle:
    """An exported bundle, read once by :func:`read_bundle`."""

    is_bundle = True

    def __init__(self, path: Path, top: str, hashes: dict[str, str | None],
                 kept: dict[str, bytes], digest: str, odd: list[str]):
        self.path = path
        self.name = path.name
        self.top = top                # the mission folder's name inside
        self._hashes = hashes         # rel -> sha256, None if unreadable
        self._kept = kept
        self.digest = digest          # sha256 of the bundle file itself
        self.odd = odd                # entries that aren't plain files
        self._dirs = {str(p) for rel in hashes
                      for p in PurePosixPath(rel).parents if str(p) != "."}

    @property
    def unreadable(self) -> list[str]:
        return sorted(rel for rel, h in self._hashes.items() if h is None)

    def is_file(self, rel: str) -> bool:
        return _norm(rel) in self._hashes

    def is_dir(self, rel: str) -> bool:
        return _norm(rel) in self._dirs

    def exists(self, rel: str) -> bool:
        rel = _norm(rel)
        return rel in self._hashes or rel in self._dirs or rel == ""

    def read_bytes(self, rel: str) -> bytes:
        rel = _norm(rel)
        if rel not in self._kept:
            raise FileNotFoundError(rel)
        return self._kept[rel]

    def sha256(self, rel: str) -> str | None:
        return self._hashes.get(_norm(rel))

    def files(self) -> Iterator[str]:
        yield from self._hashes

    @contextlib.contextmanager
    def rocrate_dir(self) -> Iterator[Path]:
        """A folder holding just ro-crate-metadata.json: the rocrate library
        reads the metadata without needing the files it describes."""
        with tempfile.TemporaryDirectory(prefix="fairy-verify-") as tmp:
            meta = "ro-crate-metadata.json"
            Path(tmp, meta).write_bytes(self.read_bytes(meta))
            yield Path(tmp)


def _norm(rel: str) -> str:
    return str(PurePosixPath(rel.strip("/"))) if rel.strip("/") else ""


def kind(path: Path) -> str | None:
    """'zip', 'tar', 'tar.gz' or 'tar.zst' by the file's content (not its
    name), or None when it is none of them."""
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return None
    if head.startswith(_ZIP_MAGIC):
        return "zip"
    if head.startswith(_GZIP_MAGIC):
        return "tar.gz"
    if head.startswith(_ZSTD_MAGIC):
        return "tar.zst"
    if head[257:262] == b"ustar":
        return "tar"
    return None


class _Hashing(io.RawIOBase):
    """Reads through to the bundle's own sha256 and the progress bar."""

    def __init__(self, f, advance: Callable[[int], None]):
        self._f = f
        self._advance = advance
        self.hash = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        data = self._f.read(len(buffer))
        n = len(data)
        buffer[:n] = data
        self.hash.update(data)
        self._advance(n)
        return n

    def drain(self) -> None:
        while self.read(_CHUNK):
            pass


def _member_path(name: str) -> tuple[str, str]:
    """(top folder, path inside it) of a member, refusing names that point
    outside the bundle."""
    p = PurePosixPath(name)  # "./a/b" is a/b
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise BundleError(f"it holds a path outside the bundle: {name}")
    return p.parts[0], "/".join(p.parts[1:])


class _Collect:
    def __init__(self) -> None:
        self.tops: set[str] = set()
        self.hashes: dict[str, str | None] = {}
        self.kept: dict[str, bytes] = {}
        self.odd: list[str] = []

    def add(self, name: str, stream, size: int,
            advance: Callable[[int], None] | None = None) -> None:
        top, rel = _member_path(name)
        self.tops.add(top)
        if not rel:
            raise BundleError(f"{name} isn't inside the mission's folder")
        keep = PurePosixPath(rel).name in _KEEP_NAMES and size <= _KEEP_MAX
        digest = hashlib.sha256()
        buf = bytearray() if keep else None
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
            if buf is not None:
                buf += chunk
            if advance:
                advance(len(chunk))
        self.hashes[rel] = digest.hexdigest()
        if buf is not None:
            self.kept[rel] = bytes(buf)

    def bundle(self, path: Path, digest: str) -> Bundle:
        if len(self.tops) != 1:
            raise BundleError(
                "an exported mission has one folder at the top, this has "
                f"{len(self.tops) or 'none'}"
                + (f" ({', '.join(sorted(self.tops)[:5])})"
                   if self.tops else ""))
        return Bundle(path, next(iter(self.tops)), self.hashes, self.kept,
                      digest, self.odd)


def read_bundle(path: Path,
                advance: Callable[[int], None] = lambda n: None) -> Bundle:
    """Read an exported bundle once. ``advance`` is told how many bytes of
    the bundle file were read, for a progress bar (twice the file's size for
    a zip)."""
    fmt = kind(path)
    if fmt is None:
        raise BundleError("it isn't a .zip or .tar bundle")
    try:
        if fmt == "zip":
            return _read_zip(path, advance)
        return _read_tar(path, fmt, advance)
    except (OSError, EOFError, tarfile.TarError, zipfile.BadZipFile,
            zlib.error) as exc:
        # tarfile says "empty file" when the decompressor gave it nothing,
        # e.g. a .tar.zst cut short inside its first block.
        why = "nothing in it could be read" if str(exc) == "empty file" \
            else str(exc)
        raise BundleError(f"it is damaged or cut short ({why})") from exc
    except Exception as exc:
        if type(exc).__module__.startswith("zstandard"):
            raise BundleError(
                f"it is damaged or cut short ({exc})") from exc
        raise


def _read_tar(path: Path, fmt: str, advance) -> Bundle:
    collect = _Collect()
    with open(path, "rb") as f:
        raw = _Hashing(f, advance)
        buffered = io.BufferedReader(raw, _CHUNK)
        stream: Any  # a gzip, zstd or plain byte stream
        if fmt == "tar.gz":
            stream = gzip.GzipFile(fileobj=buffered)
        elif fmt == "tar.zst":
            import zstandard
            stream = zstandard.ZstdDecompressor().stream_reader(buffered)
        else:
            stream = buffered
        with tarfile.open(fileobj=stream, mode="r|") as tf:
            for info in tf:
                if info.isdir():
                    _member_path(info.name)
                    continue
                if not info.isfile():
                    collect.odd.append(info.name)
                    continue
                member = tf.extractfile(info)
                assert member is not None
                collect.add(info.name, member, info.size)
        # Whatever follows the tar's end (padding) still counts for the
        # bundle's own checksum.
        buffered.read()
        raw.drain()
    return collect.bundle(path, raw.hash.hexdigest())


def _read_zip(path: Path, advance) -> Bundle:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
            advance(len(chunk))
    collect = _Collect()
    total = path.stat().st_size
    unpacked = sum(i.file_size for i in _infos(path)) or 1

    def scaled(n: int) -> None:  # the second half of the bar, by members
        advance(n * total // unpacked)

    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                _member_path(info.filename)
                continue
            try:
                with zf.open(info) as member:
                    collect.add(info.filename, member, info.file_size,
                                scaled)
            except (zipfile.BadZipFile, NotImplementedError, zlib.error,
                    EOFError):
                # One damaged member: the others can still be checked.
                top, rel = _member_path(info.filename)
                collect.tops.add(top)
                collect.hashes[rel] = None
    return collect.bundle(path, digest.hexdigest())


def _infos(path: Path) -> list[zipfile.ZipInfo]:
    with zipfile.ZipFile(path) as zf:
        return zf.infolist()
