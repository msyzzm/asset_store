"""The fetcher service's work: hand queued downloads to aria2, follow them, and put each
finished file in the root's _下载 folder, where a scan picks it up and the organizer
proposes where it belongs, like any other pack.

Each download gets a folder of its own, _下载/.partial/<id>/, which scans skip. aria2
names the file inside from the response, so a pasted link needs no name. The table is
the record of what runs: aria2 forgets everything when the container restarts (an
update), so an active download it does not know is handed to it again, into the same
folder, and aria2 picks up where the file ended.

The web server changes rows too (cancel, retry), so every change here is conditional on
the state it was read in: a download cancelled meanwhile is never started or revived.

Nothing caps a download's size; one whose size is known is refused only when the NAS
has no room for it. A file identical to one already in the library is kept and marked
a duplicate: the user decides. That check reads whole files, so it runs on a thread of
its own and never holds up the queue.
"""

import logging
import re
import shutil
import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from assetindex import scan_request
from assetindex.aria2 import Aria2, Aria2Error, Aria2Unreachable
from assetindex.categories import DOWNLOADS, PARTIAL
from assetindex.config import Settings
from assetindex.db import connect
from assetindex.dupes import file_hash
from assetindex.organize_apply import move_no_replace
from assetindex.unpack import free_bytes

log = logging.getLogger(__name__)

USER_AGENT = "assetindex-fetcher/1.0 (self-hosted game asset library)"
FREE_MARGIN = 1024**3  # left free on the NAS after a download
MAX_TWIN_CHECKS = 50  # same-size files hashed when looking for a twin
MAX_NAME_BYTES = 200
HTML_SNIFF_BYTES = 512
ARIA2_ERRORS = {
    "3": "找不到文件（404）",
    "9": "磁盘空间不够",
    "19": "域名解析失败",
    "22": "服务器拒绝了请求（可能要登录）",
    "24": "服务器要求登录",
    "32": "校验不符：下载的文件和来源给的 md5 不一样",
}
_BAD_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')


def partial_dir(base: Path, download_id: int) -> Path:
    return base / DOWNLOADS / PARTIAL / str(download_id)


def safe_name(name: str, download_id: int) -> str:
    """A file name safe on the NAS: no separators or control characters, no leading
    dots, at most MAX_NAME_BYTES (the extension kept)."""
    name = _BAD_CHARS.sub("_", name).strip().lstrip(".").strip()
    if not name:
        return f"download-{download_id}"
    stem, dot, ext = name.rpartition(".")
    if not dot or len(ext) > 10:
        stem, ext = name, ""
    suffix = f".{ext}" if ext else ""
    while len((stem + suffix).encode()) > MAX_NAME_BYTES:
        stem = stem[:-1]
    return (stem or f"download-{download_id}") + suffix


def _landing(base: Path, name: str) -> Path:
    """A free path in _下载 for `name`: name, name-2, name-3... ("(2)" would read as a
    preview of another file, see units.py)."""
    folder = base / DOWNLOADS
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    for n in range(1, 10_000):
        candidate = folder / (name if n == 1 else f"{stem}-{n}{'.' + ext if ext else ''}")
        if not candidate.exists():
            return candidate
    raise OSError(f"no free name for {name} in {folder}")


def find_twin(conn: sqlite3.Connection, root: str, base: Path, path: Path, rel: str) -> str | None:
    """The path of a file in the library identical to `path` (same size, same content)."""
    size = path.stat().st_size
    rows = conn.execute(
        "SELECT f.rel_path, f.mtime, h.hash, h.mtime AS hashed_mtime FROM files f "
        "LEFT JOIN file_hashes h ON h.file_id = f.id AND h.size = f.size "
        "WHERE f.root = ? AND f.size = ? AND f.rel_path != ? LIMIT ?",
        (root, size, rel, MAX_TWIN_CHECKS),
    ).fetchall()
    if not rows:
        return None
    mine = file_hash(path)[0]
    for row in rows:
        theirs = row["hash"] if row["hash"] and row["hashed_mtime"] == row["mtime"] else None
        if theirs is None:
            try:
                theirs = file_hash(base / row["rel_path"])[0]
            except OSError:
                continue
        if theirs == mine:
            return row["rel_path"]
    return None


def _error_text(status: dict) -> str:
    code = str(status.get("errorCode") or "")
    known = ARIA2_ERRORS.get(code)
    if known:
        return known
    message = status.get("errorMessage") or ""
    return f"下载失败（aria2 错误 {code}）{message}".strip()


def _clear(folder: Path) -> None:
    shutil.rmtree(folder, ignore_errors=True)


def _is_web_page(path: Path) -> bool:
    """A page (a login form, a site's asset page) where a file was expected."""
    with path.open("rb") as f:
        head = f.read(HTML_SNIFF_BYTES).lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html"))


def _resumable(folder: Path) -> bool:
    """Whether aria2 can pick up what is in `folder`: every partial file still has its
    .aria2 control file. Without it aria2 would carry on from the file's length, but a
    download fetched in segments has holes below that length: a corrupt file."""
    if not folder.is_dir():
        return True
    names = {p.name for p in folder.iterdir()}
    return all(f"{name}.aria2" in names for name in names if not name.endswith(".aria2"))


class Fetcher:
    """One pass over the queue per tick(); the caller loops. `run_later(fn, *args)`
    runs the duplicate check (a thread by default; tests pass one that runs at once)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        rpc: Aria2,
        mover_busy: Callable[[], bool] = lambda: False,
        free: Callable[[Path], int] = free_bytes,
        move: Callable[[str, str, bool], None] = move_no_replace,
        now: Callable[[], float] = time.time,
        run_later: Callable | None = None,
    ) -> None:
        self.conn, self.settings, self.rpc = conn, settings, rpc
        self.mover_busy, self.free, self.move, self.now = mover_busy, free, move, now
        self.run_later = run_later or ThreadPoolExecutor(max_workers=1, thread_name_prefix="twins").submit

    def tick(self) -> dict[int, dict]:
        """Start, follow, finish or stop downloads; returns the running ones' progress.
        Raises Aria2Unreachable while aria2 does not answer: nothing is failed for that."""
        self._stop_cancelled()
        progress = self._follow_active()
        self._start_queued()
        return progress

    def _base(self, root: str) -> Path | None:
        path = self.settings.roots.get(root)
        return path.resolve() if path is not None else None

    def _set(self, download_id: int, expect: tuple[str, ...], **fields) -> bool:
        """Change the row if it is still in one of the `expect`ed states."""
        names = ", ".join(f"{k} = ?" for k in fields)
        marks = ", ".join("?" for _ in expect)
        with self.conn:
            cur = self.conn.execute(
                f"UPDATE downloads SET {names} WHERE id = ? AND status IN ({marks})",
                (*fields.values(), download_id, *expect),
            )
        return cur.rowcount == 1

    def _fail(self, row, error: str, folder: Path | None = None) -> None:
        if row["gid"]:
            self.rpc.forget(row["gid"])
        if folder is not None:
            _clear(folder)
        self._set(row["id"], (row["status"],), status="failed", error=error, gid=None, finished=self.now())

    def _stop_cancelled(self) -> None:
        rows = self.conn.execute("SELECT id, root, gid FROM downloads WHERE status = 'cancelled' AND gid IS NOT NULL").fetchall()
        for row in rows:
            self.rpc.forget(row["gid"])
            base = self._base(row["root"])
            if base is not None:
                _clear(partial_dir(base, row["id"]))
            self._set(row["id"], ("cancelled",), gid=None)

    def _start_queued(self) -> None:
        rows = self.conn.execute("SELECT * FROM downloads WHERE status = 'queued' ORDER BY id").fetchall()
        for row in rows:
            base = self._base(row["root"])
            if base is None:
                self._fail(row, "没有这个根目录")
                continue
            if row["size"] and self.free(base) < row["size"] + FREE_MARGIN:
                self._fail(row, "NAS 空间不够放这个文件")
                continue
            self._hand_over(row, base)

    def _hand_over(self, row, base: Path) -> None:
        folder = partial_dir(base, row["id"])
        if not _resumable(folder):
            log.warning("download %s lost its aria2 control file: starting it over", row["id"])
            _clear(folder)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            gid = self.rpc.add(row["url"], folder, USER_AGENT, row["md5"])
        except OSError as err:
            self._fail(row, f"建不了下载目录：{err!r}")
            return
        except Aria2Unreachable:
            raise  # the row stays as it is: tried again once aria2 answers
        except Aria2Error as err:
            log.warning("aria2 refused download %s: %s", row["id"], err)
            self._fail(row, f"aria2 不接受这个链接：{err}", folder)
            return
        try:
            recorded = self._set(row["id"], (row["status"],), status="active", gid=gid)
        except sqlite3.Error:  # e.g. locked: never leave aria2 a download no row knows
            self.rpc.forget(gid)
            raise
        if not recorded:  # cancelled meanwhile
            self.rpc.forget(gid)
            _clear(folder)

    def _follow_active(self) -> dict[int, dict]:
        progress = {}
        rows = self.conn.execute("SELECT * FROM downloads WHERE status = 'active' ORDER BY id").fetchall()
        for row in rows:
            base = self._base(row["root"])
            if base is None:
                self._fail(row, "没有这个根目录")
                continue
            status = self.rpc.status(row["gid"]) if row["gid"] else None
            if status is None:  # aria2 restarted: it resumes into the same folder
                log.info("download %s unknown to aria2: handing it over again", row["id"])
                self._hand_over(row, base)
                continue
            state = status.get("status")
            if state == "complete":
                if not self.mover_busy():  # never add files while a move job rearranges the root
                    self._finish(row, base)
            elif state in ("error", "removed"):
                self._fail(row, _error_text(status), partial_dir(base, row["id"]))
            else:
                total, done = int(status.get("totalLength") or 0), int(status.get("completedLength") or 0)
                if total and not row["size"] and not self._room_for(row, base, total, done):
                    continue
                progress[row["id"]] = {"done": done, "total": total, "speed": int(status.get("downloadSpeed") or 0)}
        return progress

    def _room_for(self, row, base: Path, total: int, done: int) -> bool:
        """The size just became known (aria2 got the response): is there room for the rest?"""
        self._set(row["id"], ("active",), size=total)
        if self.free(base) < total - done + FREE_MARGIN:
            self._fail(row, "NAS 空间不够放这个文件", partial_dir(base, row["id"]))
            return False
        return True

    def _finish(self, row, base: Path) -> None:
        folder = partial_dir(base, row["id"])
        files = [p for p in folder.iterdir() if p.is_file() and not p.name.endswith(".aria2")] if folder.is_dir() else []
        if len(files) != 1:
            self._fail(row, f"下载完成，但下载目录里有 {len(files)} 个文件", folder)
            return
        try:
            if _is_web_page(files[0]):
                self._fail(row, "下到的是一个网页，不是文件：要贴文件本身的下载直链（网页上下载按钮的链接）", folder)
                return
            target = _landing(base, safe_name(files[0].name, row["id"]))
            self.move(str(files[0]), str(target), False)
        except OSError as err:
            self._fail(row, f"下载完成，但放不进 {DOWNLOADS}：{err!r}", folder)
            return
        # Landed: recorded at once, so nothing after this can make it download again.
        rel = target.relative_to(base).as_posix()
        self._set(row["id"], ("active", "cancelled"), status="done", path=rel, gid=None, finished=self.now())
        self.rpc.forget(row["gid"])
        _clear(folder)
        scan_request.request_scan(self.settings.data_dir)
        log.info("download %s landed at %s", row["id"], rel)
        self.run_later(self._mark_twin, row["id"], row["root"], rel)

    def _mark_twin(self, download_id: int, root: str, rel: str) -> None:
        """A duplicate check on its own connection (it runs on another thread)."""
        base = self._base(root)
        conn = connect(self.settings.db_path, init_schema=False)
        try:
            twin = find_twin(conn, root, base, base / rel, rel)
            if twin:
                with conn:
                    conn.execute("UPDATE downloads SET status = 'duplicate', dup_of = ? WHERE id = ? AND status = 'done'",
                                 (twin, download_id))
                log.info("download %s is the same as %s", download_id, twin)
        except Exception:
            log.exception("duplicate check of download %s failed", download_id)
        finally:
            conn.close()
