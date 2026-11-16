"""Organize the library: find duplicate packs and propose where every pack belongs.

An analysis, started from the web UI, runs in the captioner process (it uses the same
vision model, so it shares the caption lock: one model job at a time):
1. units: split each root into packs (assetindex/units.py) and remember them; a pack
   whose files changed loses its proposal;
2. hashing: files that may have a twin (assetindex/dupes.py);
3. duplicates: identical packs; all copies but one are proposed for _Duplicates/;
4. classifying: the model proposes a category and a name for each pack still without one.

Nothing is moved here: proposals are reviewed (accepted, edited, rejected) on the
organize page. Like the caption job, it talks to the web server through files in the
data dir: organize.request, organize.stop, organize.status, organize.report.json.
"""

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath

from assetindex import caption_job
from assetindex.caption import CaptionClient, CaptionConfig
from assetindex.caption_job import Scope
from assetindex.classify import CATEGORY_PATHS, DUPLICATES, classify_unit, target_for
from assetindex.config import Settings
from assetindex.dupes import UnitFile, duplicate_units, file_duplicates, hash_pending, signature, unit_files
from assetindex.embedding import EmbedError
from assetindex.scan_request import write_json_atomic
from assetindex.units import opened_folders

log = logging.getLogger(__name__)

REQUEST_NAME = "organize.request"
STOP_NAME = "organize.stop"
STATUS_NAME = "organize.status"
REPORT_NAME = "organize.report.json"
LOCK_NAME = caption_job.LOCK_NAME  # the same model, one job at a time
MAX_CONSECUTIVE_FAILURES = 5
MAX_ERROR_CHARS = 300
STATUSES = ("todo", "proposed", "accepted", "rejected")
DECIDED = ("accepted", "rejected")


@dataclass(frozen=True)
class OrganizeStatus:
    running: bool = False
    root: str | None = None
    prefix: str | None = None
    phase: str | None = None  # units | hashing | classifying
    total: int = 0
    done: int = 0
    failed: int = 0
    current: str | None = None
    started: float | None = None
    finished: float | None = None
    error: str | None = None
    stopped: bool = False


# ---- units and proposals -------------------------------------------------------------


def _splits(conn: sqlite3.Connection, root: str) -> list[str]:
    return [r[0] for r in conn.execute("SELECT path FROM unit_splits WHERE root = ?", (root,))]


def current_units(conn: sqlite3.Connection, root: str) -> dict[tuple[str, bool], list[UnitFile]]:
    paths = [r[0] for r in conn.execute("SELECT rel_path FROM files WHERE root = ?", (root,))]
    return unit_files(conn, root, opened_folders(paths, _splits(conn, root)))


def refresh_units(conn: sqlite3.Connection, root: str) -> dict[tuple[str, bool], list[UnitFile]]:
    """Bring organize_units in line with the index: new packs, gone packs, and packs
    whose files changed (their proposal no longer fits, decided or not)."""
    units = current_units(conn, root)
    known = {r["path"]: r["signature"] for r in conn.execute("SELECT path, signature FROM organize_units WHERE root = ?", (root,))}
    now = time.time()
    with conn:
        for (path, is_dir), files in units.items():
            sig = signature(files)
            size = sum(f.size for f in files)
            if path not in known:
                conn.execute(
                    "INSERT INTO organize_units (root, path, is_dir, files, size, signature, updated) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (root, path, int(is_dir), len(files), size, sig, now),
                )
            elif known[path] != sig:
                conn.execute(
                    """
                    UPDATE organize_units SET is_dir = ?, files = ?, size = ?, signature = ?, dup_of = NULL,
                        category = NULL, name = NULL, target = NULL, reason = NULL, error = NULL,
                        status = 'todo', updated = ?
                    WHERE root = ? AND path = ?
                    """,
                    (int(is_dir), len(files), size, sig, now, root, path),
                )
        gone = set(known) - {path for path, _ in units}
        conn.executemany("DELETE FROM organize_units WHERE root = ? AND path = ?", [(root, p) for p in gone])
        conn.executemany("DELETE FROM dup_keep WHERE root = ? AND path = ?", [(root, p) for p in gone])
        changed = [p for (p, _), files in units.items() if p in known and known[p] != signature(files)]
        _forget_twins(conn, root, [*gone, *changed], now)
    return units


def _forget_twins(conn: sqlite3.Connection, root: str, keepers: list[str], now: float) -> None:
    """Copies marked as duplicates of a pack that is gone or changed are duplicates of
    nothing now. Even a decided one is reset: it could be the only copy left."""
    reset = """
        UPDATE organize_units SET dup_of = NULL, category = NULL, name = NULL, target = NULL, reason = NULL,
            status = 'todo', updated = ?
        WHERE root = ? AND dup_of IS NOT NULL AND {}
    """
    conn.execute(reset.format("dup_of NOT IN (SELECT path FROM organize_units WHERE root = ?)"), (now, root, root))
    conn.executemany(reset.format("dup_of = ?"), [(now, root, keeper) for keeper in keepers])


def _taken(conn: sqlite3.Connection, root: str, exclude: str | None = None) -> set[str]:
    """Paths a proposal must not take: what exists now, and other proposals' targets."""
    rows = conn.execute("SELECT path, target FROM organize_units WHERE root = ?", (root,))
    taken = set()
    for r in rows:
        if r["path"] != exclude:
            taken.add(r["path"].lower())
            if r["target"]:
                taken.add(r["target"].lower())
    return taken


def apply_duplicates(conn: sqlite3.Connection, root: str, units: dict) -> list:
    """Mark the extra copies of identical packs; packs no longer duplicated go back to
    being classified. A decision the user already made is left alone."""
    preferred = [r[0] for r in conn.execute("SELECT path FROM dup_keep WHERE root = ?", (root,))]
    groups = duplicate_units(units, preferred)
    now = time.time()
    with conn:
        conn.execute(
            """
            UPDATE organize_units SET dup_of = NULL, category = NULL, name = NULL, target = NULL, reason = NULL,
                status = 'todo', updated = ?
            WHERE root = ? AND dup_of IS NOT NULL AND status NOT IN ('accepted', 'rejected')
            """,
            (now, root),
        )
        for group in groups:
            for other in group.others:
                row = conn.execute(
                    "SELECT status, is_dir FROM organize_units WHERE root = ? AND path = ?", (root, other)
                ).fetchone()
                if row is None or row["status"] in DECIDED:
                    continue
                name = PurePosixPath(other).name
                how = "内容完全相同" if group.exact else "文件名和大小完全相同"
                conn.execute(
                    """
                    UPDATE organize_units SET dup_of = ?, category = ?, name = ?, target = ?, reason = ?,
                        status = 'proposed', error = NULL, updated = ?
                    WHERE root = ? AND path = ?
                    """,
                    (group.keep, DUPLICATES, name, target_for(DUPLICATES, name, bool(row["is_dir"]), _taken(conn, root, other)),
                     f"和「{group.keep}」{how}，保留那一份", now, root, other),
                )
    return groups


def save_proposal(conn: sqlite3.Connection, root: str, path: str, category: str, name: str, reason: str, model: str) -> None:
    row = conn.execute("SELECT is_dir FROM organize_units WHERE root = ? AND path = ?", (root, path)).fetchone()
    if row is None:
        return
    with conn:
        target = target_for(category, name, bool(row["is_dir"]), _taken(conn, root, path))
        conn.execute(
            """
            UPDATE organize_units SET category = ?, name = ?, target = ?, reason = ?, model = ?, error = NULL,
                status = 'proposed', updated = ?
            WHERE root = ? AND path = ? AND status NOT IN ('accepted', 'rejected')
            """,
            (category, name, target, reason, model, time.time(), root, path),
        )


def _save_error(conn: sqlite3.Connection, root: str, path: str, error: str) -> None:
    with conn:
        conn.execute(
            "UPDATE organize_units SET error = ?, updated = ? WHERE root = ? AND path = ?",
            (error[:MAX_ERROR_CHARS], time.time(), root, path),
        )


def _in_scope(scope: Scope) -> tuple[str, list]:
    if not scope.prefix:
        return "root = ?", [scope.root]
    folder = scope.prefix + "/"
    return "root = ? AND (path = ? OR substr(path, 1, ?) = ?)", [scope.root, scope.prefix, len(folder), folder]


def to_classify(conn: sqlite3.Connection, scope: Scope) -> list[sqlite3.Row]:
    """Packs in scope that need the model: no proposal yet (or, with redo, an undecided one)."""
    where, params = _in_scope(scope)
    statuses = "('todo', 'proposed')" if scope.redo else "('todo')"
    return conn.execute(
        f"SELECT path, is_dir FROM organize_units WHERE {where} AND dup_of IS NULL AND status IN {statuses} ORDER BY path",
        params,
    ).fetchall()


# ---- the analysis job ----------------------------------------------------------------


def run_analysis(
    conn: sqlite3.Connection,
    settings: Settings,
    scope: Scope,
    config: CaptionConfig,
    client: CaptionClient | None = None,
    should_stop: Callable[[], bool] = lambda: False,
    report: Callable[[OrganizeStatus], None] = lambda status: None,
) -> OrganizeStatus:
    client = client or CaptionClient(config)
    status = OrganizeStatus(running=True, root=scope.root, prefix=scope.prefix, phase="units", started=time.time())
    report(status)
    refresh_units(conn, scope.root)

    status = replace(status, phase="hashing")
    report(status)
    hashed = hash_pending(conn, settings, scope.root, should_stop,
                          lambda done, total: report(replace(status, done=done, total=total)))
    if hashed.stopped:
        return replace(status, running=False, stopped=True, finished=time.time())
    units = current_units(conn, scope.root)
    groups = apply_duplicates(conn, scope.root, units)
    write_report(settings.data_dir, scope.root, groups, units)

    rows = to_classify(conn, scope)
    status = replace(status, phase="classifying", total=len(rows), done=0)
    report(status)
    consecutive = 0
    for row in rows:
        if should_stop():
            status = replace(status, stopped=True)
            break
        report(replace(status, current=row["path"]))
        try:
            proposal = classify_unit(client, conn, settings, scope.root, row["path"], bool(row["is_dir"]))
        except EmbedError as err:
            if not err.item_problem:
                status = replace(status, error=f"接口出错，已停止：{err}")
                break
            _save_error(conn, scope.root, row["path"], str(err))
            status = replace(status, failed=status.failed + 1)
            consecutive += 1
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                status = replace(status, error=f"连续 {consecutive} 个都失败，已停止，检查设置：{err}")
                break
            continue
        except Exception as err:  # an odd pack (unreadable picture...) must not end the job
            log.exception("classifying %r failed", row["path"])
            _save_error(conn, scope.root, row["path"], f"{type(err).__name__}: {err}")
            status = replace(status, failed=status.failed + 1)
            continue
        save_proposal(conn, scope.root, row["path"], proposal.category, proposal.name, proposal.reason, config.model)
        consecutive = 0
        status = replace(status, done=status.done + 1)
    return replace(status, running=False, current=None, finished=time.time())


def write_report(data_dir: Path, root: str, groups: list, units: dict) -> None:
    """Duplicate figures for the page, computed once per analysis (they need every file)."""
    skip = {p for g in groups for p in (g.keep, *g.others)}
    files = file_duplicates(units, skip)
    report = read_report(data_dir)
    report[root] = {
        "units": len(units),
        "dup_groups": len(groups),
        "dup_copies": sum(len(g.others) for g in groups),
        "dup_wasted": sum(g.wasted for g in groups),
        "files": files,
        "updated": time.time(),
    }
    write_json_atomic(data_dir / REPORT_NAME, report)


def read_report(data_dir: Path) -> dict:
    try:
        report = json.loads((data_dir / REPORT_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return report if isinstance(report, dict) else {}


# ---- request / stop / status files ---------------------------------------------------


def request_job(data_dir: Path, scope: Scope) -> None:
    (data_dir / STOP_NAME).unlink(missing_ok=True)
    write_json_atomic(data_dir / REQUEST_NAME, asdict(scope))


def take_job(data_dir: Path) -> dict | None:
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
    (data_dir / REQUEST_NAME).unlink(missing_ok=True)
    (data_dir / STOP_NAME).touch()


def stop_requested(data_dir: Path) -> bool:
    return (data_dir / STOP_NAME).exists()


def write_status(data_dir: Path, status: OrganizeStatus) -> None:
    write_json_atomic(data_dir / STATUS_NAME, asdict(status))


def read_status(data_dir: Path) -> OrganizeStatus:
    try:
        raw = json.loads((data_dir / STATUS_NAME).read_text(encoding="utf-8"))
        return OrganizeStatus(**raw) if isinstance(raw, dict) and isinstance(raw.get("running"), bool) else OrganizeStatus()
    except (OSError, ValueError, TypeError):
        return OrganizeStatus()


def valid_category(category: str) -> bool:
    return category in CATEGORY_PATHS or category == DUPLICATES
