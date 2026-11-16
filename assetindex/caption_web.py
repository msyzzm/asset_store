"""Web API for describing pictures: settings, scope picker, start/stop, progress.

The job itself runs in the captioner process (see caption_job.py); these endpoints only
read the index and drop request files for it.
"""

import contextlib
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, replace
from typing import Annotated

from fastapi import Body, Depends, FastAPI, HTTPException, Query

from assetindex import caption_job
from assetindex.caption import apply_update, load_config, probe, public_view, save_config
from assetindex.config import Settings
from assetindex.scanlock import is_scan_running

log = logging.getLogger(__name__)

MAX_PREFIX_QUERY = caption_job.MAX_PREFIX_LENGTH


def caption_state(data_dir) -> dict:
    """The describe-pictures job as the UI shows it: requested, running, last result."""
    status = caption_job.read_status(data_dir)
    if status.running and not is_scan_running(data_dir, caption_job.LOCK_NAME):
        # The captioner died mid-job (restart, OOM); nothing will finish it.
        status = replace(status, running=False, error=status.error or "中途断了（captioner 被停止或重启），可以重新开始")
    return {"requested": caption_job.is_requested(data_dir), **asdict(status)}


def add_caption_routes(
    app: FastAPI,
    settings: Settings,
    get_conn: Callable,
    page_request: Callable,
    probe_slot: Callable[[], contextlib.AbstractContextManager],
) -> None:
    data_dir = settings.data_dir
    guarded = [Depends(page_request)]

    def view() -> dict:
        return {**public_view(load_config(data_dir)), "roots": sorted(settings.roots)}

    def scope_from(root: str, prefix: str, redo: bool = False) -> caption_job.Scope:
        try:
            return caption_job.parse_scope({"root": root, "prefix": prefix, "redo": redo}, settings.roots)
        except ValueError as err:
            raise HTTPException(422, str(err)) from None

    def job_state() -> dict:
        return caption_state(data_dir)

    @app.get("/api/settings/caption")
    def api_caption_settings() -> dict:
        return view()

    @app.put("/api/settings/caption", dependencies=guarded)
    def api_save_caption_settings(update: Annotated[dict, Body()]) -> dict:
        try:
            config = apply_update(load_config(data_dir), update)
        except ValueError as err:
            raise HTTPException(422, str(err)) from None
        try:
            save_config(data_dir, config)
        except OSError as err:
            log.error("cannot save caption settings: %r", err)
            raise HTTPException(503, "设置保存失败，检查数据目录") from None
        return view()

    @app.post("/api/settings/caption/test", dependencies=guarded)
    def api_test_caption() -> dict:
        config = load_config(data_dir)
        if not config.active:
            raise HTTPException(409, "先填好接口地址和模型名并保存")
        with probe_slot():
            return probe(config)

    @app.get("/api/captions/folders")
    def api_caption_folders(
        root: str, prefix: str = Query("", max_length=MAX_PREFIX_QUERY), conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        scope = scope_from(root, prefix)
        return {"folders": caption_job.list_folders(conn, scope.root, scope.prefix)}

    @app.get("/api/captions/scope")
    def api_caption_scope(
        root: str,
        prefix: str = Query("", max_length=MAX_PREFIX_QUERY),
        redo: bool = False,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        return caption_job.scope_counts(conn, scope_from(root, prefix, redo))

    @app.get("/api/captions")
    def api_caption_status() -> dict:
        return job_state()

    # No login: anyone who can open the page can start a job. It only describes pictures
    # already in the index, one job at a time, and can be stopped by anyone the same way.
    @app.post("/api/captions", status_code=202, dependencies=guarded)
    def api_start_captions(raw: Annotated[dict, Body()]) -> dict:
        try:
            scope = caption_job.parse_scope(raw, settings.roots)
        except ValueError as err:
            raise HTTPException(422, str(err)) from None
        if not load_config(data_dir).active:
            raise HTTPException(409, "先在上面设置好看图描述的接口")
        state = job_state()
        if state["running"] or state["requested"]:
            raise HTTPException(409, "已经有一个任务在跑，先停止它")
        try:
            caption_job.request_job(data_dir, scope)
        except OSError as err:
            log.error("cannot record caption request: %r", err)
            raise HTTPException(503, "任务没能提交，检查数据目录") from None
        return job_state()

    @app.post("/api/captions/stop", dependencies=guarded)
    def api_stop_captions() -> dict:
        try:
            caption_job.request_stop(data_dir)
        except OSError as err:
            log.error("cannot record caption stop: %r", err)
            raise HTTPException(503, "没能发出停止请求，检查数据目录") from None
        return job_state()
