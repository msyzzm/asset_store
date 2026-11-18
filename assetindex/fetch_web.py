"""Routes for the download queue: list it with live progress, queue a link, cancel,
retry. The downloading itself happens in the fetcher service (assetindex/fetch_job.py)."""

import sqlite3
import time
from collections.abc import Callable
from typing import Annotated

from fastapi import Body, Depends, FastAPI, HTTPException

from assetindex import downloads, scan_request, uploads
from assetindex.config import Settings

SWEEP_SECONDS = 60  # how often listing the downloads also drops abandoned uploads


def _progress(conn: sqlite3.Connection, row: dict, progress: dict) -> dict | None:
    if row["status"] == "active":
        return progress.get(row["id"])
    if row["status"] == "uploading":
        return uploads.progress(conn, row["id"], row["size"])
    return None


def _in_index(conn: sqlite3.Connection, root: str, path: str) -> bool:
    """A file at `path`, or anything under it (a folder pack)."""
    return conn.execute(
        "SELECT 1 FROM files WHERE root = ? AND (rel_path = ? OR (rel_path >= ? AND rel_path < ?)) LIMIT 1",
        (root, path, f"{path}/", f"{path}0"),  # "0" sorts right after "/"
    ).fetchone() is not None


def _gone(conn: sqlite3.Connection, row: dict, scanned: float | None) -> bool:
    """A finished download or upload no longer where it landed (organized away or
    deleted). Only told after a scan that began later: before it, the index may not
    have it yet."""
    return bool(row["path"] and row["status"] in ("done", "duplicate") and scanned and row["finished"]
                and row["finished"] < scanned and not _in_index(conn, row["root"], row["path"]))


def download_state(conn: sqlite3.Connection, data_dir) -> dict:
    progress = downloads.read_progress(data_dir)
    last = scan_request.read_status(data_dir)
    scanned = last.started if last.ok and not last.running else None  # a scan cut short may have missed it
    items = [{**row, "progress": _progress(conn, row, progress), "gone": _gone(conn, row, scanned)}
             for row in downloads.recent(conn)]
    return {"mount": downloads.read_mount(data_dir), "counts": downloads.counts(conn), "items": items}


def writable_root(settings: Settings, root) -> str:
    """The root to download into (the first one if none given), if the fetcher can write
    it; raises HTTPException otherwise."""
    root = root or next(iter(settings.roots))
    if root not in settings.roots:
        raise HTTPException(422, "没有这个根目录")
    mount = downloads.read_mount(settings.data_dir)
    if mount is None:
        raise HTTPException(409, "fetcher 服务还没运行：更新一次（docker compose up -d）后会自动启动")
    if not mount["aria2"]:
        raise HTTPException(409, "fetcher 里没有 aria2：重新构建镜像（docker compose up -d --build）")
    if not mount["roots"].get(root):
        raise HTTPException(409, "NAS 现在对 fetcher 是只读的：在 .env 里设 ASSET_MOVER_ACCESS=rw，再 docker compose up -d")
    return root


def queue_download(conn: sqlite3.Connection, settings: Settings, raw) -> dict:
    """Validate a request ({url, root?, title?, source?, md5?, size?}) and queue it; raises
    HTTPException for the caller to pass on."""
    if not isinstance(raw, dict) or set(raw) - {"url", "root", "title", "source", "md5", "size"}:
        raise HTTPException(422, "请求格式不对")
    root = writable_root(settings, raw.get("root"))
    try:
        download_id = downloads.add(conn, root, raw.get("url"), title=raw.get("title"), source=raw.get("source"),
                                    md5=raw.get("md5"), size=raw.get("size"))
    except ValueError as err:
        raise HTTPException(422, str(err)) from None
    return downloads.get(conn, download_id)


def add_fetch_routes(app: FastAPI, settings: Settings, get_conn: Callable, page_request: Callable) -> None:
    guarded = [Depends(page_request)]
    swept = {"at": float("-inf")}

    @app.get("/api/downloads")
    def api_downloads(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        if time.monotonic() - swept["at"] >= SWEEP_SECONDS:
            swept["at"] = time.monotonic()
            uploads.sweep(conn, settings)
        return download_state(conn, settings.data_dir)

    @app.post("/api/downloads", status_code=201, dependencies=guarded)
    def api_queue(raw: Annotated[dict, Body()], conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return queue_download(conn, settings, raw)

    @app.post("/api/downloads/{download_id}/cancel", dependencies=guarded)
    def api_cancel(download_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        row = downloads.get(conn, download_id)
        if row is None:
            raise HTTPException(404, "没有这个下载")
        if not downloads.cancel(conn, download_id):
            raise HTTPException(409, "这个下载已经结束了")
        if row["source"] == uploads.SOURCE and row["root"] in settings.roots:  # no fetcher clears these
            uploads.clear(settings, row)
        return downloads.get(conn, download_id)

    @app.post("/api/downloads/{download_id}/retry", dependencies=guarded)
    def api_retry(download_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        row = downloads.get(conn, download_id)
        if row is None:
            raise HTTPException(404, "没有这个下载")
        if row["source"] == uploads.SOURCE:
            raise HTTPException(409, "上传的文件在你电脑上：重新上传")
        if not downloads.retry(conn, download_id):
            raise HTTPException(409, "只有失败或取消的下载能重试（取消后稍等几秒）")
        return downloads.get(conn, download_id)
