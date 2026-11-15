"""One scan at a time per data dir (a scheduled scan may still be running when the next
one starts, or someone runs a manual scan meanwhile).

The lock is an OS file lock, so the kernel drops it when the process dies, even on
SIGKILL from an out-of-memory kill; a PID file would go stale. It lives in the data dir,
which must be a local filesystem: locks on NFS are unreliable.
"""

import contextlib
import sys
import time
from collections.abc import Iterator
from pathlib import Path

LOCK_NAME = "scan.lock"
RETRY_SECONDS = 0.05


class ScanLocked(Exception):
    """Another scan holds the lock."""


def _try_lock(f) -> bool:
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


@contextlib.contextmanager
def scan_lock(data_dir: Path, wait_seconds: float = 0) -> Iterator[None]:
    """Hold the scan lock. `wait_seconds` rides out the web server's momentary probe
    (see is_scan_running); a real scan holds the lock far longer and still refuses us."""
    data_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_seconds
    with open(data_dir / LOCK_NAME, "a+b") as f:
        f.seek(0)
        while not _try_lock(f):
            if time.monotonic() >= deadline:
                raise ScanLocked(f"another scan is running (lock {data_dir / LOCK_NAME})")
            time.sleep(RETRY_SECONDS)
        yield  # closing the file releases the lock


def is_scan_running(data_dir: Path) -> bool:
    """True if some process holds the scan lock. Briefly takes the lock to find out,
    which is why scanners wait a moment for it."""
    try:
        f = open(data_dir / LOCK_NAME, "rb")  # never create it: the web side only looks
    except FileNotFoundError:
        return False
    with f:
        return not _try_lock(f)
