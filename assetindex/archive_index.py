"""Keep the `archives` / `archive_entries` tables in sync with archive files on disk."""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from assetindex import archives, attempts
from assetindex.archives import ARCHIVE_EXTS, ArchiveError, ArchiveListing, list_archive
from assetindex.scan_request import ScanStopped, never, no_progress

log = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 300
STAGE = "list"
CRASH_ERROR = "scanner was killed while reading this archive (out of memory?)"


@dataclass(frozen=True)
class ArchiveStats:
    listed: int
    failed: int  # corrupt/encrypted: recorded, retried only when the file changes
    unreadable: int  # I/O errors: nothing recorded, retried next scan
    unchanged: int


def _ext_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _store(
    conn: sqlite3.Connection, file_id: int, size: int, mtime: float,
    listing: ArchiveListing | None, error: str | None,
) -> None:
    conn.execute("DELETE FROM archive_entries WHERE file_id = ?", (file_id,))
    entries = listing.entries if listing else ()
    conn.executemany(
        "INSERT INTO archive_entries (file_id, path, ext, size, guid, has_preview) VALUES (?, ?, ?, ?, ?, ?)",
        [(file_id, e.path, _ext_of(e.path), e.size, e.guid, int(e.has_preview)) for e in entries],
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO archives
            (file_id, size, mtime, status, error, entry_count, truncated, engine, engine_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_id, size, mtime, "error" if error else "ok", error, len(entries),
            int(bool(listing and listing.truncated)),
            listing.engine if listing else None, listing.engine_version if listing else None,
        ),
    )


def index_archives(
    conn: sqlite3.Connection,
    root_name: str,
    root: Path,
    retry_errors: bool = False,
    should_stop: Callable[[], bool] = never,
    progress: Callable[[str, int, int | None], None] = no_progress,
) -> ArchiveStats:
    """List every new or changed archive under one root. Commits per archive, so an
    interrupted run resumes where it stopped. retry_errors relists archives recorded as
    bad (e.g. after upgrading a parser library)."""
    placeholders = ",".join("?" * len(ARCHIVE_EXTS))
    rows = conn.execute(
        f"""
        SELECT f.id, f.rel_path, f.ext, f.size, f.mtime, a.size AS a_size, a.mtime AS a_mtime, a.status, a.error
        FROM files f LEFT JOIN archives a ON a.file_id = f.id
        WHERE f.root = ? AND f.ext IN ({placeholders})
        """,
        (root_name, *sorted(ARCHIVE_EXTS)),
    ).fetchall()

    counts = {"listed": 0, "failed": 0, "unreadable": 0, "unchanged": 0}
    for i, row in enumerate(rows):
        progress("archives", i, len(rows))
        unchanged = (row["a_size"], row["a_mtime"]) == (row["size"], row["mtime"])
        # A changed file is listed anyway; only an unchanged failed one is worth a look at its first bytes.
        retry = unchanged and row["status"] == "error" and (retry_errors or _misnamed(root, row))
        if unchanged and not retry:
            counts["unchanged"] += 1
            continue
        if should_stop():  # before attempts.begin: a stop is not a crash
            raise ScanStopped("stopped while listing archives")
        if not attempts.begin(conn, row["id"], STAGE):
            log.warning("archive %r killed the scanner %d times; marked bad", row["rel_path"], attempts.MAX_ATTEMPTS)
            with conn:
                _store(conn, row["id"], row["size"], row["mtime"], None, CRASH_ERROR)
            attempts.finish(conn, row["id"], STAGE)  # a changed file gets a fresh chance
            counts["failed"] += 1
            continue
        try:
            counts[_list_one(conn, root, row)] += 1
        finally:
            attempts.finish(conn, row["id"], STAGE)
    progress("archives", len(rows), len(rows))
    _recheck_unity(conn, root_name)
    _recheck_daz(conn, root_name)
    return ArchiveStats(**counts)


def _recheck_unity(conn: sqlite3.Connection, root_name: str) -> None:
    """Unchanged archives are not relisted, so redo the Unity guess from the stored entries
    (the rule got stricter on 2026-11-19: models plus a bonus .unitypackage are for any engine)."""
    rows = conn.execute(
        "SELECT f.id, f.ext FROM files f JOIN archives a ON a.file_id = f.id"
        " WHERE f.root = ? AND a.engine = 'unity' AND f.ext != 'unitypackage'",
        (root_name,),
    ).fetchall()
    for row in rows:
        paths = [r[0] for r in conn.execute("SELECT path FROM archive_entries WHERE file_id = ?", (row["id"],))]
        engine, version = archives.detect_engine(row["ext"], paths)
        if engine != "unity":
            with conn:
                conn.execute(
                    "UPDATE archives SET engine = ?, engine_version = ? WHERE file_id = ?", (engine, version, row["id"])
                )


def _recheck_daz(conn: sqlite3.Connection, root_name: str) -> None:
    """Archives listed before DAZ was recognised (2026-11-26) keep no engine until relisted:
    mark the unchanged ones whose stored entries hold a DAZ file."""
    placeholders = ",".join("?" * len(archives.DAZ_EXTS))
    with conn:
        conn.execute(
            "UPDATE archives SET engine = 'daz', engine_version = NULL"
            " WHERE engine IS NULL AND file_id IN (SELECT f.id FROM files f WHERE f.root = ?)"
            f" AND EXISTS (SELECT 1 FROM archive_entries e WHERE e.file_id = archives.file_id"
            f" AND lower(e.ext) IN ({placeholders}))",
            (root_name, *sorted(archives.DAZ_EXTS)),
        )


def _misnamed(root: Path, row: sqlite3.Row) -> bool:
    """Failed when read by its name, and its first bytes say it is another format: worth
    one more try (once read by content, its error says so and it is not retried again)."""
    if row["ext"] == "unitypackage" or (row["error"] or "").startswith(archives.READ_AS):
        return False
    try:
        fmt = archives.sniff_format(root / row["rel_path"])
    except OSError:
        return False
    return fmt is not None and fmt != row["ext"]


def _list_one(conn: sqlite3.Connection, root: Path, row: sqlite3.Row) -> str:
    try:
        listing = list_archive(root / row["rel_path"], row["ext"])
    except (OSError, MemoryError) as err:
        # Transient (NAS hiccup, memory pressure): keep old entries, retry next scan.
        log.warning("cannot read archive %r: %r", row["rel_path"], err)
        return "unreadable"
    except ArchiveError as err:
        log.info("bad archive %r: %s", row["rel_path"], err)
        with conn:
            _store(conn, row["id"], row["size"], row["mtime"], None, str(err)[:MAX_ERROR_LENGTH] or "unreadable")
        return "failed"
    with conn:
        _store(conn, row["id"], row["size"], row["mtime"], listing, None)
    return "listed"
