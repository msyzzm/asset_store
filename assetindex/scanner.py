"""Walk an asset root and sync its files into the index."""

import logging
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from assetindex.kinds import kind_of
from assetindex.scan_request import ScanStopped, never, no_progress
from assetindex.sequences import group_sequences, sequence_of

log = logging.getLogger(__name__)

IGNORED_DIRS: frozenset[str] = frozenset({
    "node_modules", ".git", ".svn", "__pycache__", "@eaDir", "#recycle",
})
BATCH_SIZE = 2000
# Past this many unreadable paths, something is broadly wrong: skip deletion entirely.
MAX_PROTECTED_PREFIXES = 200


@dataclass(frozen=True)
class FileEntry:
    rel_path: str
    size: int
    mtime: float


@dataclass(frozen=True)
class ScanStats:
    added: int
    updated: int
    removed: int
    unchanged: int
    deletion_skipped: bool = False
    protected_prefixes: tuple[str, ...] = ()


def is_utf8(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _walk(root: Path, unreadable: list[str], should_stop: Callable[[], bool] = never) -> Iterator[FileEntry]:
    """Yield files under root. Paths that couldn't be read go into `unreadable`
    as root-relative POSIX paths ("" means the root itself). Checks `should_stop`
    once per folder."""

    def rel_of(path: str) -> str:
        try:
            rel = Path(path).relative_to(root).as_posix()
        except ValueError:
            return ""
        return "" if rel == "." else rel

    def on_error(err: OSError) -> None:
        log.warning("cannot read %r: %s", err.filename, err.strerror)
        unreadable.append(rel_of(err.filename or str(root)))

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        if should_stop():
            raise ScanStopped(f"stopped while walking {root}")
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            rel = rel_of(full)
            if not is_utf8(rel):
                # e.g. GBK names on a Linux NFS mount; SQLite and JSON can't carry them.
                log.warning("skipping file with undecodable name %r", full)
                continue
            try:
                st = os.stat(full)
            except OSError as err:
                log.warning("cannot stat %r: %s", full, err.strerror)
                unreadable.append(rel)
                continue
            yield FileEntry(rel_path=rel, size=st.st_size, mtime=st.st_mtime)


def _ext(rel_path: str) -> str:
    name = rel_path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _row(root_name: str, scan_id: int, entry: FileEntry) -> tuple:
    ext = _ext(entry.rel_path)
    kind = kind_of(ext)
    seq_key, seq_index = sequence_of(entry.rel_path, kind) or (None, None)
    return (
        root_name, entry.rel_path, entry.rel_path.rsplit("/", 1)[-1],
        ext, kind, entry.size, entry.mtime, scan_id, seq_key, seq_index,
    )


def _upsert(conn: sqlite3.Connection, root_name: str, scan_id: int, batch: list[FileEntry]) -> None:
    conn.executemany(
        """
        INSERT INTO files (root, rel_path, name, ext, kind, size, mtime, scan_id, seq_key, seq_index)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (root, rel_path) DO UPDATE SET
            size = excluded.size, mtime = excluded.mtime,
            kind = excluded.kind, scan_id = excluded.scan_id,
            seq_key = excluded.seq_key, seq_index = excluded.seq_index
        """,
        [_row(root_name, scan_id, e) for e in batch],
    )


def _delete_stale(
    conn: sqlite3.Connection, root_name: str, scan_id: int, protected: tuple[str, ...]
) -> int:
    """Delete rows not seen in this scan, except under paths we couldn't read."""
    clauses = ["root = ?", "scan_id <> ?"]
    params: list[object] = [root_name, scan_id]
    for prefix in protected:
        # substr instead of LIKE: '_' and '%' are common in asset file names.
        clauses.append("NOT (rel_path = ? OR substr(rel_path, 1, ?) = ?)")
        params.extend([prefix, len(prefix) + 1, f"{prefix}/"])
    return conn.execute(f"DELETE FROM files WHERE {' AND '.join(clauses)}", params).rowcount


def scan_root(
    conn: sqlite3.Connection,
    root_name: str,
    root: Path,
    should_stop: Callable[[], bool] = never,
    progress: Callable[[str, int, int | None], None] = no_progress,
) -> ScanStats:
    """Raises ScanStopped if stopped mid-walk; nothing is deleted then, since files not
    reached yet would look gone."""
    if not root.is_dir():
        raise FileNotFoundError(f"asset root {root_name!r} not found: {root}")

    known = {
        r["rel_path"]: (r["size"], r["mtime"])
        for r in conn.execute("SELECT rel_path, size, mtime FROM files WHERE root = ?", (root_name,))
    }
    scan_id = time.time_ns()
    added = updated = unchanged = 0
    batch: list[FileEntry] = []
    unreadable: list[str] = []

    for entry in _walk(root, unreadable, should_stop):
        previous = known.get(entry.rel_path)
        if previous is None:
            added += 1
        elif previous != (entry.size, entry.mtime):
            updated += 1
        else:
            unchanged += 1
        progress("walk", added + updated + unchanged, None)
        batch.append(entry)
        if len(batch) >= BATCH_SIZE:
            with conn:
                _upsert(conn, root_name, scan_id, batch)
            batch = []

    protected = tuple(sorted(set(unreadable)))
    seen = added + updated + unchanged
    # An unmounted share looks like an empty dir; an unreadable root hides everything.
    # In those cases "not seen" doesn't mean "deleted", so keep every old row.
    skip_delete = (
        "" in protected
        or len(protected) > MAX_PROTECTED_PREFIXES
        or (seen == 0 and bool(known))
    )
    with conn:
        _upsert(conn, root_name, scan_id, batch)
        removed = 0 if skip_delete else _delete_stale(conn, root_name, scan_id, protected)
        group_sequences(conn, root_name)
    if skip_delete or protected:
        log.warning(
            "root %r: %d unreadable paths, %d files seen; %s",
            root_name, len(protected), seen,
            "skipped removing stale entries" if skip_delete else "kept entries under unreadable paths",
        )

    return ScanStats(
        added=added, updated=updated, removed=removed, unchanged=unchanged,
        deletion_skipped=skip_delete, protected_prefixes=protected,
    )
