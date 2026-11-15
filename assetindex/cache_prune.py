"""Keep the thumbnail cache from growing forever.

Removes thumbnails whose source file was deleted or changed, and, when the on-demand
caches exceed a size cap, the least recently written ones. Only files whose names this
program generates are touched, so a misconfigured data dir can't lose anything else.
Archive contact sheets are never evicted by the cap: they are only built during a scan,
so an evicted sheet would stay missing until the archive changes.
"""

import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# The web server writes thumbnails while the scanner prunes; leave recent files alone.
FRESH_SECONDS = 600
_NUMBER_DIR = re.compile(r"^\d+$")
_FILE_THUMB = re.compile(r"^(\d+)_(-?\d+)\.webp$")
_ARCHIVE_PREVIEW = re.compile(r"^(?:sheet|u)_(-?\d+)(?:_\d+)?\.webp$")
_HEX_DIR = re.compile(r"^[0-9a-f]{2}$")
_ENTRY_THUMB = re.compile(r"^[0-9a-f]{64}\.webp$")
_TEMP = re.compile(r"^[^.]+\.[0-9a-f]+\.tmp$")  # save_webp's temp file, left by a crash


@dataclass(frozen=True)
class PruneStats:
    removed: int
    freed_bytes: int


@dataclass(frozen=True)
class _Cached:
    path: Path
    size: int
    mtime: float


def _files(directory: Path, pattern: re.Pattern) -> list[tuple[re.Match | None, os.DirEntry]]:
    """(match, entry) for files in directory; match is None for our temp files."""
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return []
    found = []
    for entry in entries:
        if not entry.is_file(follow_symlinks=False):
            continue
        match = pattern.match(entry.name)
        if match or _TEMP.match(entry.name):
            found.append((match, entry))
    return found


def _subdirs(directory: Path, pattern: re.Pattern) -> list[Path]:
    try:
        return [Path(e.path) for e in os.scandir(directory) if e.is_dir(follow_symlinks=False) and pattern.match(e.name)]
    except OSError:
        return []


class _Pruner:
    def __init__(self, now: float):
        self.cutoff = now - FRESH_SECONDS
        self.removed = 0
        self.freed = 0

    def remove(self, path: Path, size: int) -> None:
        try:
            path.unlink()
        except OSError as err:
            log.warning("cannot remove cached %r: %r", str(path), err)
            return
        self.removed += 1
        self.freed += size

    def sweep(self, directory: Path, pattern: re.Pattern, keep) -> list[_Cached]:
        """Remove old temp files and files `keep(match)` rejects; return the rest."""
        kept = []
        for match, entry in _files(directory, pattern):
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue  # renamed or removed by the web process since the listing
            cached = _Cached(Path(entry.path), stat.st_size, stat.st_mtime)
            if cached.mtime > self.cutoff:
                continue  # possibly still being written
            if match is None or not keep(match):
                self.remove(cached.path, cached.size)
            else:
                kept.append(cached)
        return kept


def prune_thumbs(conn: sqlite3.Connection, thumb_dir: Path, max_bytes: int, now: float | None = None) -> PruneStats:
    if not thumb_dir.is_dir():
        return PruneStats(0, 0)
    pruner = _Pruner(time.time() if now is None else now)
    file_mtimes = {r[0]: int(r[1]) for r in conn.execute("SELECT id, mtime FROM files")}
    archive_mtimes = {r[0]: int(r[1]) for r in conn.execute("SELECT file_id, mtime FROM archives")}

    evictable: list[_Cached] = []
    for sub in _subdirs(thumb_dir, _NUMBER_DIR):
        evictable += pruner.sweep(sub, _FILE_THUMB, lambda m: file_mtimes.get(int(m[1])) == int(m[2]))
    for sub in _subdirs(thumb_dir / "archives", _NUMBER_DIR):
        mtime = archive_mtimes.get(int(sub.name))
        pruner.sweep(sub, _ARCHIVE_PREVIEW, lambda m, mtime=mtime: mtime == int(m[1]))
        try:
            sub.rmdir()  # only succeeds once empty
        except OSError:
            pass
    for sub in _subdirs(thumb_dir / "entries", _HEX_DIR):
        # Keyed by a hash of the archive's identity: not checkable, only the cap applies.
        evictable += pruner.sweep(sub, _ENTRY_THUMB, lambda m: True)

    total = sum(c.size for c in evictable)
    for cached in sorted(evictable, key=lambda c: c.mtime):
        if total <= max_bytes:
            break
        pruner.remove(cached.path, cached.size)
        total -= cached.size
    return PruneStats(pruner.removed, pruner.freed)
