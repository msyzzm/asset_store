"""Routes for the 外部素材 page: search Poly Haven, ambientCG, OpenGameArt and itch.io, look at one asset,
and queue one of its downloads. The server asks the site what to download (the page
only names the asset and the option), so the queue only ever gets the site's own links."""

import logging
import re
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated

from fastapi import Body, Depends, FastAPI, HTTPException, Query

from assetindex import downloads, ext_ambientcg, ext_itch, ext_opengameart, ext_polyhaven, external
from assetindex.config import Settings
from assetindex.fetch_web import writable_root

log = logging.getLogger(__name__)

SOURCES = {m.NAME: m for m in (ext_polyhaven, ext_ambientcg, ext_opengameart, ext_itch)}
MAX_QUERY = 200
MAX_OFFSET = 10_000
_ASSET_ID = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_pool = ThreadPoolExecutor(max_workers=len(SOURCES) * 2, thread_name_prefix="external")


def _in_library(conn: sqlite3.Connection, asset_id: str) -> str | None:
    """A file in the index whose path holds the asset's id (how both sites name files)."""
    if len(asset_id) < 3:  # the trigram index needs three characters
        return None
    row = conn.execute(
        "SELECT f.root, f.rel_path FROM files_fts JOIN files f ON f.id = files_fts.rowid "
        "WHERE files_fts.rel_path MATCH ? LIMIT 1",
        ('"' + asset_id.replace('"', '""') + '"',),
    ).fetchone()
    return f"{row['root']}:/{row['rel_path']}" if row else None


def _marked(conn: sqlite3.Connection, source: str, items: list[dict]) -> list[dict]:
    wanted = downloads.wanted(conn, source, [i["id"] for i in items])
    matches = SOURCES[source].MATCHES_LIBRARY
    return [{**i, "in_library": _in_library(conn, i["id"]) if matches else None, "downloaded": i["id"] in wanted}
            for i in items]


def _search_one(module, q: str, offset: int) -> dict:
    """One site's results; its failure is its own, never the other site's."""
    head = {"label": module.LABEL, "credit": module.CREDIT, "license": module.LICENSE}
    try:
        return {**head, "error": None, **module.search(q, offset)}
    except external.SourceError as err:
        return {**head, "error": str(err), "items": [], "total": 0}
    except Exception:  # an answer shaped unlike what we know
        log.exception("search on %s failed", module.NAME)
        return {**head, "error": "网站返回的内容看不懂（接口可能改了）", "items": [], "total": 0}


def _source(name: str):
    module = SOURCES.get(name)
    if module is None:
        raise HTTPException(404, "没有这个网站")
    return module


def _check_id(asset_id: str) -> str:
    if not _ASSET_ID.fullmatch(asset_id):
        raise HTTPException(422, "素材编号不对")
    return asset_id


def _ask(module, call, *args):
    """A detail or an option from the site, its failures as HTTP errors."""
    try:
        return call(*args)
    except KeyError:
        raise HTTPException(404, "网站上没有这个素材或规格") from None
    except external.SourceError as err:
        raise HTTPException(502, str(err)) from None
    except Exception:  # an answer shaped unlike what we know
        log.exception("%s on %s failed", call.__name__, module.NAME)
        raise HTTPException(502, "网站返回的内容看不懂（接口可能改了）") from None


def add_external_routes(app: FastAPI, settings: Settings, get_conn: Callable, page_request: Callable) -> None:
    guarded = [Depends(page_request)]

    @app.get("/api/external/search")
    def api_search(
        q: Annotated[str, Query(max_length=MAX_QUERY)] = "",
        source: str = "",
        offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        modules = [_source(source)] if source else list(SOURCES.values())
        futures = {m.NAME: _pool.submit(_search_one, m, q.strip(), offset) for m in modules}
        results = {name: f.result() for name, f in futures.items()}  # each bounded by its own timeouts
        return {
            "query": q,
            "chinese": external.has_cjk(q),
            "sources": {name: {**r, "items": _marked(conn, name, r["items"])} for name, r in results.items()},
        }

    @app.get("/api/external/{source}/{asset_id}")
    def api_detail(source: str, asset_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        module = _source(source)
        found = _ask(module, module.detail, _check_id(asset_id))
        return _marked(conn, source, [found])[0]

    @app.post("/api/external/{source}/{asset_id}/download", status_code=201, dependencies=guarded)
    def api_download(source: str, asset_id: str, body: Annotated[dict, Body()],
                     conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        module = _source(source)
        key = body.get("option") if isinstance(body, dict) else None
        if not isinstance(key, str) or not key or set(body) - {"option", "root"}:
            raise HTTPException(422, "要选一个下载规格")
        root = writable_root(settings, body.get("root"))
        found = _ask(module, module.detail, _check_id(asset_id))
        if not any(o["key"] == key and o["ready"] for o in found["options"]):
            raise HTTPException(422, "这个素材没有这个下载规格")
        chosen = _ask(module, module.option, asset_id, key)
        title = f"{found['title']} {chosen['label']}"
        try:
            if "parts" in chosen:  # several files: one folder named after the asset and option
                download_id = downloads.add_bundle(conn, root, found["page"], title, chosen["parts"],
                                                   source=source, source_id=asset_id)
            else:
                download_id = downloads.add(conn, root, chosen["url"], title=title, source=source,
                                            source_id=asset_id, md5=chosen["md5"], size=chosen["size"])
        except ValueError as err:
            raise HTTPException(502, f"网站给的下载链接不对：{err}") from None
        return downloads.get(conn, download_id)
