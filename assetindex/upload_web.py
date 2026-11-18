"""Routes for uploads (see assetindex/uploads.py): declare the files, send each one as
the raw body of a PUT, then finish. Cancel and the list are the downloads' routes."""

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from assetindex import downloads, move_job, uploads
from assetindex.config import Settings
from assetindex.db import connect
from assetindex.fetch_job import mark_twin
from assetindex.scanlock import is_scan_running

WRITE_BYTES = 1 << 20  # what arrives is written in pieces of about this size


def mover_busy(data_dir: Path) -> bool:
    return is_scan_running(data_dir, move_job.LOCK_NAME)


def _call(fn, *args, **kwargs):
    """Run an uploads function, its errors turned into HTTP answers."""
    try:
        return fn(*args, **kwargs)
    except LookupError as err:
        raise HTTPException(404, str(err)) from None
    except ValueError as err:
        raise HTTPException(422, str(err)) from None
    except uploads.Refused as err:
        raise HTTPException(409, str(err)) from None


def _open(target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    return target.open("wb")


async def _receive(request: Request, target: Path, size: int) -> None:
    """Write the request body to `target`; it must be exactly `size` bytes. Nothing is
    left behind when it is not, or when the sender goes away midway."""
    f = await run_in_threadpool(_open, target)
    received, pending = 0, []
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > size:
                raise HTTPException(422, f"文件比说好的 {size} 字节大")
            pending.append(chunk)
            if sum(map(len, pending)) >= WRITE_BYTES:
                await run_in_threadpool(f.write, b"".join(pending))
                pending = []
        if pending:
            await run_in_threadpool(f.write, b"".join(pending))
        if received != size:
            raise HTTPException(422, f"文件没传完整：收到 {received} 字节，应为 {size}")
    except BaseException:
        f.close()
        target.unlink(missing_ok=True)
        raise
    await run_in_threadpool(f.close)


def add_upload_routes(app: FastAPI, settings: Settings, get_conn: Callable, page_request: Callable) -> None:
    guarded = [Depends(page_request)]

    @app.post("/api/uploads", status_code=201, dependencies=guarded)
    def api_start(raw: Annotated[dict, Body()], conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        if not isinstance(raw, dict) or set(raw) - {"root", "folder", "name", "files"}:
            raise HTTPException(422, "请求格式不对")
        root = raw.get("root") or next(iter(settings.roots), None)
        if root not in settings.roots:
            raise HTTPException(422, "没有这个根目录")
        uploads.sweep(conn, settings)
        upload_id = _call(uploads.start, conn, settings, root, raw)
        return downloads.get(conn, upload_id)

    @app.put("/api/uploads/{upload_id}/files/{idx}", dependencies=guarded)
    async def api_file(upload_id: int, idx: int, request: Request) -> dict:
        conn = await run_in_threadpool(connect, settings.db_path, False)
        try:
            target, size = await run_in_threadpool(_call, uploads.part_target, conn, settings, upload_id, idx)
            try:
                await _receive(request, target, size)
            except BaseException:
                uploads.clear_if_ended(conn, settings, upload_id)
                raise
            await run_in_threadpool(_call, uploads.part_done, conn, settings, upload_id, idx)
        finally:
            conn.close()
        return {"id": upload_id, "idx": idx, "size": size}

    @app.post("/api/uploads/{upload_id}/finish", dependencies=guarded)
    def api_finish(upload_id: int, background: BackgroundTasks, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        rel = _call(uploads.finish, conn, settings, upload_id, lambda: mover_busy(settings.data_dir))
        row = downloads.get(conn, upload_id)
        if row["url"] == uploads.FILE_URL:  # a folder pack is not one file to compare
            background.add_task(mark_twin, settings, upload_id, row["root"], rel)
        return row
