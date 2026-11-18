"""Uploads: files sent to the web server from the page (or an agent), landing in the
root's _下载 folder like a finished download.

An upload is a row in `downloads` (source "upload", status "uploading") with one
download_parts row per file, its path and size declared up front. Each file is written
into _下载/.partial/<id>/ as it arrives (the scanner never looks there) and its part
marked done once every byte came; when all are done the upload is moved into _下载: one
file as itself, a folder as one pack. The fetcher only works on queued and active rows,
so it never touches these.

The web server writes the NAS here, with the same switch as the mover and fetcher
(ASSET_MOVER_ACCESS=rw in .env): read-only otherwise, and uploads are refused.
"""

import logging
import os
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from assetindex import downloads, scan_request
from assetindex.config import Settings
from assetindex.fetch_job import FREE_MARGIN, _clear, _landing, partial_dir, safe_name
from assetindex.organize_apply import move_no_replace
from assetindex.unpack import free_bytes

log = logging.getLogger(__name__)

SOURCE = "upload"
FILE_URL, FOLDER_URL = "upload:file", "upload:folder"  # the url column: what was uploaded
MAX_FILES = 20_000
MAX_PATH = 1000  # bytes of one file's path inside the folder
MAX_DEPTH = 64
IDLE_SECONDS = 3600  # nothing arrived for this long: the page was closed, the upload is dropped


class Refused(Exception):
    """The upload cannot do this now (HTTP 409)."""


def writable(base: Path) -> bool:
    return os.access(base, os.W_OK)


def _files(raw, folder: bool) -> list[tuple[str, int]]:
    files = raw.get("files")
    if not isinstance(files, list) or not 0 < len(files) <= MAX_FILES or not all(isinstance(f, dict) for f in files):
        raise ValueError(f"要 1 到 {MAX_FILES} 个文件")
    if not folder and len(files) != 1:
        raise ValueError("几个文件一起传要作为一个文件夹")
    checked = []
    for f in files:
        size = f.get("size")
        if type(size) is not int or size < 0:
            raise ValueError("大小格式不对")
        path = downloads.check_out(f.get("path"), max_len=MAX_PATH, max_depth=MAX_DEPTH if folder else 1)
        checked.append((path, size))
    lowered = [p.lower() for p, _ in checked]
    seen = set(lowered)
    if len(seen) != len(lowered):
        raise ValueError("文件路径重复")
    for path in lowered:  # a file and a folder of one name
        names = path.split("/")
        if any("/".join(names[:i]) in seen for i in range(1, len(names))):
            raise ValueError(f"文件路径互相包含：{path[:100]}")
    return checked


def start(conn: sqlite3.Connection, settings: Settings, root: str, raw, now: float | None = None) -> int:
    """Check a request ({folder?, name?, files: [{path, size}]}) and record the upload.
    Raises ValueError for a bad request, Refused when the NAS cannot take it."""
    folder = raw.get("folder", False)
    if not isinstance(folder, bool):
        raise ValueError("folder 格式不对")
    files = _files(raw, folder)
    name = raw.get("name") if folder else files[0][0]
    if not isinstance(name, str) or not name.strip():
        raise ValueError("文件夹要有名字")
    base = settings.roots[root].resolve()
    if not writable(base):
        raise Refused("NAS 现在对网页服务是只读的：在 .env 里设 ASSET_MOVER_ACCESS=rw，再 docker compose up -d")
    total = sum(size for _, size in files)
    if free_bytes(base) < total + FREE_MARGIN:
        raise Refused("NAS 空间不够放这些文件")
    with conn:
        cur = conn.execute(
            "INSERT INTO downloads (root, url, title, source, size, status, created) VALUES (?, ?, ?, ?, ?, 'uploading', ?)",
            (root, FOLDER_URL if folder else FILE_URL, name.strip()[:downloads.MAX_TITLE], SOURCE, total,
             time.time() if now is None else now),
        )
        conn.executemany(
            "INSERT INTO download_parts (download_id, idx, url, out, size) VALUES (?, ?, '', ?, ?)",
            [(cur.lastrowid, i, path, size) for i, (path, size) in enumerate(files)],
        )
    return cur.lastrowid


def _row(conn: sqlite3.Connection, upload_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM downloads WHERE id = ? AND source = ?", (upload_id, SOURCE)).fetchone()
    if row is None:
        raise LookupError("没有这个上传")
    if row["status"] != "uploading":
        raise Refused("这个上传已经结束了")
    return row


def _folder(settings: Settings, row) -> Path:
    return partial_dir(settings.roots[row["root"]].resolve(), row["id"])


def part_target(conn: sqlite3.Connection, settings: Settings, upload_id: int, idx: int) -> tuple[Path, int]:
    """Where file `idx` of the upload is written and its size; it counts as not arrived
    until part_done (a file sent again starts over)."""
    row = _row(conn, upload_id)
    part = conn.execute("SELECT out, size FROM download_parts WHERE download_id = ? AND idx = ?",
                        (upload_id, idx)).fetchone()
    if part is None:
        raise LookupError("这个上传里没有这个文件")
    with conn:
        conn.execute("UPDATE download_parts SET done = 0 WHERE download_id = ? AND idx = ?", (upload_id, idx))
    return _folder(settings, row).joinpath(*part["out"].split("/")), part["size"]


def part_done(conn: sqlite3.Connection, settings: Settings, upload_id: int, idx: int) -> None:
    """Every byte of file `idx` arrived. Refused if the upload was cancelled meanwhile
    (its folder is cleared again: this file may have been written after the cancel did)."""
    with conn:
        cur = conn.execute(
            "UPDATE download_parts SET done = 1 WHERE download_id = ? AND idx = ? "
            "AND (SELECT status FROM downloads WHERE id = ?) = 'uploading'",
            (upload_id, idx, upload_id),
        )
    if cur.rowcount != 1:
        row = conn.execute("SELECT root FROM downloads WHERE id = ?", (upload_id,)).fetchone()
        if row is not None:
            _clear(partial_dir(settings.roots[row["root"]].resolve(), upload_id))
        raise Refused("这个上传已经结束了")


def clear(settings: Settings, row) -> None:
    """Delete what a cancelled upload had received."""
    _clear(_folder(settings, row))


def clear_if_ended(conn: sqlite3.Connection, settings: Settings, upload_id: int) -> None:
    """After a file failed to arrive: if the upload ended meanwhile (cancelled while the
    file was opened, which may have made its folder again), nothing of it stays."""
    row = conn.execute("SELECT * FROM downloads WHERE id = ? AND source = ?", (upload_id, SOURCE)).fetchone()
    if row is not None and row["status"] != "uploading" and row["root"] in settings.roots:
        clear(settings, row)


def finish(conn: sqlite3.Connection, settings: Settings, upload_id: int, mover_busy: Callable[[], bool],
           move: Callable[[str, str, bool], None] = move_no_replace, now: float | None = None) -> str:
    """Move a complete upload into _下载; its path under the root."""
    row = _row(conn, upload_id)
    parts = downloads.parts(conn, upload_id)
    missing = sum(1 for p in parts if not p["done"])
    if missing:
        raise Refused(f"还有 {missing} 个文件没传完")
    folder = _folder(settings, row)
    for part in parts:
        path = folder.joinpath(*part["out"].split("/"))
        if not path.is_file() or path.stat().st_size != part["size"]:
            raise Refused(f"{part['out']} 不完整：重新上传")
    os.utime(folder)  # waiting for a move job is no reason to drop it (see sweep)
    if mover_busy():
        raise Refused("整理正在移动文件：等它结束再放进 _下载（页面会自己再试）")
    base = settings.roots[row["root"]].resolve()
    target = _landing(base, safe_name(row["title"], upload_id))
    rel = target.relative_to(base).as_posix()
    is_folder = row["url"] == FOLDER_URL
    with conn:  # claimed first: a cancel from now on finds it ended
        cur = conn.execute("UPDATE downloads SET status = 'done', path = ?, finished = ? WHERE id = ? AND status = 'uploading'",
                           (rel, time.time() if now is None else now, upload_id))
    if cur.rowcount != 1:
        raise Refused("这个上传已经结束了")
    try:
        move(str(folder if is_folder else folder / parts[0]["out"]), str(target), is_folder)
    except OSError as err:
        with conn:
            conn.execute("UPDATE downloads SET status = 'failed', path = NULL, error = ? WHERE id = ?",
                         (f"传完了，但放不进 _下载：{err!r}", upload_id))
        _clear(folder)
        raise Refused(f"传完了，但放不进 _下载：{err}") from None
    _clear(folder)
    scan_request.request_scan(settings.data_dir)
    log.info("upload %s landed at %s", upload_id, rel)
    return rel


def _last_activity(folder: Path, created: float, since: float) -> float:
    """When the upload last saw something arrive; its files are only looked at when the
    upload and its folder are older than `since` (thousands of them, on the NAS)."""
    try:
        last = max(created, folder.stat().st_mtime)
        if last >= since:
            return last
        return max([last, *(p.stat().st_mtime for p in folder.rglob("*"))])
    except OSError:
        return created


def sweep(conn: sqlite3.Connection, settings: Settings, now: float | None = None) -> None:
    """Drop uploads nothing arrived for in IDLE_SECONDS (the page was closed midway)."""
    now = time.time() if now is None else now
    rows = conn.execute("SELECT * FROM downloads WHERE source = ? AND status = 'uploading'", (SOURCE,)).fetchall()
    for row in rows:
        if row["root"] not in settings.roots:
            continue
        folder = _folder(settings, row)
        if now - _last_activity(folder, row["created"], now - IDLE_SECONDS) < IDLE_SECONDS:
            continue
        with conn:
            cur = conn.execute(
                "UPDATE downloads SET status = 'failed', error = ?, finished = ? WHERE id = ? AND status = 'uploading'",
                ("上传中断了（一小时没收到数据）：重新上传", now, row["id"]),
            )
        if cur.rowcount == 1:
            _clear(folder)
            log.info("upload %s abandoned", row["id"])


def progress(conn: sqlite3.Connection, upload_id: int, total: int | None) -> dict:
    done = conn.execute("SELECT COALESCE(SUM(size), 0) FROM download_parts WHERE download_id = ? AND done = 1",
                        (upload_id,)).fetchone()[0]
    return {"done": done, "total": total or 0, "speed": 0}
