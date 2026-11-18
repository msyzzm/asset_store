"""Pack tags for search (2026-11-17): the view a pack suits (top-down, side-on...), its
art style and themes, asked of the vision model with the same facts and picture the
organizer uses. Kept in their own table, apart from the organizer's decisions; search by
pack shows them. The view is a hint, never a filter: a 3D scene often suits any view.

Runs in the captioner (the model's one user at a time), started from the web with the
packs to tag: tried on a few first, then on all.
"""

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Annotated, Protocol

from fastapi import Body, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from assetindex import classify
from assetindex.config import Settings
from assetindex.caption_job import LOCK_NAME  # the model's lock, which organizing holds too
from assetindex.embedding import EmbedError
from assetindex.scan_request import write_json_atomic
from assetindex.scanlock import is_scan_running

log = logging.getLogger(__name__)

VIEWS = ("俯视", "等距", "横版", "第一人称", "第三人称", "不限")
STYLES = ("写实", "卡通", "像素", "低多边形", "风格化", "手绘", "其他")
MAX_THEMES = 5
MAX_THEME_CHARS = 12
ANSWER_TOKENS = 200
ANSWER_SHOWN = 80  # chars of an answer without JSON kept in its error: why it went wrong
MAX_PATHS = 5000
MAX_ERROR_CHARS = 300
REQUEST_NAME = "tags.request"
STATUS_NAME = "tags.status"
STOP_NAME = "tags.stop"


@dataclass(frozen=True)
class Tags:
    view: str
    style: str
    themes: tuple[str, ...]


@dataclass(frozen=True)
class TagStatus:
    running: bool = False
    root: str | None = None
    total: int = 0
    done: int = 0
    failed: int = 0
    current: str | None = None
    stopped: bool = False
    error: str | None = None
    last_error: str | None = None  # the latest pack that failed, and why
    started: float | None = None
    finished: float | None = None


class Asker(Protocol):
    def ask(self, prompt: str, jpeg: bytes | None, max_tokens: int) -> str: ...


def build_prompt(facts: str) -> str:
    return (
        "你在给一个游戏美术素材库打标签。下面是一个素材包的信息，附图是包里几张图拼成的预览（可能没有）。\n"
        f"视角：它最适合哪种游戏视角，只能选一个：{'、'.join(VIEWS)}。"
        "3D 场景、道具、角色多数各种视角都能用，看不出明确限制就填「不限」；"
        "2D 图块、精灵图要看画法：从正上方或斜上方画的是俯视，侧面画的是横版，45 度菱形格是等距。\n"
        f"画风：只能选一个：{'、'.join(STYLES)}。\n"
        f"题材：1 到 {MAX_THEMES} 个词，用顿号分隔，比如「蒸汽朋克、维多利亚、城市」「中世纪、地牢」「科幻、太空站」。\n\n"
        '只输出一个 JSON，不要别的文字：{"view": "视角", "style": "画风", "themes": "题材"}\n\n'
        f"素材包信息：\n{facts}"
    )


def parse_tags(text: str) -> Tags:
    """The model's JSON answer, checked; ValueError if there is none."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("回答里没有 JSON")
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        raise ValueError("回答里的 JSON 不完整") from None
    if not isinstance(data, dict):
        raise ValueError("回答不是 JSON 对象")
    view, style, themes = data.get("view"), data.get("style"), data.get("themes")
    words = themes.replace(",", "、").replace("，", "、").split("、") if isinstance(themes, str) else []
    words = [w.strip()[:MAX_THEME_CHARS] for w in words if w.strip()]
    return Tags(
        view=view if view in VIEWS else "不限",
        style=style if style in STYLES else "其他",
        themes=tuple(dict.fromkeys(words))[:MAX_THEMES],
    )


def save_tags(conn: sqlite3.Connection, root: str, path: str, tags: Tags, model: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO pack_tags (root, path, view, style, themes, model, created) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (root, path) DO UPDATE SET view = excluded.view, style = excluded.style,"
            " themes = excluded.themes, model = excluded.model, created = excluded.created",
            (root, path, tags.view, tags.style, "、".join(tags.themes), model, time.time()),
        )


def tags_of(conn: sqlite3.Connection, root: str, path: str) -> dict | None:
    row = conn.execute("SELECT view, style, themes FROM pack_tags WHERE root = ? AND path = ?", (root, path)).fetchone()
    if row is None:
        return None
    return {"view": row[0], "style": row[1], "themes": [t for t in (row[2] or "").split("、") if t]}


def _is_dir(conn: sqlite3.Connection, root: str, path: str) -> bool | None:
    """True for a folder pack, False for a file pack, None if `path` is no pack: the
    organizer's units decide (with the user's splits and joins); a pack it has not seen
    yet is told by the index, as long as it is a file or a folder of files."""
    row = conn.execute("SELECT is_dir FROM organize_units WHERE root = ? AND path = ?", (root, path)).fetchone()
    if row is not None:
        return bool(row[0])
    if conn.execute("SELECT 1 FROM organize_units WHERE root = ? AND path LIKE ? || '/%' LIMIT 1",
                    (root, path)).fetchone():
        return None  # a folder holding packs, not a pack
    if conn.execute("SELECT 1 FROM files WHERE root = ? AND rel_path = ?", (root, path)).fetchone():
        return False
    low, high = classify.under(path)
    found = conn.execute(
        "SELECT 1 FROM files WHERE root = ? AND rel_path >= ? AND rel_path < ? LIMIT 1", (root, low, high)
    ).fetchone()
    return True if found else None


def _tag_one(conn: sqlite3.Connection, settings: Settings, root: str, path: str, client: Asker, model: str) -> None:
    """Raises ValueError (this pack) or EmbedError (the model)."""
    is_dir = _is_dir(conn, root, path)
    if is_dir is None:
        raise ValueError("索引里没有这个包（可能已移走，等扫描更新）")
    prompt = build_prompt(classify.digest(conn, root, path, is_dir))
    jpeg = classify.picture(conn, settings, root, path, is_dir)
    save_tags(conn, root, path, parse_tags(client.ask(prompt, jpeg, ANSWER_TOKENS)), model)


@dataclass(frozen=True)
class Patience:
    """As the organizer (caption settings): API errors on one pack are retried after a wait;
    so many failures in a row, of any kind, stop the job."""
    retries: int = 3
    retry_wait: float = 10
    max_failures: int = 100


def _attempt(conn, settings, root, path, client, model, patience: Patience, sleep) -> None:
    """One pack, its API errors retried. Raises what finally failed."""
    for attempt in range(patience.retries + 1):
        try:
            _tag_one(conn, settings, root, path, client, model)
            return
        except EmbedError as err:
            if err.item_problem or attempt == patience.retries:
                raise
            log.warning("model API error on %r (try %d): %s", path, attempt + 1, err)
            sleep(patience.retry_wait)


def run_tags(
    conn: sqlite3.Connection, settings: Settings, root: str, paths: list[str], client: Asker, model: str,
    should_stop: Callable[[], bool] = lambda: False, report: Callable[[TagStatus], None] = lambda status: None,
    patience: Patience = Patience(), sleep: Callable[[float], None] = time.sleep,
) -> TagStatus:
    """Tag the packs one by one. A pack that fails (a bad answer, API errors on every try,
    anything odd about it) fails alone; `patience.max_failures` in a row stop the job, and
    the tags so far are kept."""
    status = TagStatus(running=True, root=root, total=len(paths), started=time.time())
    in_a_row = 0
    for path in paths:
        if should_stop():
            status = replace(status, stopped=True)
            break
        report(replace(status, current=path))
        try:
            _attempt(conn, settings, root, path, client, model, patience, sleep)
            status, in_a_row = replace(status, done=status.done + 1), 0
        except Exception as err:  # this pack only: a broken one must not end a nine-hour job
            log.warning("tags for %r: %r", path, err)
            in_a_row += 1
            status = replace(status, failed=status.failed + 1, last_error=f"{path}：{err}"[:MAX_ERROR_CHARS])
            if in_a_row >= patience.max_failures:
                status = replace(status, error=f"连续 {in_a_row} 个都失败，已停止：{err}"[:MAX_ERROR_CHARS])
                break
    return replace(status, running=False, current=None, finished=time.time())


# ---- request / stop / status files ---------------------------------------------------


def request_job(data_dir: Path, root: str, paths: list[str]) -> None:
    (data_dir / STOP_NAME).unlink(missing_ok=True)
    write_json_atomic(data_dir / REQUEST_NAME, {"root": root, "paths": paths})


def take_job(data_dir: Path) -> dict | None:
    path = data_dir / REQUEST_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raw = None
    path.unlink(missing_ok=True)
    return raw if isinstance(raw, dict) else None


def is_requested(data_dir: Path) -> bool:
    return (data_dir / REQUEST_NAME).exists()


def stop_requested(data_dir: Path) -> bool:
    return (data_dir / STOP_NAME).exists()


def write_status(data_dir: Path, status: TagStatus) -> None:
    write_json_atomic(data_dir / STATUS_NAME, asdict(status))


def read_status(data_dir: Path) -> dict:
    """The job's status; "running" left by a captioner that was killed says so."""
    base = asdict(TagStatus())
    try:
        raw = json.loads((data_dir / STATUS_NAME).read_text(encoding="utf-8"))
        base.update({k: v for k, v in raw.items() if k in base} if isinstance(raw, dict) else {})
    except (OSError, ValueError):
        pass
    if base["running"] and not is_scan_running(data_dir, LOCK_NAME):
        base.update(running=False, error=base["error"] or "中途断了（captioner 被停止或重启），可以重新开始")
    return {**base, "requested": is_requested(data_dir)}


class TagRequest(BaseModel):
    root: str
    paths: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(min_length=1, max_length=MAX_PATHS)


def add_tag_routes(app: FastAPI, settings: Settings, page_request: Callable) -> None:
    @app.get("/api/organize/tags")
    def api_tag_status() -> dict:
        return read_status(settings.data_dir)

    @app.post("/api/organize/tags", status_code=202, dependencies=[Depends(page_request)])
    def api_tag(body: Annotated[TagRequest, Body()]) -> dict:
        if body.root not in settings.roots:
            raise HTTPException(422, "没有这个根目录")
        if any(p.startswith("/") or ".." in p.split("/") for p in body.paths):
            raise HTTPException(422, "路径要是根目录下的相对路径")
        request_job(settings.data_dir, body.root, body.paths)
        return read_status(settings.data_dir)

    @app.post("/api/organize/tags/stop", dependencies=[Depends(page_request)])
    def api_tag_stop() -> dict:
        (settings.data_dir / REQUEST_NAME).unlink(missing_ok=True)
        (settings.data_dir / STOP_NAME).touch()
        return read_status(settings.data_dir)
