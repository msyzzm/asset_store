"""Web API for the organize page: start/stop the analysis, review and decide proposals.

Nothing here moves files: a decision only changes the proposal's status (and target).
"""

import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import PurePosixPath
from typing import Annotated, Literal

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from assetindex import caption_job, organize
from assetindex.caption import load_config
from assetindex.classify import CATEGORIES, DUPLICATES, UNSORTED, file_suffix, under
from assetindex.config import Settings
from assetindex.kinds import LISTABLE_ARCHIVE_EXTS, THUMBNAIL_EXTS
from assetindex.scanlock import is_scan_running
from assetindex.thumbs import EMBEDDED_PREVIEW_EXTS
from assetindex.units import companions_by_unit, is_container_name

log = logging.getLogger(__name__)

MAX_PAGE = 200
MAX_BATCH = 500
MAX_PATH = 1000
_UNSAFE_PART = re.compile(r'[\\:*?"<>|\x00-\x1f\x7f]')
Show = Literal["proposed", "duplicates", "accepted", "rejected", "todo", "failed", "all"]
_SHOW_SQL = {
    "proposed": "status = 'proposed' AND dup_of IS NULL",
    "duplicates": "dup_of IS NOT NULL AND status NOT IN ('accepted', 'rejected')",
    "accepted": "status = 'accepted'",
    "rejected": "status = 'rejected'",
    "todo": "status = 'todo' AND error IS NULL",
    "failed": "status = 'todo' AND error IS NOT NULL",
    "all": "1 = 1",
}
_DECIDE_SQL = {
    "accept": "UPDATE organize_units SET status = 'accepted' WHERE root = ? AND path = ? AND target IS NOT NULL",
    "reject": "UPDATE organize_units SET status = 'rejected' WHERE root = ? AND path = ? AND target IS NOT NULL",
    "reset": "UPDATE organize_units SET status = CASE WHEN target IS NULL THEN 'todo' ELSE 'proposed' END"
    " WHERE root = ? AND path = ?",
    # Ask the model again (after the scheme or the prompt changed): not a decided pack or a duplicate.
    "redo": "UPDATE organize_units SET status = 'todo', category = NULL, name = NULL, target = NULL, reason = NULL,"
    " error = NULL WHERE root = ? AND path = ? AND status != 'accepted' AND dup_of IS NULL",
}


def organize_state(data_dir) -> dict:
    status = organize.read_status(data_dir)
    if status.running and not is_scan_running(data_dir, organize.LOCK_NAME):
        status = replace(status, running=False, error=status.error or "中途断了（captioner 被停止或重启），可以重新开始")
    return {"requested": organize.is_requested(data_dir), **asdict(status)}


class Decide(BaseModel):
    root: str
    paths: list[str] = Field(max_length=MAX_BATCH)
    action: Literal["accept", "reject", "reset", "redo"]


class AcceptAll(BaseModel):
    """Accept every pack the list shows for one category (and its search text)."""

    root: str
    show: Literal["proposed", "duplicates"]
    category: str = Field(min_length=1, max_length=100)
    q: str = Field("", max_length=200)

    @field_validator("category")
    @classmethod
    def _not_unsorted(cls, value: str) -> str:
        if value == UNSORTED:
            raise ValueError("模型没能归类的包要一个个看，不能整批确认")
        return value


def _unit_filter(root: str, show: str, category: str, q: str) -> tuple[str, list]:
    """The list's WHERE clause, shared by the list and by accepting it whole."""
    where, params = ["root = ?", _SHOW_SQL[show]], [root]
    if category:
        where.append("category = ?")
        params.append(category)
    if q.strip():
        where.append("(instr(lower(path), lower(?)) > 0 OR instr(lower(coalesce(target, '')), lower(?)) > 0)")
        params += [q.strip(), q.strip()]
    return " AND ".join(where), params


class Retarget(BaseModel):
    root: str
    path: str = Field(max_length=MAX_PATH)
    target: str = Field(max_length=MAX_PATH)


class UnitRef(BaseModel):
    root: str
    path: str = Field(max_length=MAX_PATH)


def check_target(target: str) -> tuple[str, str]:
    """A typed-in target: "<category>/<name>" with a known category and a safe name."""
    target = target.strip().replace("\\", "/").strip("/")
    category, _, name = target.rpartition("/")
    if category not in {c for c, _ in CATEGORIES} | {UNSORTED, DUPLICATES}:
        raise ValueError("目标要以分类开头，例如 01_2D/UI/包名")
    if not name or name in (".", "..") or _UNSAFE_PART.search(name) or name != name.strip(" ."):
        raise ValueError("名字里不能有 \\ : * ? \" < > | 这些字符，也不能以点或空格开头结尾")
    return category, name


PICTURE_LIMIT = 24  # pictures shown for one pack in the large view
RAW_PICTURE_MAX_BYTES = 20 * 2**20  # bigger ones are shown by their thumbnail: a huge PNG can stall the page
_PICTURE_EXTS = sorted(THUMBNAIL_EXTS | EMBEDDED_PREVIEW_EXTS | LISTABLE_ARCHIVE_EXTS)
_BROWSER_IMAGE_EXTS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp"})  # shown as they are


def _showable(row: sqlite3.Row) -> bool:
    if row["ext"] in LISTABLE_ARCHIVE_EXTS:
        return row["preview_state"] == 1  # its contact sheet
    return row["ext"] in THUMBNAIL_EXTS or row["ext"] in EMBEDDED_PREVIEW_EXTS


def _pictures(conn: sqlite3.Connection, root: str, path: str, is_dir: bool, companions: list[str]) -> list[dict]:
    """The pack's pictures for the large view: the file and its previews, or the folder's
    pictures (and archives with a contact sheet), at most PICTURE_LIMIT."""
    select = (
        "SELECT f.id, f.rel_path, f.ext, f.size, a.preview_state FROM files f LEFT JOIN archives a ON a.file_id = f.id"
        f" WHERE f.root = ? AND (f.seq_frames IS NULL OR f.seq_frames > 0) AND f.ext IN ({','.join('?' * len(_PICTURE_EXTS))})"
    )
    if is_dir:
        rows = list(conn.execute(
            f"{select} AND f.rel_path >= ? AND f.rel_path < ? ORDER BY f.rel_path LIMIT ?",
            (root, *_PICTURE_EXTS, *under(path), PICTURE_LIMIT * 4),  # some archives may have no sheet
        ))
        base = path + "/"
    else:
        members = [path, *companions]
        rows = conn.execute(f"{select} AND f.rel_path IN ({','.join('?' * len(members))})", (root, *_PICTURE_EXTS, *members))
        rows = sorted(rows, key=lambda r: members.index(r["rel_path"]))
        base = path.rpartition("/")[0] + "/" if "/" in path else ""
    pictures = []
    for row in rows:
        if not _showable(row):
            continue
        url = f"/api/files/{row['id']}"
        pictures.append({
            "id": row["id"], "name": row["rel_path"].removeprefix(base),
            "large": f"{url}/raw" if row["ext"] in _BROWSER_IMAGE_EXTS and row["size"] <= RAW_PICTURE_MAX_BYTES
            else f"{url}/thumb",
            "thumb": f"{url}/thumb",
        })
        if len(pictures) >= PICTURE_LIMIT:
            break
    return pictures


def _thumb_id(conn: sqlite3.Connection, root: str, path: str, is_dir: bool, companions: dict[str, list[str]]) -> int | None:
    """A file whose thumbnail stands for the unit (`companions`: file unit -> its companions)."""
    if not is_dir:  # the file's own picture, else a preview next to it
        for member in [path, *companions.get(path, [])]:
            row = conn.execute(
                "SELECT f.id, f.ext, a.preview_state FROM files f LEFT JOIN archives a ON a.file_id = f.id"
                " WHERE f.root = ? AND f.rel_path = ?",
                (root, member),
            ).fetchone()
            if row is None:
                continue
            if row["ext"] in LISTABLE_ARCHIVE_EXTS:
                if row["preview_state"] == 1:
                    return row["id"]
            elif row["ext"] in THUMBNAIL_EXTS or row["ext"] in EMBEDDED_PREVIEW_EXTS:
                return row["id"]
        return None
    exts = sorted(THUMBNAIL_EXTS)
    row = conn.execute(
        f"SELECT id FROM files WHERE root = ? AND rel_path >= ? AND rel_path < ? AND ext IN ({','.join('?' * len(exts))})"
        " ORDER BY rel_path LIMIT 1",
        (root, *under(path), *exts),
    ).fetchone()
    return row["id"] if row else None


def _keep_folders_whole(conn: sqlite3.Connection, root: str, paths: list[str]) -> None:
    """A folder pack someone decided on is one pack from now on, even if the decision is
    undone later: undoing it must not let the rules split it (a join, as the user would)."""
    conn.executemany(
        "INSERT OR IGNORE INTO unit_joins (root, path)"
        " SELECT root, path FROM organize_units WHERE root = ? AND path = ? AND is_dir = 1",
        [(root, p) for p in paths],
    )


def _counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for r in conn.execute(
        "SELECT root, status, dup_of IS NOT NULL AS dup, error IS NOT NULL AS failed, COUNT(*) AS n"
        " FROM organize_units GROUP BY 1, 2, 3, 4"
    ):
        c = counts.setdefault(r["root"], dict.fromkeys(_SHOW_SQL, 0))
        c["all"] += r["n"]
        if r["dup"] and r["status"] not in organize.DECIDED:
            c["duplicates"] += r["n"]
        elif r["status"] == "todo":
            c["failed" if r["failed"] else "todo"] += r["n"]
        else:
            c[r["status"]] += r["n"]
    return counts


def add_organize_routes(app: FastAPI, settings: Settings, get_conn: Callable, page_request: Callable) -> None:
    data_dir = settings.data_dir
    guarded = [Depends(page_request)]

    def root_or_422(root: str) -> str:
        if root not in settings.roots:
            raise HTTPException(422, "没有这个根目录")
        return root

    def unit_or_404(conn: sqlite3.Connection, root: str, path: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM organize_units WHERE root = ? AND path = ?", (root, path)).fetchone()
        if row is None:
            raise HTTPException(404, "没有这个包（可能已经变了，重新分析一下）")
        return row

    @app.get("/api/organize")
    def api_organize(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return {
            "job": organize_state(data_dir),
            "report": organize.read_report(data_dir),
            "counts": _counts(conn),
            "roots": sorted(settings.roots),
            "categories": [{"path": p, "description": d} for p, d in CATEGORIES],
            "model_ready": load_config(data_dir).active,
        }

    @app.get("/api/organize/units")
    def api_units(
        root: str,
        show: Show = "proposed",
        category: str = Query("", max_length=100),
        q: str = Query("", max_length=200),
        limit: int = Query(50, ge=1, le=MAX_PAGE),
        offset: int = Query(0, ge=0),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        root_or_422(root)
        clause, params = _unit_filter(root, show, category, q)
        total = conn.execute(f"SELECT COUNT(*) FROM organize_units WHERE {clause}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM organize_units WHERE {clause} ORDER BY path LIMIT ? OFFSET ?", (*params, limit, offset)
        ).fetchall()
        items = []
        companions = {}
        if any(not r["is_dir"] for r in rows):  # once per page, not per row
            companions = companions_by_unit(*organize.stored_layout(conn, root))
        for r in rows:
            thumb = _thumb_id(conn, root, r["path"], bool(r["is_dir"]), companions)
            items.append({
                "path": r["path"], "is_dir": bool(r["is_dir"]), "files": r["files"], "size": r["size"],
                "category": r["category"], "name": r["name"], "target": r["target"], "reason": r["reason"],
                "status": r["status"], "dup_of": r["dup_of"], "error": r["error"],
                "thumb": f"/api/files/{thumb}/thumb" if thumb else None,
            })
        return {"total": total, "items": items}

    @app.get("/api/organize/units/pictures")
    def api_unit_pictures(
        root: str, path: str = Query(max_length=MAX_PATH), conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        root_or_422(root)
        unit = conn.execute("SELECT is_dir FROM organize_units WHERE root = ? AND path = ?", (root, path)).fetchone()
        if unit is None:
            raise HTTPException(404, "没有这个包，可能已经变了，刷新一下")
        companions = [] if unit["is_dir"] else companions_by_unit(*organize.stored_layout(conn, root)).get(path, [])
        return {"pictures": _pictures(conn, root, path, bool(unit["is_dir"]), companions)}

    @app.post("/api/organize/units/decide", dependencies=guarded)
    def api_decide(body: Annotated[Decide, Body()], conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        root_or_422(body.root)
        with conn:
            changed = sum(conn.execute(_DECIDE_SQL[body.action], (body.root, p)).rowcount for p in body.paths)
            if body.action in ("accept", "reject"):
                _keep_folders_whole(conn, body.root, body.paths)
        return {"changed": changed}

    @app.post("/api/organize/units/accept-all", dependencies=guarded)
    def api_accept_all(body: Annotated[AcceptAll, Body()], conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        root_or_422(body.root)
        clause, params = _unit_filter(body.root, body.show, body.category, body.q)
        with conn:  # one statement: a pack decided meanwhile no longer matches the list's filter
            rows = conn.execute(
                f"UPDATE organize_units SET status = 'accepted' WHERE {clause} AND target IS NOT NULL RETURNING path",
                params,
            ).fetchall()
            paths = [r["path"] for r in rows]
            _keep_folders_whole(conn, body.root, paths)
        return {"changed": len(paths)}

    @app.post("/api/organize/units/target", dependencies=guarded)
    def api_retarget(body: Annotated[Retarget, Body()], conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        root_or_422(body.root)
        row = unit_or_404(conn, body.root, body.path)
        try:
            category, name = check_target(body.target)
        except ValueError as err:
            raise HTTPException(422, str(err)) from None
        suffix = file_suffix(PurePosixPath(body.path).name)
        if not row["is_dir"] and suffix and not name.lower().endswith(suffix.lower()):
            name += suffix  # renaming "pack.zip" to "图标包" must not lose the ".zip"
        target = f"{category}/{name}"
        if target != row["target"] and target.lower() in organize._taken(conn, body.root, body.path):
            raise HTTPException(409, f"「{target}」已经有了，或者被别的包占用")
        with conn:
            conn.execute(
                "UPDATE organize_units SET category = ?, name = ?, target = ?, status = 'accepted' WHERE root = ? AND path = ?",
                (category, name, target, body.root, body.path),
            )
            _keep_folders_whole(conn, body.root, [body.path])
        return {"target": target}

    @app.post("/api/organize/units/keep", dependencies=guarded)
    def api_keep(body: Annotated[UnitRef, Body()], conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        """Keep this copy of a duplicate instead of the one chosen. The choice is stored, so
        later analyses keep it too; this copy takes over the other's proposed place."""
        root_or_422(body.root)
        row = unit_or_404(conn, body.root, body.path)
        kept = row["dup_of"]
        if not kept:
            raise HTTPException(409, "这个包不是重复的副本")
        old = unit_or_404(conn, body.root, kept)
        for decided in (old, row):
            if decided["status"] in organize.DECIDED:
                raise HTTPException(409, f"「{decided['path']}」已经确认或拒绝过，先撤回那个决定")
        with conn:
            conn.execute("DELETE FROM dup_keep WHERE root = ? AND path = ?", (body.root, kept))
            conn.execute("INSERT OR IGNORE INTO dup_keep (root, path) VALUES (?, ?)", (body.root, body.path))
        organize.apply_duplicates(conn, body.root, organize.current_units(conn, body.root))
        if old["target"] and old["category"] != DUPLICATES:  # same content: same place
            organize.save_proposal(conn, body.root, body.path, old["category"], old["name"], old["reason"] or "", old["model"] or "")
        return {"kept": body.path}

    @app.post("/api/organize/split", dependencies=guarded)
    def api_split(body: Annotated[UnitRef, Body()], conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        root_or_422(body.root)
        if not unit_or_404(conn, body.root, body.path)["is_dir"]:
            raise HTTPException(409, "单个文件不能再拆")
        with conn:
            conn.execute("DELETE FROM unit_joins WHERE root = ? AND path = ?", (body.root, body.path))
            conn.execute("INSERT OR IGNORE INTO unit_splits (root, path) VALUES (?, ?)", (body.root, body.path))
        return {"units": len(organize.refresh_units(conn, body.root))}

    @app.post("/api/organize/join", dependencies=guarded)
    def api_join(body: Annotated[UnitRef, Body()], conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        """Keep a folder as one pack: undoes the default rule and any split inside it."""
        root_or_422(body.root)
        if not body.path or ("/" not in body.path and is_container_name(body.path)):
            raise HTTPException(409, "这是放分类的顶层文件夹（比如 UE、Unity），不能合并成一个包")
        low, high = under(body.path)
        if conn.execute(
            "SELECT 1 FROM files WHERE root = ? AND rel_path >= ? AND rel_path < ? LIMIT 1", (body.root, low, high)
        ).fetchone() is None:
            raise HTTPException(404, "没有这个文件夹")
        if conn.execute(
            "SELECT 1 FROM organize_units WHERE root = ? AND path >= ? AND path < ? AND status IN ('accepted', 'rejected') LIMIT 1",
            (body.root, low, high),
        ).fetchone():
            raise HTTPException(409, "里面有已经确认或拒绝的包，先撤回那些决定")
        with conn:
            conn.execute(
                "DELETE FROM unit_splits WHERE root = ? AND (path = ? OR (path >= ? AND path < ?))",
                (body.root, body.path, low, high),
            )
            conn.execute("INSERT OR IGNORE INTO unit_joins (root, path) VALUES (?, ?)", (body.root, body.path))
        return {"units": len(organize.refresh_units(conn, body.root))}

    @app.post("/api/organize/unsplit", dependencies=guarded)
    def api_unsplit(body: Annotated[UnitRef, Body()], conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        """Undo a split, and any split made inside it (it would come back on a re-split)."""
        root_or_422(body.root)
        with conn:
            conn.execute(
                "DELETE FROM unit_splits WHERE root = ? AND (path = ? OR (path >= ? AND path < ?))",
                (body.root, body.path, *under(body.path)),
            )
        return {"units": len(organize.refresh_units(conn, body.root))}

    @app.get("/api/organize/splits")
    def api_splits(root: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        root_or_422(root)
        return {"splits": [r[0] for r in conn.execute("SELECT path FROM unit_splits WHERE root = ? ORDER BY path", (root,))]}

    @app.post("/api/organize", status_code=202, dependencies=guarded)
    def api_start(raw: Annotated[dict, Body()]) -> dict:
        try:
            scope = caption_job.parse_scope(raw, settings.roots)
        except ValueError as err:
            raise HTTPException(422, str(err)) from None
        if not load_config(data_dir).active:
            raise HTTPException(409, "整理要用看图模型：先在「看图描述」里设置好接口")
        state = organize_state(data_dir)
        if state["running"] or state["requested"]:
            raise HTTPException(409, "已经有一个整理分析在跑")
        try:
            organize.request_job(data_dir, scope)
        except OSError as err:
            log.error("cannot record organize request: %r", err)
            raise HTTPException(503, "没能提交，检查数据目录") from None
        return organize_state(data_dir)

    @app.post("/api/organize/stop", dependencies=guarded)
    def api_stop() -> dict:
        organize.request_stop(data_dir)
        return organize_state(data_dir)
