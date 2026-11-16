"""Routes for carrying out confirmed proposals: the move plan, starting a move (or an
undo, or settling a run a crash cut short), its progress, and the history of runs.
The moving itself happens in the mover service (assetindex/move_job.py)."""

import sqlite3
from collections.abc import Callable
from dataclasses import asdict, replace
from typing import Annotated

from fastapi import Body, Depends, FastAPI, HTTPException

from assetindex import move_job, organize, organize_apply
from assetindex.config import Settings
from assetindex.organize_move import build_plan
from assetindex.scanlock import is_scan_running

PLAN_SAMPLE = 200  # moves listed on the page; the count covers all


def move_state(data_dir) -> dict:
    status = move_job.read_status(data_dir)
    if status.running and not is_scan_running(data_dir):  # the mover holds the scan lock while it runs
        status = replace(status, running=False, error=status.error or "中途断了（mover 被停止或重启）：再点一次会先收尾")
    requested = move_job.is_requested(data_dir)
    busy = is_scan_running(data_dir) or is_scan_running(data_dir, organize.LOCK_NAME)
    return {"requested": requested, "waiting": requested and busy, **asdict(status)}


def add_move_routes(app: FastAPI, settings: Settings, get_conn: Callable, page_request: Callable) -> None:
    data_dir = settings.data_dir
    guarded = [Depends(page_request)]

    @app.get("/api/organize/moves")
    def api_moves() -> dict:
        return {
            "job": move_state(data_dir),
            "mount": move_job.read_mount(data_dir),
            "history": move_job.history(data_dir),
            "unsettled": len(organize_apply.unsettled(data_dir)),
        }

    @app.get("/api/organize/moves/plan")
    def api_plan(root: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        if root not in settings.roots:
            raise HTTPException(422, "没有这个根目录")
        plan = build_plan(conn, root)
        return {
            "moves": len(plan.moves),
            "files": sum(len(m.renames) for m in plan.moves),
            "problems": [{"path": p.path, "reason": p.reason} for p in plan.problems],
            "sample": [
                {"path": m.path, "target": m.new_path, "renames": [[r.src, r.dst, r.is_dir] for r in m.renames]}
                for m in plan.moves[:PLAN_SAMPLE]
            ],
        }

    @app.post("/api/organize/moves", status_code=202, dependencies=guarded)
    def api_start(raw: Annotated[dict, Body()]) -> dict:
        try:
            action, root, _ = move_job.parse(raw, settings)
        except ValueError as err:
            raise HTTPException(422, str(err)) from None
        state = move_state(data_dir)
        if state["running"] or state["requested"]:
            raise HTTPException(409, "已经有一个移动任务了")
        mount = move_job.read_mount(data_dir)
        if mount is None:
            raise HTTPException(409, "mover 服务还没运行：更新一次（docker compose up -d）后会自动启动")
        if action != "recover" and not mount.get(root):
            raise HTTPException(409, "NAS 现在对 mover 是只读的：在 .env 里设 ASSET_MOVER_ACCESS=rw，再 docker compose up -d")
        move_job.request(data_dir, raw)
        return move_state(data_dir)

    @app.post("/api/organize/moves/stop", dependencies=guarded)
    def api_stop() -> dict:
        state = move_state(data_dir)
        if not (state["running"] or state["requested"]):
            raise HTTPException(409, "没有在移动")
        move_job.request_stop(data_dir)
        return move_state(data_dir)
