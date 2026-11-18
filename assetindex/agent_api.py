"""Endpoints for AI agents (see skills/asset-store): compact search results with links,
asset details with archive contents, one file taken out of an archive, and what the
scanner and the captioner are doing.

The browser UI's own endpoints stay as they are; these return less, in a shape that is
cheap to read in a model's context, and every item says where to fetch it.
"""

import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi import Path as PathParam
from fastapi.responses import Response

from assetindex.archive_queries import list_entries
from assetindex.caption_web import caption_state
from assetindex.organize_web import organize_state
from assetindex.config import Settings
from assetindex.extract import ExtractError, find_bsdtar, read_member
from assetindex.kinds import LISTABLE_ARCHIVE_EXTS, THUMBNAIL_EXTS
from assetindex.search import MAX_QUERY_LENGTH, fetch_items, parse_terms
from assetindex.thumbs import EMBEDDED_PREVIEW_EXTS
from assetindex.updater import update_state

MAX_AGENT_LIMIT = 50  # results per page: enough to choose from, small enough for a context window
DETAIL_ENTRIES = 200
MAX_ENTRY_BYTES = 256 * 1024**2  # one member taken out of an archive, held in memory
ENTRY_TIMEOUT_SECONDS = 120
ENTRY_SLOTS = 2
ENTRY_SLOT_WAIT_SECONDS = 5
# bsdtar reads these; a .unitypackage stores assets under GUID folders, not their paths.
EXTRACTABLE_EXTS = LISTABLE_ARCHIVE_EXTS - {"unitypackage"}

Id = Annotated[int, PathParam(ge=1, le=2**63 - 1)]


def _modified(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _has_thumb(item: dict) -> bool:
    if item["ext"] in LISTABLE_ARCHIVE_EXTS:
        return item["sheet"] == 1
    return item["ext"] in THUMBNAIL_EXTS or item["ext"] in EMBEDDED_PREVIEW_EXTS


def agent_item(item: dict) -> dict:
    """A search result as an agent sees it."""
    links = {"detail": f"/api/agent/files/{item['id']}", "download": f"/api/files/{item['id']}/download"}
    if _has_thumb(item):
        links["thumb"] = f"/api/files/{item['id']}/thumb"
    archive = None
    if item["entry_count"] is not None:
        archive = {"entries": item["entry_count"], "engine": item["engine"], "engine_version": item["engine_version"]}
    return {
        "id": item["id"],
        "name": item["name"],
        "path": f"{item['root']}:/{item['rel_path']}",
        "kind": item["kind"],
        "ext": item["ext"],
        "size": item["size"],
        "modified": _modified(item["mtime"]),
        "caption": item["caption"],
        "frames": item["frames"] or None,  # an image sequence shown as one card
        "archive": archive,
        "matches_inside": item["hits"],
        "links": links,
    }


def _disposition(name: str) -> str:
    """Attachment header: an ASCII-only fallback (no quotes or backslashes, which would
    end the quoted string) plus the real name, UTF-8 encoded (RFC 6266 / 5987)."""
    fallback = "".join(c if c.isascii() and c.isprintable() and c not in '"\\' else "_" for c in name)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def _entry(file_id: int, archive_ext: str, entry: dict) -> dict:
    out = {"id": entry["id"], "path": entry["path"], "size": entry["size"]}
    if archive_ext in EXTRACTABLE_EXTS:
        out["download"] = f"/api/agent/files/{file_id}/entries/{entry['id']}/download"
    return out


def add_agent_routes(
    app: FastAPI,
    settings: Settings,
    get_conn: Callable,
    resolve: Callable,
    run_search: Callable,
    scan_state: Callable[[], dict],
    pack_search: Callable,
) -> None:
    entry_slots = threading.BoundedSemaphore(ENTRY_SLOTS)

    @app.get("/api/agent/packs")
    def agent_packs(
        q: str = Query("", max_length=MAX_QUERY_LENGTH),  # none: every pack, by path
        category: str = Query("", max_length=100),
        view: str = Query("", max_length=20),  # a view tag (俯视, 横版...): only packs tagged so
        style: str = Query("", max_length=20),  # a style tag (像素, 卡通...)
        mode: Literal["auto", "text"] = "auto",
        limit: int = Query(20, ge=1, le=MAX_AGENT_LIMIT),
        offset: int = Query(0, ge=0),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        """Packs (folders, archives with their previews), one result each, best first."""
        result, used, note = pack_search(conn, q, category.strip("/"), limit, offset, mode, view, style)
        return {"total": result.total, "mode": used, "note": note, "packs": result.packs}

    def item_or_404(conn: sqlite3.Connection, file_id: int) -> dict:
        items = fetch_items(conn, [file_id], [])
        if not items:
            raise HTTPException(404, "file not indexed")
        return items[0]

    @app.get("/api/agent/search")
    def agent_search(
        q: str = Query("", max_length=MAX_QUERY_LENGTH),
        kind: str | None = None,
        mode: Literal["auto", "text", "semantic"] = "auto",
        limit: int = Query(20, ge=1, le=MAX_AGENT_LIMIT),
        offset: int = Query(0, ge=0),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        result, used, note = run_search(conn, q, kind, limit, offset, mode)
        return {"total": result.total, "mode": used, "note": note, "items": [agent_item(i) for i in result.items]}

    @app.get("/api/agent/files/{file_id}")
    def agent_file(file_id: Id, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        item = agent_item(item_or_404(conn, file_id))
        if item["archive"]:
            total, entries = list_entries(conn, file_id, [], DETAIL_ENTRIES, 0)
            item["entries_total"] = total
            item["entries"] = [_entry(file_id, item["ext"], e) for e in entries]
        return item

    @app.get("/api/agent/files/{file_id}/entries")
    def agent_entries(
        file_id: Id,
        q: str = Query("", max_length=MAX_QUERY_LENGTH),
        limit: int = Query(DETAIL_ENTRIES, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        item = item_or_404(conn, file_id)
        total, entries = list_entries(conn, file_id, parse_terms(q), limit, offset)
        return {"total": total, "entries": [_entry(file_id, item["ext"], e) for e in entries]}

    @app.get("/api/agent/files/{file_id}/entries/{entry_id}/download")
    def agent_entry_download(
        file_id: Id, entry_id: Id, conn: sqlite3.Connection = Depends(get_conn)
    ) -> Response:
        entry = conn.execute(
            "SELECT e.path, e.size, f.ext FROM archive_entries e JOIN files f ON f.id = e.file_id"
            " WHERE e.id = ? AND e.file_id = ?",
            (entry_id, file_id),
        ).fetchone()
        if entry is None:
            raise HTTPException(404, "no such entry in this archive")
        if entry["ext"] not in EXTRACTABLE_EXTS:
            raise HTTPException(415, "files can't be taken out of this kind of archive; download the whole archive")
        if entry["size"] > MAX_ENTRY_BYTES:
            raise HTTPException(413, f"entry is larger than {MAX_ENTRY_BYTES} bytes; download the whole archive")
        name = PurePosixPath(entry["path"].replace("\\", "/")).name or "file"
        headers = {"Content-Disposition": _disposition(name)}
        if entry["size"] == 0:  # bsdtar output for an empty member is indistinguishable from a miss
            return Response(b"", media_type="application/octet-stream", headers=headers)
        tool = find_bsdtar()
        if tool is None:
            raise HTTPException(503, "bsdtar is not installed on the server")
        _, archive = resolve(conn, file_id)
        if not entry_slots.acquire(timeout=ENTRY_SLOT_WAIT_SECONDS):
            raise HTTPException(503, "busy extracting other files", headers={"Retry-After": "5"})
        try:
            data = read_member(archive, entry["path"], MAX_ENTRY_BYTES, tool, ENTRY_TIMEOUT_SECONDS)
        except ExtractError as err:
            # Includes names stored differently from the archive's raw bytes (GBK, backslashes).
            raise HTTPException(422, f"cannot extract this entry ({err}); download the whole archive instead") from None
        finally:
            entry_slots.release()
        return Response(data, media_type="application/octet-stream", headers=headers)

    @app.get("/api/agent/tasks")
    def agent_tasks() -> dict:
        return {
            "scan": scan_state(),
            "caption": caption_state(settings.data_dir),
            "organize": organize_state(settings.data_dir),
            "update": update_state(settings.data_dir),
        }
