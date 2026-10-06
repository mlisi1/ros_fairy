"""An identifier of the ros_fairy code that is running.

``__version__`` stays the same between installs from the same checkout, so it
can't tell an up-to-date watchdog from one still running the code it started
with. This hashes the package's own source files: two processes with the same
id run the same code.
"""

import functools
import hashlib
from pathlib import Path


@functools.lru_cache(maxsize=1)
def code_id() -> str:
    """12 hex digits; computed once per process (so a long-running process
    keeps reporting the code it started with)."""
    root = Path(__file__).resolve().parent.parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        try:
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
        except OSError:
            continue
    return digest.hexdigest()[:12]
