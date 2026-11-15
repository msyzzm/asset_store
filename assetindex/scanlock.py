"""One scan at a time per data dir (a scheduled scan may still be running when the next
one starts, or someone runs a manual scan meanwhile).

The lock is an OS file lock, so the kernel drops it when the process dies, even on
SIGKILL from an out-of-memory kill; a PID file would go stale. It lives in the data dir,
which must be a local filesystem: locks on NFS are unreliable.
"""

import contextlib
import sys
from collections.abc import Iterator
from pathlib import Path

LOCK_NAME = "scan.lock"


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
def scan_lock(data_dir: Path) -> Iterator[None]:
    data_dir.mkdir(parents=True, exist_ok=True)
    with open(data_dir / LOCK_NAME, "a+b") as f:
        f.seek(0)
        if not _try_lock(f):
            raise ScanLocked(f"another scan is running (lock {data_dir / LOCK_NAME})")
        yield  # closing the file releases the lock
