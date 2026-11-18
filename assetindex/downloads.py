"""The download queue: links the web UI (or an agent) asks for, kept in the `downloads`
table. The fetcher service (assetindex/fetch_job.py) works through it with aria2.

The table holds each download's state; how far a running one got changes every second,
so it goes in a small JSON file instead (fetch.progress), written by the fetcher and
read by the web server, as the scanner and mover do with their status files.
"""

import json
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

from assetindex.scan_request import write_json_atomic

PROGRESS_NAME = "fetch.progress"
MOUNT_NAME = "fetch.mount"
MOUNT_STALE_SECONDS = 300  # the fetcher rewrites fetch.mount every ~90 s; older: it is not running
MAX_URL = 4000
MAX_TITLE = 300
LIST_LIMIT = 200
OPEN = ("queued", "active")  # still to finish
ENDED = ("done", "duplicate", "failed", "cancelled")
_COLUMNS = "id, root, url, title, source, source_id, md5, size, status, path, dup_of, error, created, finished"


def check_url(url) -> str:
    """The link, if the fetcher may download it: http or https with a host."""
    if not isinstance(url, str) or not url.strip() or len(url) > MAX_URL:
        raise ValueError("要一个下载链接")
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("只能下载 http:// 或 https:// 开头的链接")
    return url


def _md5(value) -> str | None:
    if value is None:
        return None
    if not (isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdefABCDEF" for c in value)):
        raise ValueError("md5 格式不对")
    return value.lower()


def add(conn: sqlite3.Connection, root: str, url, title=None, source=None, md5=None, size=None,
        source_id=None, now: float | None = None) -> int:
    url = check_url(url)
    if title is not None and not isinstance(title, str):
        raise ValueError("标题格式不对")
    for value in (source, source_id):
        if value is not None and not (isinstance(value, str) and 0 < len(value) <= 200):
            raise ValueError("来源格式不对")
    if size is not None and (type(size) is not int or size < 0):
        raise ValueError("大小格式不对")
    with conn:
        cur = conn.execute(
            "INSERT INTO downloads (root, url, title, source, source_id, md5, size, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (root, url, (title or "").strip()[:MAX_TITLE] or None, source, source_id, _md5(md5), size,
             time.time() if now is None else now),
        )
    return cur.lastrowid


def wanted(conn: sqlite3.Connection, source: str, source_ids: list[str]) -> set[str]:
    """Which of these assets of `source` are queued, downloading or downloaded."""
    if not source_ids:
        return set()
    marks = ", ".join("?" for _ in source_ids)
    rows = conn.execute(
        f"SELECT DISTINCT source_id FROM downloads WHERE source = ? AND source_id IN ({marks}) "
        "AND status NOT IN ('failed', 'cancelled')",
        (source, *source_ids),
    )
    return {r["source_id"] for r in rows}


def get(conn: sqlite3.Connection, download_id: int) -> dict | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM downloads WHERE id = ?", (download_id,)).fetchone()
    return dict(row) if row else None


def recent(conn: sqlite3.Connection, limit: int = LIST_LIMIT) -> list[dict]:
    """Unfinished downloads first (oldest first, the order they run in), then ended ones, newest first."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM downloads ORDER BY status IN {OPEN} DESC, "
        f"CASE WHEN status IN {OPEN} THEN id ELSE -id END LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def cancel(conn: sqlite3.Connection, download_id: int, now: float | None = None) -> bool:
    """Queued or running: cancelled (the fetcher stops it and clears its folder)."""
    with conn:
        cur = conn.execute(
            f"UPDATE downloads SET status = 'cancelled', finished = ? WHERE id = ? AND status IN {OPEN}",
            (time.time() if now is None else now, download_id),
        )
    return cur.rowcount == 1


def retry(conn: sqlite3.Connection, download_id: int) -> bool:
    """Failed or cancelled, and stopped by the fetcher: queued again, from scratch."""
    with conn:
        cur = conn.execute(
            "UPDATE downloads SET status = 'queued', error = NULL, finished = NULL "
            "WHERE id = ? AND status IN ('failed', 'cancelled') AND gid IS NULL",
            (download_id,),
        )
    return cur.rowcount == 1


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM downloads GROUP BY status")}


def write_progress(data_dir: Path, items: dict[int, dict]) -> None:
    write_json_atomic(data_dir / PROGRESS_NAME, {"at": time.time(), "items": {str(k): v for k, v in items.items()}})


def read_progress(data_dir: Path) -> dict[int, dict]:
    """{download id: {"done", "total", "speed"}} for running downloads; {} if unknown."""
    try:
        raw = json.loads((data_dir / PROGRESS_NAME).read_text(encoding="utf-8"))
        items = raw["items"]
        return {int(k): v for k, v in items.items() if isinstance(v, dict)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def write_mount(data_dir: Path, roots: dict[str, bool], has_aria2: bool) -> None:
    """A heartbeat from the fetcher: which roots it may write, and whether aria2 runs."""
    write_json_atomic(data_dir / MOUNT_NAME, {"roots": roots, "aria2": has_aria2, "at": time.time()})


def read_mount(data_dir: Path, now: float | None = None) -> dict | None:
    """{"roots": {name: writable}, "aria2": bool}, or None if no fetcher runs."""
    try:
        raw = json.loads((data_dir / MOUNT_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("roots"), dict) or not isinstance(raw.get("at"), (int, float)):
        return None
    if (time.time() if now is None else now) - raw["at"] > MOUNT_STALE_SECONDS:
        return None
    return {
        "roots": {k: v for k, v in raw["roots"].items() if isinstance(v, bool)},
        "aria2": raw.get("aria2") is True,
    }
