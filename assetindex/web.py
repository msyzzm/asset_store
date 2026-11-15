"""FastAPI app: search API, file preview/download, static UI."""

import io
import logging
import mimetypes
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, replace
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi import Path as PathParam
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from assetindex.archive_queries import list_entries
from assetindex.config import Settings
from assetindex.db import connect
from assetindex.extract import ExtractError, find_bsdtar, read_member
from assetindex.kinds import KINDS, LISTABLE_ARCHIVE_EXTS, THUMBNAIL_EXTS
from assetindex.scan_request import is_requested, read_status, request_scan
from assetindex.scanlock import is_scan_running
from assetindex.search import MAX_LIMIT, MAX_QUERY_LENGTH, parse_terms, search
from assetindex.sequences import frames_of
from assetindex.thumbs import (
    entry_thumb_path,
    file_thumb_path,
    preview_image_bytes,
    render_thumbnail,
    sheet_path,
    source_limit,
    unity_preview_path,
)

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
THUMB_CACHE_SECONDS = 7 * 24 * 3600
THUMB_CONCURRENCY = 4  # decoding big PSD/TIFF is memory-heavy; bound parallel renders
ENTRY_THUMB_BOX = (160, 160)
# Extracting from a big solid archive can take seconds: keep it off the regular
# thumbnail slots, cap it tighter, and don't park request threads waiting for a slot.
EXTRACT_CONCURRENCY = 2
EXTRACT_SLOT_WAIT_SECONDS = 5
ENTRY_EXTRACT_TIMEOUT_SECONDS = 10
FAILED_ENTRY_TTL_SECONDS = 600
MAX_FAILED_ENTRIES = 10_000
SQLITE_MAX_INT = 2**63 - 1
RAW_SECURITY_HEADERS = {
    # Assets include .html/.svg. Served inline on our origin they could run scripts
    # or make requests into the LAN; sandbox + default-src 'none' blocks both.
    "Content-Security-Policy": "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'",
    "X-Content-Type-Options": "nosniff",
}

FileId = Annotated[int, PathParam(ge=1, le=SQLITE_MAX_INT)]


def _render_or_415(source: Path | io.BytesIO, target: Path, box: tuple[int, int] | None = None) -> None:
    try:
        if box:
            render_thumbnail(source, target, box)
        else:
            render_thumbnail(source, target)
    except Exception as err:  # Pillow raises many types on malformed files
        if target.exists():
            return  # a concurrent render won the race (e.g. replace blocked on Windows)
        log.warning("thumbnail failed for %r: %r", str(target), err)
        raise HTTPException(415, "cannot decode image") from err


def _thumb_source(path: Path, ext: str, size: int) -> Path | io.BytesIO:
    """Image to render for a file on disk: the file, or a .uasset's embedded thumbnail."""
    if ext != "uasset":
        return path
    limit = source_limit(ext)
    if size > limit:
        raise HTTPException(415, "file too large to preview")
    try:
        # Bounded read: the file may have grown since the scan recorded its size.
        with path.open("rb") as f:
            data = f.read(limit + 1)
    except OSError as err:
        raise HTTPException(415, "cannot read file") from err
    if len(data) > limit:
        raise HTTPException(415, "file too large to preview")
    encoded = preview_image_bytes(ext, data)
    if encoded is None:
        raise HTTPException(415, "no embedded thumbnail")
    return io.BytesIO(encoded)


def _webp(path: Path) -> FileResponse:
    return FileResponse(
        path, media_type="image/webp", headers={"Cache-Control": f"public, max-age={THUMB_CACHE_SECONDS}"}
    )


def create_app(settings: Settings) -> FastAPI:
    connect(settings.db_path).close()  # ensure schema exists once at startup
    app = FastAPI(title="Asset Index")
    thumb_slots = threading.BoundedSemaphore(THUMB_CONCURRENCY)
    extract_slots = threading.BoundedSemaphore(EXTRACT_CONCURRENCY)
    failed_entries: dict[Path, float] = {}  # cache path -> time of failure

    def recently_failed(target: Path) -> bool:
        failed_at = failed_entries.get(target)
        return failed_at is not None and time.monotonic() - failed_at < FAILED_ENTRY_TTL_SECONDS

    def remember_failure(target: Path) -> None:
        if len(failed_entries) >= MAX_FAILED_ENTRIES:
            failed_entries.clear()
        failed_entries[target] = time.monotonic()

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = connect(settings.db_path, init_schema=False)
        try:
            yield conn
        finally:
            conn.close()

    def resolve(conn: sqlite3.Connection, file_id: int) -> tuple[sqlite3.Row, Path]:
        """Map an id to a real path, never trusting anything client-supplied."""
        row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "file not indexed")
        root = settings.roots.get(row["root"])
        if root is None:
            raise HTTPException(404, "asset root not configured")
        root_resolved = root.resolve()
        path = (root_resolved / row["rel_path"]).resolve()
        if not path.is_relative_to(root_resolved) or not path.is_file():
            raise HTTPException(404, "file missing on disk; rescan needed")
        return row, path

    @app.get("/api/search")
    def api_search(
        q: str = Query("", max_length=MAX_QUERY_LENGTH),
        kind: str | None = None,
        limit: int = Query(50, ge=1, le=MAX_LIMIT),
        offset: int = Query(0, ge=0),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        if kind and kind not in KINDS:
            raise HTTPException(422, f"unknown kind {kind!r}")
        result = search(conn, q, kind=kind, limit=limit, offset=offset)
        return {"total": result.total, "items": result.items}

    @app.get("/api/stats")
    def api_stats(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, int]:
        rows = conn.execute("SELECT kind, COUNT(*) AS n FROM files GROUP BY kind")
        return {r["kind"]: r["n"] for r in rows}

    def scan_state() -> dict:
        status = read_status(settings.data_dir)
        if status.running and not is_scan_running(settings.data_dir):
            # The kernel dropped the lock: the scanner died mid-scan (OOM, docker kill).
            status = replace(status, running=False, ok=False)
        return {"requested": is_requested(settings.data_dir), **asdict(status)}

    @app.get("/api/scan")
    def api_scan_status() -> dict:
        return scan_state()

    # No login anywhere, so anyone who can open the page can press this; it only wakes
    # the scanner early (one pending request, one scan at a time, a minimum gap between
    # scans). The custom header keeps other websites from pressing it through a visitor's
    # browser: a cross-origin request carrying it needs a CORS preflight, which we never allow.
    @app.post("/api/scan", status_code=202)
    def api_scan_request(x_requested_with: Annotated[str | None, Header()] = None) -> dict:
        if not x_requested_with:
            raise HTTPException(403, "missing X-Requested-With header")
        try:
            request_scan(settings.data_dir)
        except OSError as err:
            log.error("cannot record scan request: %r", err)
            raise HTTPException(503, "cannot record the scan request; check the data dir") from None
        return scan_state()

    @app.get("/api/files/{file_id}")
    def api_file(file_id: FileId, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "file not indexed")
        return dict(row)

    @app.get("/api/files/{file_id}/frames")
    def api_frames(file_id: FileId, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        frames = frames_of(conn, file_id)
        if frames is None:
            raise HTTPException(404, "file not indexed")
        return {"frames": frames}

    @app.get("/api/files/{file_id}/entries")
    def api_entries(
        file_id: FileId,
        q: str = Query("", max_length=MAX_QUERY_LENGTH),
        limit: int = Query(100, ge=1, le=MAX_LIMIT),
        offset: int = Query(0, ge=0),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        if conn.execute("SELECT 1 FROM files WHERE id = ?", (file_id,)).fetchone() is None:
            raise HTTPException(404, "file not indexed")
        archive = conn.execute(
            "SELECT status, error, entry_count, truncated, engine, engine_version FROM archives WHERE file_id = ?",
            (file_id,),
        ).fetchone()
        total, entries = list_entries(conn, file_id, parse_terms(q), limit, offset)
        return {"archive": dict(archive) if archive else None, "total": total, "entries": entries}

    @app.get("/api/files/{file_id}/raw")
    def api_raw(file_id: FileId, conn: sqlite3.Connection = Depends(get_conn)) -> FileResponse:
        _, path = resolve(conn, file_id)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type, headers=RAW_SECURITY_HEADERS)

    @app.get("/api/files/{file_id}/download")
    def api_download(file_id: FileId, conn: sqlite3.Connection = Depends(get_conn)) -> FileResponse:
        row, path = resolve(conn, file_id)
        return FileResponse(path, filename=row["name"], media_type="application/octet-stream")

    @app.get("/api/files/{file_id}/thumb")
    def api_thumb(file_id: FileId, conn: sqlite3.Connection = Depends(get_conn)) -> FileResponse:
        row, path = resolve(conn, file_id)
        if row["ext"] in LISTABLE_ARCHIVE_EXTS:
            # Contact sheets are built at scan time; never parse an archive in a request.
            sheet = sheet_path(settings.thumb_dir, file_id, row["mtime"])
            if not sheet.exists():
                raise HTTPException(415, "no preview for this archive")
            return _webp(sheet)
        if row["ext"] not in THUMBNAIL_EXTS and row["ext"] != "uasset":
            raise HTTPException(415, "no thumbnail for this file type")
        target = file_thumb_path(settings.thumb_dir, file_id, row["mtime"])
        if not target.exists():
            if recently_failed(target):
                raise HTTPException(415, "cannot decode image")
            with thumb_slots:
                if not target.exists():  # another request may have rendered it meanwhile
                    try:
                        _render_or_415(_thumb_source(path, row["ext"], row["size"]), target)
                    except HTTPException:
                        remember_failure(target)
                        raise
        return _webp(target)

    @app.get("/api/files/{file_id}/entries/{entry_id}/thumb")
    def api_entry_thumb(
        file_id: FileId, entry_id: FileId, conn: sqlite3.Connection = Depends(get_conn)
    ) -> FileResponse:
        entry = conn.execute(
            """
            SELECT e.path, e.ext, e.size AS entry_size, f.ext AS archive_ext, a.size, a.mtime
            FROM archive_entries e
            JOIN files f ON f.id = e.file_id
            JOIN archives a ON a.file_id = e.file_id
            WHERE e.id = ? AND e.file_id = ?
            """,
            (entry_id, file_id),
        ).fetchone()
        if entry is None:
            raise HTTPException(404, "no such entry in this archive")
        if entry["archive_ext"] == "unitypackage":
            preview = unity_preview_path(settings.thumb_dir, file_id, entry["mtime"], entry_id)
            if not preview.exists():
                raise HTTPException(415, "no preview for this asset")
            return _webp(preview)
        limit = source_limit(entry["ext"])
        if limit is None or entry["entry_size"] > limit:
            raise HTTPException(415, "no thumbnail for this entry")
        tool = find_bsdtar()
        if tool is None:
            raise HTTPException(415, "archive previews need bsdtar")
        _, archive = resolve(conn, file_id)
        target = entry_thumb_path(settings.thumb_dir, file_id, entry["mtime"], entry["size"], entry["path"])
        if target.exists():
            return _webp(target)
        if recently_failed(target):
            raise HTTPException(415, "cannot extract entry")
        if not extract_slots.acquire(timeout=EXTRACT_SLOT_WAIT_SECONDS):
            raise HTTPException(503, "busy extracting other previews", headers={"Retry-After": "5"})
        try:
            if not target.exists():
                try:
                    data = read_member(archive, entry["path"], limit, tool, ENTRY_EXTRACT_TIMEOUT_SECONDS)
                    encoded = preview_image_bytes(entry["ext"], data)
                    if encoded is None:
                        raise HTTPException(415, "no embedded thumbnail")
                    _render_or_415(io.BytesIO(encoded), target, ENTRY_THUMB_BOX)
                except (ExtractError, HTTPException) as err:
                    remember_failure(target)
                    raise HTTPException(415, "cannot extract entry") from err
        finally:
            extract_slots.release()
        return _webp(target)

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
