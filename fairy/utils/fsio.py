"""Atomic file write helpers.

harvest.json and watchdog.state must never be observable in a torn state:
write to a sibling temp file, fsync, rename.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, text: str, mode: int | None = None) -> None:
    """Write ``text`` to ``path`` atomically.

    The temp file is unique per process and thread: two writers (the
    watchdog's harvest thread and its main loop) sharing one ``<name>.tmp``
    could rename each other's half-written file or fail with ENOENT. On
    failure (a full disk) the temp file is removed, not left behind.
    """
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w") as fh:
            fh.write(text)
            fh.flush()
            if mode is not None:
                os.fchmod(fh.fileno(), mode)
            os.fsync(fh.fileno())
        os.rename(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def atomic_write_json(path: Path, document: Any) -> None:
    atomic_write_text(path, json.dumps(document, indent=2) + "\n")


def fsync_dir(path: Path) -> None:
    """Flush a directory's entries (creations, renames) to disk."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_tree(root: Path) -> None:
    """Flush every file and directory under ``root`` (inclusive) to disk.

    A rename only commits the name: after a power cut, files whose data was
    still in the page cache come back zero-length. Run this before renaming a
    finished tree into place so the rename commits real content.
    """
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            fd = os.open(os.path.join(dirpath, name), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fsync_dir(Path(dirpath))


def dir_size_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def sha256_file(path: Path) -> str:
    """Streaming SHA-256 hex digest of a file (constant memory)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextlib.contextmanager
def locked(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive ``flock`` on ``lock_path`` (created group-writable).

    Serialises read-modify-write updates of a shared file between processes
    (the root watchdog, operator CLIs) and threads. Not re-entrant: never
    nest two ``locked()`` on the same path.
    """
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o664)
    try:
        with contextlib.suppress(OSError):
            os.fchmod(fd, 0o664)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
