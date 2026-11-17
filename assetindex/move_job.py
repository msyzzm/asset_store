"""Move jobs, on request from the web UI: carry out the confirmed proposals, undo a run,
or settle a run a crash cut short.

They run in the "mover" service, the only one that may get the NAS mounted writable
(ASSET_MOVER_ACCESS=rw in .env); everything else mounts it read-only. As for scans,
the web server and the mover share only the data dir: move.request (JSON), move.stop,
move.status (JSON). A job holds the scan lock and the model lock: no scan or analysis
changes the index while files move.
"""

import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path

from assetindex import organize, organize_apply, purge, repair, scan_request, unpack, verify
from assetindex.units import companions_by_unit, stored_layout
from assetindex.config import Settings
from assetindex.organize_move import build_plan
from assetindex.scan_request import write_json_atomic

log = logging.getLogger(__name__)

REQUEST_NAME = "move.request"
STOP_NAME = "move.stop"
STATUS_NAME = "move.status"
LOCK_NAME = "move.lock"  # held by the mover for a whole job: "running" means this lock is held
ACTIONS = ("apply", "undo", "recover", "unpack", "verify", "purge", "repair")
WITH_PATHS = ("verify", "repair")  # these need `paths`; unpack may take them
MAX_VERIFY = 200  # each archive gets a line in the status (MAX_PROBLEMS)
MAX_PASSWORD = 200
MAX_UNPACK_LIMIT = 100_000
MAX_PROBLEMS = 200  # shown on the page; the journal has every detail
MAX_HISTORY = 50
MOUNT_STALE_SECONDS = 300  # the mover refreshes move.mount every ~90 s; older: it is not running
_JOURNAL_NAME = re.compile(r"[0-9]{8}-[0-9]{6}\.jsonl")


@dataclass(frozen=True)
class MoveStatus:
    running: bool = False
    action: str | None = None
    root: str | None = None
    journal: str | None = None  # file name in data/moves
    total: int = 0
    done: int = 0
    moved: int = 0  # packs moved, or put back for an undo
    problems: list[dict] = field(default_factory=list)  # {"path", "reason"}
    started: float | None = None
    finished: float | None = None
    error: str | None = None
    stopped: bool = False
    writable: bool | None = None  # the NAS as the mover sees it; None: not known yet


def writable(path: Path) -> bool:
    """Whether the mount at `path` is writable, without writing anything."""
    try:
        return not os.statvfs(path).f_flag & os.ST_RDONLY
    except (AttributeError, OSError):  # no statvfs (Windows): ask the OS instead
        return os.access(path, os.W_OK)


MOUNT_NAME = "move.mount"


def write_mount(data_dir: Path, roots: dict[str, bool]) -> None:
    """What the mover sees at startup: which roots it may write. The web page shows it
    before anyone presses "move" (ASSET_MOVER_ACCESS=rw in .env)."""
    write_json_atomic(data_dir / MOUNT_NAME, {"roots": roots, "at": time.time()})


def read_mount(data_dir: Path, now: float | None = None) -> dict[str, bool] | None:
    """Which roots the mover may write; None if no mover runs (not deployed, stopped)."""
    try:
        raw = json.loads((data_dir / MOUNT_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    roots = raw.get("roots") if isinstance(raw, dict) else None
    at = raw.get("at") if isinstance(raw, dict) else None
    if not isinstance(roots, dict) or not isinstance(at, (int, float)):
        return None
    if (time.time() if now is None else now) - at > MOUNT_STALE_SECONDS:
        return None
    return {k: v for k, v in roots.items() if isinstance(v, bool)}


def request(data_dir: Path, raw: dict) -> None:
    (data_dir / STOP_NAME).unlink(missing_ok=True)
    write_json_atomic(data_dir / REQUEST_NAME, raw)


def take(data_dir: Path) -> dict | None:
    """The requested job, marked running before the request goes, so the page never
    sees a moment with neither (and accepts a second job). A stop pressed meanwhile
    stays: request() clears stale ones."""
    path = data_dir / REQUEST_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raw = None
    action = raw.get("action") if isinstance(raw, dict) and raw.get("action") in ACTIONS else None
    write_status(data_dir, MoveStatus(running=True, action=action, started=time.time()))
    path.unlink(missing_ok=True)
    return raw if isinstance(raw, dict) else None


def is_requested(data_dir: Path) -> bool:
    return (data_dir / REQUEST_NAME).exists()


def request_stop(data_dir: Path) -> None:
    (data_dir / REQUEST_NAME).unlink(missing_ok=True)
    (data_dir / STOP_NAME).touch()


def stop_requested(data_dir: Path) -> bool:
    return (data_dir / STOP_NAME).exists()


def write_status(data_dir: Path, status: MoveStatus) -> None:
    write_json_atomic(data_dir / STATUS_NAME, asdict(status))


def read_status(data_dir: Path) -> MoveStatus:
    try:
        raw = json.loads((data_dir / STATUS_NAME).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("running"), bool):
            return MoveStatus(**raw)
    except (OSError, ValueError, TypeError):
        pass
    return MoveStatus()


def parse(raw, settings: Settings) -> tuple[str, str, str | None]:
    """(action, root, journal name) from a request, validated."""
    if not isinstance(raw, dict) or set(raw) - {"action", "root", "journal", "password", "limit", "paths"}:
        raise ValueError("请求格式不对")
    action, root, journal = raw.get("action"), raw.get("root"), raw.get("journal")
    password = raw.get("password")
    if action == "unpack" and not (isinstance(password, str) and 0 < len(password) <= MAX_PASSWORD):
        raise ValueError("要填解压密码")
    limit = raw.get("limit")
    if action != "unpack" and (password is not None or limit is not None):
        raise ValueError("请求格式不对")
    if limit is not None and (type(limit) is not int or not 0 < limit <= MAX_UNPACK_LIMIT):
        raise ValueError("要解开几个？")
    paths = raw.get("paths")
    if (action in WITH_PATHS and paths is None) or (action not in (*WITH_PATHS, "unpack") and paths is not None):
        raise ValueError("请求格式不对")
    if paths is not None and not _relative_paths(paths):
        raise ValueError(f"要处理哪些文件？（1 到 {MAX_VERIFY} 个根目录下的路径）")
    if action == "repair" and len(paths) < 2:
        raise ValueError("修复要两份以上的副本")
    if action not in ACTIONS:
        raise ValueError("不认识的操作")
    if not isinstance(root, str) or root not in settings.roots:
        raise ValueError("没有这个根目录")
    if action == "undo":
        if not isinstance(journal, str) or not _JOURNAL_NAME.fullmatch(journal):
            raise ValueError("要撤销哪一次？")
        path = settings.data_dir / organize_apply.JOURNAL_DIR / journal
        if not path.is_file():
            raise ValueError("没有这次移动的记录")
        if organize_apply.journal_root(path) != root:
            raise ValueError("这次移动不是在这个根目录里做的")
    elif journal is not None:
        raise ValueError("请求格式不对")
    return action, root, journal


def _relative_paths(paths) -> bool:
    return (
        isinstance(paths, list) and 0 < len(paths) <= MAX_VERIFY
        and all(isinstance(p, str) and p and not p.startswith("/") and ".." not in p.split("/") for p in paths)
    )


def history(data_dir: Path) -> list[dict]:
    """Past runs, newest first: what each moved and whether it can still be undone."""
    recent = list(reversed(organize_apply.journals(data_dir)))[:MAX_HISTORY]
    return [{"journal": j.name, **organize_apply.summary(j)} for j in recent]


def _problems(items) -> list[dict]:
    return [{"path": p.path, "reason": p.reason} for p in items][:MAX_PROBLEMS]


def run(
    conn: sqlite3.Connection,
    settings: Settings,
    raw,
    should_stop: Callable[[], bool] = lambda: False,
    report: Callable[[MoveStatus], None] = lambda status: None,
    move: Callable[[str, str, bool], None] = organize_apply.move_no_replace,
    now: Callable[[], float] = time.time,
    free_bytes: Callable[[Path], int] = unpack.free_bytes,
    test_archive: Callable[[Path], str | None] = verify.test_archive,
    remove: Callable[[str], None] = purge.remove,
) -> MoveStatus:
    """One job; the caller holds the locks."""
    try:
        action, root, journal_name = parse(raw, settings)
    except ValueError as err:
        return MoveStatus(error=str(err), finished=now())
    base = settings.roots[root].resolve()
    status = MoveStatus(running=True, action=action, root=root, started=now(), writable=writable(base))
    report(status)
    left = []  # first, whatever a crash left open, in every run
    for journal in organize_apply.unsettled(settings.data_dir):
        journal_root = organize_apply.journal_root(journal)
        if journal_root not in settings.roots:  # unknown or unreadable: never guess which root it was
            log.warning("move journal %s is for root %r, not configured: left as it is", journal.name, journal_root)
            continue
        left += organize_apply.recover(conn, settings.roots[journal_root].resolve(), journal_root, journal, move=move)
    if left:
        return replace(status, running=False, finished=now(), problems=_problems(left),
                       error="上次移动中断，还有没能收尾的包，先处理这些（见移动记录）")
    if action == "recover":
        organize.refresh_units(conn, root)
        return replace(status, running=False, finished=now())
    if action == "purge":
        return _purge(settings, status, should_stop, report, remove, now)
    if action == "verify":
        return _verify(settings, raw["paths"], status, should_stop, report, test_archive, now)
    if action == "unpack":
        return _unpack(conn, settings, raw, status, should_stop, report, move, free_bytes, now)
    if action == "repair":
        return _repair(conn, settings, raw["paths"], status, should_stop, report, move, free_bytes, now)
    journals = settings.data_dir / organize_apply.JOURNAL_DIR
    progress = {"done": 0}

    def step(done: int, total: int) -> None:
        progress["done"] = done
        report(replace(status, done=done, total=total))

    if action == "undo":
        journal = journals / journal_name
        status = replace(status, journal=journal_name)
        result = organize_apply.undo(conn, base, root, journal, move=move, should_stop=should_stop, report=step)
    else:
        plan = build_plan(conn, root)
        journal = journals / f"{datetime.fromtimestamp(now()).strftime('%Y%m%d-%H%M%S')}.jsonl"
        status = replace(status, total=len(plan.moves), journal=journal.name, problems=_problems(plan.problems))
        report(status)
        result = organize_apply.apply_plan(conn, base, root, plan, journal, should_stop, step, move=move)
    organize.refresh_units(conn, root)
    return replace(
        status, running=False, finished=now(), moved=result.moved,
        done=progress["done"] if result.stopped else status.total,
        problems=(status.problems + _problems(result.problems))[:MAX_PROBLEMS],
        error=result.error, stopped=result.stopped,
    )


def _unpack(conn, settings: Settings, raw: dict, status: MoveStatus, should_stop, report, move, free_bytes, now) -> MoveStatus:
    """Unpack the password-protected archives (see assetindex/unpack.py): the `paths`
    asked for (rar the index cannot tell locked), else all locked 7z or the first
    `limit`; a scan picks up the new folders."""
    root, password = status.root, raw["password"]
    base = settings.roots[root].resolve()
    if raw.get("paths"):
        items = [unpack.Locked(p, (base / p).stat().st_size if (base / p).is_file() else 0) for p in raw["paths"]]
    else:
        items = unpack.locked_archives(conn, root, base)[: raw.get("limit")]
    status = replace(status, total=len(items))
    report(status)
    progress = {"done": 0}

    def step(done: int) -> None:
        progress["done"] = done
        report(replace(status, done=done))

    result = unpack.unpack_all(conn, base, root, password, items, should_stop, step,
                               move=move, free=free_bytes)
    if result.unpacked:
        scan_request.request_scan(settings.data_dir)
    return replace(status, running=False, finished=now(), moved=result.unpacked, done=progress["done"],
                   problems=_problems(result.problems), error=result.error, stopped=result.stopped)


def _repair(conn, settings: Settings, paths: list[str], status: MoveStatus, should_stop, report, move, free_bytes,
            now) -> MoveStatus:
    """One pack rebuilt from damaged copies (see assetindex/repair.py); a scan picks it up."""
    base = settings.roots[status.root].resolve()
    companions = companions_by_unit(*stored_layout(conn, status.root))
    progress = {"done": 0, "total": 0}

    def step(done: int, total: int) -> None:
        progress.update(done=done, total=total)
        report(replace(status, done=done, total=total))

    moved, error, stopped = 0, None, False
    try:
        repair.repair_one(base, paths, companions, move=move, free=free_bytes, should_stop=should_stop, report=step)
        moved = 1
    except repair.Stopped:
        stopped = True
    except repair.HalfDone as err:  # the folder is there: the index should learn of it
        moved, error = 1, str(err)
    except repair.RepairError as err:
        error = str(err)
    except OSError as err:
        error = f"修复时出错：{err!r}"
    if moved:
        scan_request.request_scan(settings.data_dir)
    return replace(status, running=False, finished=now(), moved=moved, done=progress["done"],
                   total=progress["total"], error=error, stopped=stopped)


def _verify(settings: Settings, paths: list[str], status: MoveStatus, should_stop, report, test_archive, now) -> MoveStatus:
    """Read the archives whole (see assetindex/verify.py): a line each in `problems`,
    `moved` counts the whole ones. Nothing on the NAS changes."""
    status = replace(status, total=len(paths))
    report(status)
    progress = {"done": 0}

    def step(done: int) -> None:
        progress["done"] = done
        report(replace(status, done=done))

    results, whole, stopped = verify.verify_all(settings.roots[status.root].resolve(), paths, should_stop, step, test=test_archive)
    return replace(status, running=False, finished=now(), moved=whole, done=progress["done"],
                   problems=_problems(results), stopped=stopped)


def _purge(settings: Settings, status: MoveStatus, should_stop, report, remove, now) -> MoveStatus:
    """Delete what is in _待删除 for good (see assetindex/purge.py); a scan forgets it."""
    base = settings.roots[status.root].resolve()
    try:
        status = replace(status, total=purge.contents(base)[0])
    except purge.Refused as err:
        return replace(status, running=False, finished=now(), error=str(err))
    report(status)
    progress = {"done": 0}

    def step(done: int) -> None:
        progress["done"] = done
        report(replace(status, done=done))

    removed, problems, stopped = purge.purge(base, should_stop, step, remove=remove)
    if removed:
        scan_request.request_scan(settings.data_dir)
    return replace(status, running=False, finished=now(), moved=removed, done=progress["done"],
                   problems=_problems(problems), stopped=stopped)
