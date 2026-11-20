"""FastAPI app: search API, file preview/download, static UI."""

import contextlib
import io
import logging
import mimetypes
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import asdict, replace
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi import Path as PathParam
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from assetindex.agent_api import add_agent_routes
from assetindex.archive_queries import list_entries
from assetindex.caption_web import add_caption_routes
from assetindex.config import Settings
from assetindex.db import connect
from assetindex.embed_stage import progress
from assetindex.embedding import (
    EmbedClient,
    EmbedConfig,
    EmbedError,
    apply_update,
    load_config,
    probe,
    public_view,
    save_config,
)
from assetindex.extract import ExtractError, find_bsdtar, read_member
from assetindex.kinds import KINDS, LISTABLE_ARCHIVE_EXTS, THUMBNAIL_EXTS
from assetindex.external_web import add_external_routes
from assetindex.fetch_web import add_fetch_routes
from assetindex.upload_web import add_upload_routes
from assetindex.move_web import add_move_routes
from assetindex.organize_web import add_organize_routes
from assetindex.scan_request import is_requested, read_status, request_scan, stop_requested, timing
from assetindex.scan_request import request_stop as request_scan_stop
from assetindex.scanlock import is_scan_running
from assetindex.search import MAX_LIMIT, MAX_QUERY_LENGTH, fetch_items, parse_terms, search
from assetindex import fab_covers, pack_grid
from assetindex.packs import pack_facets, search_packs
from assetindex.tags import add_tag_routes
from assetindex.semantic import NoVectors, VectorIndex, ranked_ids, semantic_search
from assetindex.sequences import frames_of
from assetindex.updater import add_update_routes
from assetindex.thumbs import (
    EMBEDDED_PREVIEW_EXTS,
    THUMB_SIZE,
    compose_sheet,
    entry_thumb_path,
    file_thumb_path,
    open_image,
    preview_image_bytes,
    render_thumbnail,
    sheet_path,
    source_limit,
    unity_preview_path,
)

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class _RevalidatedStaticFiles(StaticFiles):
    """The page's own files: the browser asks every time (an unchanged file is a cheap
    304), so after an update it never runs an old cached page or script."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response
THUMB_CACHE_SECONDS = 7 * 24 * 3600
THUMB_CONCURRENCY = 4  # decoding big PSD/TIFF is memory-heavy; bound parallel renders
ENTRY_THUMB_BOX = (160, 160)
ENTRY_LARGE_BOX = (2048, 2048)  # the large view of a picture inside an archive
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
QUERY_EMBED_TIMEOUT_SECONDS = 15
QUERY_VECTOR_CACHE_SIZE = 256  # "load more" asks for the same query again


PROBE_SLOTS = 1  # connection tests at once
SEMANTIC_SLOTS = 4  # semantic searches waiting on the API at once
MAX_PREFIX_LENGTH = 1000  # a pack's folder, for listing what it holds
PACK_MEANING_CARDS = 3000  # cards semantic search hands to search by pack, before grouping


class _Busy:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = 0


@contextlib.contextmanager
def slot(busy: _Busy, limit: int, message: str) -> Iterator[None]:
    """Run at most `limit` of something at once; refuse (429) rather than queue."""
    with busy.lock:
        if busy.running >= limit:
            raise HTTPException(429, message)
        busy.running += 1
    try:
        yield
    finally:
        with busy.lock:
            busy.running -= 1


def page_request(x_requested_with: Annotated[str | None, Header()] = None) -> None:
    """Guard for state-changing endpoints. There is no login, but this keeps other
    websites from acting through a visitor's browser: a cross-origin request carrying a
    custom header needs a CORS preflight, which this app never allows."""
    if not x_requested_with:
        raise HTTPException(403, "missing X-Requested-With header")


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
    """Image to render for a file on disk: the file, or the one embedded in it (.uasset, .eps)."""
    if ext not in EMBEDDED_PREVIEW_EXTS:
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
    vectors = VectorIndex()
    query_vectors: OrderedDict[tuple[str, str, str], list[float]] = OrderedDict()
    cache_lock = threading.Lock()
    probe_busy, semantic_busy = _Busy(), _Busy()

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
        mode: Literal["text", "semantic"] = "text",
        prefix: str = Query("", max_length=MAX_PREFIX_LENGTH),
        sort: Literal["", "new"] = "",  # "new": indexed last first; semantic search keeps its own order
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        if kind and kind not in KINDS:
            raise HTTPException(422, f"unknown kind {kind!r}")
        if mode == "semantic" and parse_terms(q):
            result = semantic(conn, q, kind, limit, offset)
        else:
            result = search(conn, q, kind=kind, limit=limit, offset=offset, prefix=prefix, sort=sort)
        return {"total": result.total, "items": result.items}

    @app.get("/api/files/{file_id}/card")
    def api_card(file_id: FileId, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        """One file as a search result shows it (the page opens its viewer with this)."""
        items = fetch_items(conn, [file_id], [])
        if not items:
            raise HTTPException(404, "file not indexed")
        return items[0]

    def query_vector(config: EmbedConfig, q: str) -> list[float]:
        key = (config.base_url, config.model, q)
        with cache_lock:
            if key in query_vectors:
                query_vectors.move_to_end(key)
                return query_vectors[key]
        vector = EmbedClient(config, timeout=QUERY_EMBED_TIMEOUT_SECONDS).embed_texts([q])[0]
        with cache_lock:
            query_vectors[key] = vector
            while len(query_vectors) > QUERY_VECTOR_CACHE_SIZE:
                query_vectors.popitem(last=False)
        return vector

    def semantic(conn: sqlite3.Connection, q: str, kind: str | None, limit: int, offset: int):
        config = load_config(settings.data_dir)
        if not config.active:
            raise HTTPException(409, "语义搜索还没启用：在设置里填好接口并启用")
        # Each new query is a call to the API (maybe a paid one) holding a request thread.
        with slot(semantic_busy, SEMANTIC_SLOTS, "语义搜索正忙，稍后再试"):
            try:
                vector = query_vector(config, q)
                return semantic_search(
                    conn, vectors, EmbedClient(config), config.model, q, kind, limit, offset, query_vector=vector
                )
            except EmbedError as err:
                raise HTTPException(502, f"向量接口出错：{err}") from None
            except NoVectors:
                raise HTTPException(409, "还没有可用的向量：等扫描算完再搜（换了模型也要重新算）") from None

    @app.get("/api/settings/embedding")
    def api_embedding_settings(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        config = load_config(settings.data_dir)
        return {**public_view(config), "progress": progress(conn, config.model)}

    @app.put("/api/settings/embedding", dependencies=[Depends(page_request)])
    def api_save_embedding_settings(
        update: Annotated[dict, Body()], conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        try:
            config = apply_update(load_config(settings.data_dir), update)
        except ValueError as err:
            raise HTTPException(422, str(err)) from None
        try:
            save_config(settings.data_dir, config)
        except OSError as err:
            log.error("cannot save embedding settings: %r", err)
            raise HTTPException(503, "设置保存失败，检查数据目录") from None
        with conn:  # inputs refused under the old settings get another try under the new ones
            conn.execute("DELETE FROM embeddings WHERE vector IS NULL")
        return {**public_view(config), "progress": progress(conn, config.model)}

    @app.post("/api/settings/embedding/test", dependencies=[Depends(page_request)])
    def api_test_embedding() -> dict:
        config = load_config(settings.data_dir)
        if not config.base_url or not config.model:
            raise HTTPException(409, "先填好接口地址和模型名并保存")
        with slot(probe_busy, PROBE_SLOTS, "上一次测试还没结束"):
            return probe(config)

    caption_probe_busy = _Busy()
    add_caption_routes(
        app, settings, get_conn, page_request, lambda: slot(caption_probe_busy, PROBE_SLOTS, "上一次测试还没结束")
    )

    @app.get("/api/stats")
    def api_stats(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, int]:
        rows = conn.execute("SELECT kind, COUNT(*) AS n FROM files GROUP BY kind")
        return {r["kind"]: r["n"] for r in rows}

    def scan_state() -> dict:
        status = read_status(settings.data_dir)
        if status.running and not is_scan_running(settings.data_dir):
            # The kernel dropped the lock: the scanner died mid-scan (OOM, docker kill).
            status = replace(status, running=False, ok=False)
        return {
            "requested": is_requested(settings.data_dir),
            **asdict(status),
            "stopping": status.running and stop_requested(settings.data_dir),
            **timing(status, time.time()),
        }

    @app.get("/api/scan")
    def api_scan_status() -> dict:
        return scan_state()

    # No login anywhere, so anyone who can open the page can press this; it only wakes
    # the scanner early (one pending request, one scan at a time, a minimum gap between scans).
    @app.post("/api/scan", status_code=202, dependencies=[Depends(page_request)])
    def api_scan_request() -> dict:
        try:
            request_scan(settings.data_dir)
        except OSError as err:
            log.error("cannot record scan request: %r", err)
            raise HTTPException(503, "cannot record the scan request; check the data dir") from None
        return scan_state()

    # Takes effect between folders/archives/batches; a scan not started yet is dropped.
    @app.post("/api/scan/stop", dependencies=[Depends(page_request)])
    def api_scan_stop() -> dict:
        state = scan_state()
        if not (state["running"] or state["requested"]):
            raise HTTPException(409, "没有正在进行的扫描")
        try:
            request_scan_stop(settings.data_dir)
        except OSError as err:
            log.error("cannot record scan stop: %r", err)
            raise HTTPException(503, "没能发出停止请求，检查数据目录") from None
        return scan_state()

    def agent_search(conn: sqlite3.Connection, q: str, kind: str | None, limit: int, offset: int, mode: str):
        """Search for agents: "auto" tries semantic search and falls back to keywords."""
        if kind and kind not in KINDS:
            raise HTTPException(422, f"unknown kind {kind!r}; one of {', '.join(KINDS)}")
        note = None
        if mode != "text" and parse_terms(q):
            try:
                return semantic(conn, q, kind, limit, offset), "semantic", None
            except HTTPException as err:
                if mode == "semantic":
                    raise
                note = f"语义搜索不可用（{err.detail}），已改用关键词搜索"
        return search(conn, q, kind=kind, limit=limit, offset=offset), "text", note

    def meaning_ids(conn: sqlite3.Connection, q: str) -> list[int]:
        """Semantic search's file ids for `q`, best first. HTTPException if unavailable."""
        config = load_config(settings.data_dir)
        if not config.active:
            raise HTTPException(409, "语义搜索还没启用")
        with slot(semantic_busy, SEMANTIC_SLOTS, "语义搜索正忙，稍后再试"):
            try:
                # Many cards, then grouped: one unpacked pack's hundreds of cards must not fill the list.
                return ranked_ids(conn, vectors, config.model, query_vector(config, q), limit=PACK_MEANING_CARDS)
            except EmbedError as err:
                raise HTTPException(502, f"向量接口出错：{err}") from None
            except NoVectors:
                raise HTTPException(409, "还没有可用的向量") from None

    def agent_packs(conn: sqlite3.Connection, q: str, category: str, limit: int, offset: int, mode: str,
                    view: str = "", style: str = "", sort: str = ""):
        """Packs for agents: words first, then (unless mode is "text") what the meaning adds."""
        meaning, used, note = None, "text", None
        if mode != "text" and q.strip():  # no words: browsing, nothing to mean
            try:
                meaning, used = meaning_ids(conn, q), "text+semantic"
            except HTTPException as err:
                note = f"语义搜索不可用（{err.detail}），只用了关键词"
        return search_packs(conn, q, category, limit, offset, meaning, view, style, sort), used, note

    @app.get("/api/packs/facets")
    def api_pack_facets(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        """The folders and tags the pack page filters by, with their pack counts."""
        return pack_facets(conn)

    add_agent_routes(app, settings, get_conn, resolve, agent_search, scan_state, agent_packs)
    add_tag_routes(app, settings, page_request)
    add_update_routes(app, settings.data_dir, page_request)
    add_organize_routes(app, settings, get_conn, page_request)
    add_move_routes(app, settings, get_conn, page_request)
    add_fetch_routes(app, settings, get_conn, page_request)
    add_upload_routes(app, settings, get_conn, page_request)
    add_external_routes(app, settings, get_conn, page_request)

    @app.get("/api/files/{file_id}")
    def api_file(file_id: FileId, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        row = conn.execute(
            "SELECT f.*, c.text AS caption, c.error AS caption_error"
            " FROM files f LEFT JOIN captions c ON c.file_id = f.id WHERE f.id = ?",
            (file_id,),
        ).fetchone()
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
        if row["ext"] not in THUMBNAIL_EXTS and row["ext"] not in EMBEDDED_PREVIEW_EXTS:
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

    def grid_cells(conn: sqlite3.Connection, root: str, folder: str) -> list:
        """The folder's grid members' own thumbnails (cached for the cards too); files
        without one are skipped and remembered. A render slot is held per file only, so
        other cards' thumbnails are not kept waiting behind a whole grid."""
        cells = []
        for row in pack_grid.members(conn, root, folder):
            thumb = file_thumb_path(settings.thumb_dir, row["id"], row["mtime"])
            if not thumb.exists() and not recently_failed(thumb):
                try:
                    _, path = resolve(conn, row["id"])
                    with thumb_slots:
                        if not thumb.exists():
                            _render_or_415(_thumb_source(path, row["ext"], row["size"]), thumb)
                except Exception as err:  # HTTPException (415, 404), or a parser's own error
                    log.debug("no grid cell from file %d: %r", row["id"], err)
                    remember_failure(thumb)
            if thumb.exists():
                try:
                    cells.append(open_image(thumb, THUMB_SIZE))
                except Exception as err:  # a cached file damaged on disk: leave it out
                    log.warning("cannot open cached thumbnail %r: %r", str(thumb), err)
            if len(cells) >= pack_grid.GRID_CELLS:
                break
        return cells

    @app.get("/api/packs/grid")
    def api_pack_grid(
        root: str = Query(..., max_length=100),
        path: str = Query(..., min_length=1, max_length=MAX_PREFIX_LENGTH),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> FileResponse:
        """A folder pack's .uasset thumbnails as one picture (see pack_grid). The v= in
        its URL only keeps browsers from showing an older grid."""
        if root not in settings.roots:
            raise HTTPException(404, "asset root not configured")
        target = pack_grid.grid_path(settings.thumb_dir, root, path, pack_grid.summary(conn, root, path)[2])
        if not target.exists():
            if recently_failed(target):
                raise HTTPException(415, "no thumbnails in this folder")
            cells = grid_cells(conn, root, path)  # one cell: a full-size cover, like a lone image
            if not cells:
                remember_failure(target)
                raise HTTPException(415, "no thumbnails in this folder")
            compose_sheet(cells, target)  # atomic: two requests building it at once is harmless
        return _webp(target)

    @app.get("/api/fab-covers/status")
    def api_fab_status() -> dict:
        """The last Fab lookup after a scan: how many names, how many matched, its error."""
        return fab_covers.read_status(settings.data_dir)

    @app.get("/api/fab-covers/{name}")
    def api_fab_cover(name: str) -> FileResponse:
        if not fab_covers.is_cover_file(name):
            raise HTTPException(404, "no such cover")
        target = settings.data_dir / fab_covers.COVER_DIR / name
        if not target.is_file():
            raise HTTPException(404, "no such cover")
        return _webp(target)

    @app.get("/api/files/{file_id}/entries/{entry_id}/thumb")
    def api_entry_thumb(
        file_id: FileId,
        entry_id: FileId,
        size: Literal["", "large"] = "",
        conn: sqlite3.Connection = Depends(get_conn),
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
        target = entry_thumb_path(settings.thumb_dir, file_id, entry["mtime"], entry["size"], entry["path"], size)
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
                    _render_or_415(io.BytesIO(encoded), target, ENTRY_LARGE_BOX if size else ENTRY_THUMB_BOX)
                except (ExtractError, HTTPException) as err:
                    remember_failure(target)
                    raise HTTPException(415, "cannot extract entry") from err
        finally:
            extract_slots.release()
        return _webp(target)

    app.mount("/", _RevalidatedStaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
