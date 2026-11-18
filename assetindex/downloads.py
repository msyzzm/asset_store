"""The download queue: links the web UI (or an agent) asks for, kept in the `downloads`
table. The fetcher service (assetindex/fetch_job.py) works through it with aria2.

The table holds each download's state; how far a running one got changes every second,
so it goes in a small JSON file instead (fetch.progress), written by the fetcher and
read by the web server, as the scanner and mover do with their status files.
"""

import json
import re
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
MAX_PARTS = 500
MAX_OUT = 400
MAX_OUT_DEPTH = 8
MAX_NAME_BYTES = 255  # one file or folder name, on most file systems
_RESOLVE = re.compile(r"[a-z]{1,20}:[A-Za-z0-9_.-]{1,100}:[a-z0-9]{1,20}")
_BAD_NAME = re.compile(r'[\x00-\x1f\x7f\\:*?"<>|]')
OPEN = ("queued", "active", "uploading")  # still to finish (uploading: see uploads.py)
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
        source_id=None, now: float | None = None, resolve=None) -> int:
    """`resolve` ("site:id:format"): the fetcher asks the site for the link each time it
    starts the download, `url` being the asset's page (Sketchfab's links expire)."""
    url = check_url(url)
    if resolve is not None and not (isinstance(resolve, str) and _RESOLVE.fullmatch(resolve)):
        raise ValueError("下载方式格式不对")
    if title is not None and not isinstance(title, str):
        raise ValueError("标题格式不对")
    for value in (source, source_id):
        if value is not None and not (isinstance(value, str) and 0 < len(value) <= 200):
            raise ValueError("来源格式不对")
    if size is not None and (type(size) is not int or size < 0):
        raise ValueError("大小格式不对")
    with conn:
        cur = conn.execute(
            "INSERT INTO downloads (root, url, title, source, source_id, md5, size, created, resolve) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (root, url, (title or "").strip()[:MAX_TITLE] or None, source, source_id, _md5(md5), size,
             time.time() if now is None else now, resolve),
        )
    return cur.lastrowid


def check_out(out, max_len: int = MAX_OUT, max_depth: int = MAX_OUT_DEPTH) -> str:
    """A part's path inside its download's folder, as a site gave it: relative, plain
    names only (no "..", no drive or backslash, no hidden or odd characters)."""
    if not isinstance(out, str) or not 0 < len(out.encode()) <= max_len:
        raise ValueError("文件路径不对")
    names = out.split("/")
    if len(names) > max_depth or out.lower().endswith(".aria2") or any(
            not n or n.startswith(".") or n != n.strip() or _BAD_NAME.search(n) or len(n.encode()) > MAX_NAME_BYTES
            for n in names):
        raise ValueError(f"文件路径不对：{out[:100]}")
    return out


def add_bundle(conn: sqlite3.Connection, root: str, page_url, title: str, parts: list[dict], source=None,
               source_id=None, now: float | None = None) -> int:
    """A download of several files ([{url, out, size?, md5?}]), landing as one folder;
    `page_url` (where it came from) stands for them in the list."""
    if not isinstance(parts, list) or not 0 < len(parts) <= MAX_PARTS or not all(isinstance(p, dict) for p in parts):
        raise ValueError(f"要 1 到 {MAX_PARTS} 个文件")
    checked = [(check_url(p.get("url")), check_out(p.get("out")), p.get("size"), _md5(p.get("md5"))) for p in parts]
    outs = [o.lower() for _, o, _, _ in checked]
    if len(set(outs)) != len(outs) or any(b.startswith(f"{a}/") for a in outs for b in outs):  # a file and a folder of one name
        raise ValueError("文件路径重复或互相包含")
    sizes = [s for _, _, s, _ in checked]
    if any(s is not None and (type(s) is not int or s < 0) for s in sizes):
        raise ValueError("大小格式不对")
    total = sum(sizes) if all(s is not None for s in sizes) else None
    download_id = add(conn, root, page_url, title=title, source=source, source_id=source_id, size=total, now=now)
    with conn:
        conn.executemany(
            "INSERT INTO download_parts (download_id, idx, url, out, size, md5) VALUES (?, ?, ?, ?, ?, ?)",
            [(download_id, i, url, out, size, md5) for i, (url, out, size, md5) in enumerate(checked)],
        )
    return download_id


def parts(conn: sqlite3.Connection, download_id: int) -> list[sqlite3.Row]:
    """The files of a download made of several, in order; [] for a one-file download."""
    return conn.execute("SELECT * FROM download_parts WHERE download_id = ? ORDER BY idx", (download_id,)).fetchall()


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
    """Failed or cancelled, and stopped by the fetcher: queued again, from scratch.
    Never an upload: its files are on the sender's side."""
    with conn:
        cur = conn.execute(
            "UPDATE downloads SET status = 'queued', error = NULL, finished = NULL "
            "WHERE id = ? AND status IN ('failed', 'cancelled') AND gid IS NULL AND source IS NOT 'upload'",
            (download_id,),
        )
        if cur.rowcount == 1:  # its folder was cleared: every part again
            conn.execute("UPDATE download_parts SET done = 0, gid = NULL WHERE download_id = ?", (download_id,))
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
