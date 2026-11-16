"""Describe the pictures in one folder, on request: a long job (a few seconds per
picture on a local model), so it only ever runs when someone starts it from the web UI,
never as part of the scheduled scan.

The web server and the captioner (its own process/container) share only the data dir,
so, as for scans, they talk through files in it: caption.request (the scope, JSON),
caption.stop (empty) and caption.status (JSON progress), plus caption.lock, held while a
job runs.
"""

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from assetindex.caption import CaptionClient, CaptionConfig
from assetindex.config import Settings
from assetindex.embed_stage import _has_image, card_jpeg
from assetindex.embedding import EmbedError
from assetindex.scan_request import write_json_atomic

log = logging.getLogger(__name__)

REQUEST_NAME = "caption.request"
STOP_NAME = "caption.stop"
STATUS_NAME = "caption.status"
LOCK_NAME = "caption.lock"
CAPTION_BOX = (512, 512)  # enough detail to tell what is drawn; a contact sheet stays legible
MAX_CONSECUTIVE_FAILURES = 5  # every picture refused: the settings, not the pictures, are wrong
EMBED_EVERY = 20  # refresh text vectors this often, so search improves while the job runs
MAX_PREFIX_LENGTH = 500
MAX_ERROR_CHARS = 300
MAX_FOLDERS = 500


@dataclass(frozen=True)
class Scope:
    root: str
    prefix: str = ""  # a folder (or one file) under the root; "" = the whole root
    redo: bool = False  # describe again pictures that already have a description


@dataclass(frozen=True)
class CaptionStatus:
    running: bool = False
    root: str | None = None
    prefix: str | None = None
    total: int = 0
    done: int = 0
    failed: int = 0
    current: str | None = None
    started: float | None = None  # epoch seconds
    finished: float | None = None
    error: str | None = None  # why the job stopped early
    stopped: bool = False  # stopped from the web UI


def parse_scope(raw, roots) -> Scope:
    """A scope from the browser, validated: a configured root and a clean folder path."""
    if not isinstance(raw, dict) or set(raw) - {"root", "prefix", "redo"}:
        raise ValueError("范围格式不对")
    root, prefix, redo = raw.get("root"), raw.get("prefix", ""), raw.get("redo", False)
    if not isinstance(root, str) or root not in roots:
        raise ValueError("没有这个根目录")
    if not isinstance(prefix, str) or not isinstance(redo, bool):
        raise ValueError("范围格式不对")
    prefix = prefix.strip().replace("\\", "/")
    if prefix.startswith(f"{root}:"):  # pasted as the cards show paths: "nas:/2D/pack.zip"
        prefix = prefix[len(root) + 1 :]
    prefix = prefix.strip("/")
    if len(prefix) > MAX_PREFIX_LENGTH or any(not c.isprintable() for c in prefix):
        raise ValueError("文件夹路径不对")
    return Scope(root=root, prefix=prefix, redo=redo)


def _in_scope(scope: Scope) -> tuple[str, list]:
    """The folder itself as a file, or anything under it. substr() rather than LIKE, so
    "_" and "%" in folder names are plain characters."""
    if not scope.prefix:
        return "f.root = ?", [scope.root]
    folder = scope.prefix + "/"
    return "f.root = ? AND (f.rel_path = ? OR substr(f.rel_path, 1, ?) = ?)", [
        scope.root, scope.prefix, len(folder), folder,
    ]


def _cards(conn: sqlite3.Connection, scope: Scope) -> list[sqlite3.Row]:
    """Cards with a picture in the scope, each with whether it is already described."""
    where, params = _in_scope(scope)
    rows = conn.execute(
        f"""
        SELECT f.id, f.root, f.rel_path, f.ext, f.mtime, a.preview_state,
               (c.text IS NOT NULL AND c.mtime = f.mtime) AS described
        FROM files f
        LEFT JOIN archives a ON a.file_id = f.id
        LEFT JOIN captions c ON c.file_id = f.id
        WHERE {where} AND (f.seq_frames IS NULL OR f.seq_frames > 0)
        ORDER BY f.rel_path
        """,
        params,
    ).fetchall()
    return [r for r in rows if _has_image(r)]


def scope_counts(conn: sqlite3.Connection, scope: Scope) -> dict:
    cards = _cards(conn, scope)
    done = sum(1 for r in cards if r["described"])
    return {"cards": len(cards), "done": done, "todo": len(cards) if scope.redo else len(cards) - done}


def list_folders(conn: sqlite3.Connection, root: str, prefix: str) -> list[str]:
    """Folders directly inside `prefix` ("" = the root), for the scope picker."""
    folder = prefix + "/" if prefix else ""
    rows = conn.execute(
        """
        SELECT DISTINCT substr(rest, 1, instr(rest, '/') - 1) AS child FROM (
            SELECT substr(rel_path, ?) AS rest FROM files
            WHERE root = ? AND substr(rel_path, 1, ?) = ?
        ) WHERE instr(rest, '/') > 0 ORDER BY child LIMIT ?
        """,
        (len(folder) + 1, root, len(folder), folder, MAX_FOLDERS),
    )
    return [folder + r["child"] for r in rows]


def save_caption(conn: sqlite3.Connection, row: sqlite3.Row, model: str, text: str | None, error: str | None) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO captions (file_id, model, mtime, text, error, created) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET
                -- A failed redo keeps the description it already had (and its mtime).
                model = CASE WHEN excluded.text IS NULL THEN captions.model ELSE excluded.model END,
                mtime = CASE WHEN excluded.text IS NULL THEN captions.mtime ELSE excluded.mtime END,
                text = COALESCE(excluded.text, captions.text),
                created = CASE WHEN excluded.text IS NULL THEN captions.created ELSE excluded.created END,
                error = excluded.error
            """,
            (row["id"], model, row["mtime"], text, error[:MAX_ERROR_CHARS] if error else None, time.time()),
        )
        if text is not None:  # the card's text now includes the caption: embed it again
            conn.execute("DELETE FROM embeddings WHERE file_id = ? AND source = 'text'", (row["id"],))


def run_job(
    conn: sqlite3.Connection,
    settings: Settings,
    scope: Scope,
    config: CaptionConfig,
    client: CaptionClient | None = None,
    should_stop: Callable[[], bool] = lambda: False,
    report: Callable[[CaptionStatus], None] = lambda status: None,
    after_batch: Callable[[], None] = lambda: None,
) -> CaptionStatus:
    """Describe the pictures in scope one at a time, saving each as it comes, so a job
    that stops (button, crash, restart) loses at most one picture. `after_batch` runs
    every EMBED_EVERY captions and at the end (to refresh text vectors)."""
    client = client or CaptionClient(config)
    rows = [r for r in _cards(conn, scope) if scope.redo or not r["described"]]
    status = CaptionStatus(running=True, root=scope.root, prefix=scope.prefix, total=len(rows), started=time.time())
    report(status)
    consecutive = 0
    for row in rows:
        if should_stop():
            status = replace(status, stopped=True)
            break
        report(replace(status, current=row["rel_path"]))
        try:
            jpeg = card_jpeg(settings, row, CAPTION_BOX)
        except Exception as err:  # gone since the scan, undecodable: skip this card
            save_caption(conn, row, config.model, None, str(err) or type(err).__name__)
            status = replace(status, failed=status.failed + 1)
            continue
        try:
            text = client.describe(jpeg)
        except EmbedError as err:
            if not err.item_problem:  # down, timing out, wrong address or key: stop
                status = replace(status, error=f"接口出错，已停止：{err}")
                break
            save_caption(conn, row, config.model, None, str(err))
            status = replace(status, failed=status.failed + 1)
            consecutive += 1
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                status = replace(status, error=f"连续 {consecutive} 张都失败，已停止，检查设置：{err}")
                break
            continue
        save_caption(conn, row, config.model, text, None)
        consecutive = 0
        status = replace(status, done=status.done + 1)
        if status.done % EMBED_EVERY == 0:
            after_batch()
    if status.done:
        after_batch()
    return replace(status, running=False, current=None, finished=time.time())


def request_job(data_dir: Path, scope: Scope) -> None:
    (data_dir / STOP_NAME).unlink(missing_ok=True)
    write_json_atomic(data_dir / REQUEST_NAME, asdict(scope))


def take_job(data_dir: Path) -> dict | None:
    """The requested scope (unvalidated JSON), clearing the request and any stale stop."""
    path = data_dir / REQUEST_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raw = None
    path.unlink(missing_ok=True)
    (data_dir / STOP_NAME).unlink(missing_ok=True)
    return raw if isinstance(raw, dict) else None


def is_requested(data_dir: Path) -> bool:
    return (data_dir / REQUEST_NAME).exists()


def request_stop(data_dir: Path) -> None:
    (data_dir / REQUEST_NAME).unlink(missing_ok=True)  # one not started yet is dropped too
    (data_dir / STOP_NAME).touch()


def stop_requested(data_dir: Path) -> bool:
    return (data_dir / STOP_NAME).exists()


def write_status(data_dir: Path, status: CaptionStatus) -> None:
    write_json_atomic(data_dir / STATUS_NAME, asdict(status))


def read_status(data_dir: Path) -> CaptionStatus:
    """The last status written; a fresh one if none or unreadable."""
    try:
        raw = json.loads((data_dir / STATUS_NAME).read_text(encoding="utf-8"))
        return CaptionStatus(**raw) if isinstance(raw, dict) and isinstance(raw.get("running"), bool) else CaptionStatus()
    except (OSError, ValueError, TypeError):
        return CaptionStatus()
