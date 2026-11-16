"""Carry out a move plan on disk, undo it, and recover from a run cut short.

This is the one place that changes the library, so:
- a pack moves as one transaction, journaled first: "begin" (every rename, the unit's
  old and new path), "did" after each rename, "commit" once the index follows, else
  "abort" (all turned back) or "stuck" (could not be turned back: the run stops). The
  journal is flushed to disk at every line;
- recover() settles any transaction left open by a crash: all renames done -> finish
  the index; none -> abort; some -> turn them back. The scanner calls it (read-only:
  it can only finish the index) and refuses to scan while one stays open, since a scan
  would drop the moved files' rows, and with them captions and previews;
- never overwrite: targets are checked first (without case: NAS shares often ignore
  it), and the move itself refuses to replace: a file is hard-linked to its new name
  then unlinked (link fails if the name exists, where rename would replace it); a
  folder's new name is created empty first (mkdir fails if it exists) then renamed
  onto. Where hard links are not supported, a checked rename is the fallback;
- the index follows: files keep their ids (captions, previews, hashes stay attached),
  only paths change; text vectors, which include the path, are dropped to be redone.
The caller holds the scan lock and the model lock, so nothing else writes the index.
"""

import errno
import json
import logging
import os
import sqlite3
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from assetindex.classify import under
from assetindex.organize_move import MovePlan, Problem, Rename, left_folders
from assetindex.sequences import group_sequences, sequence_of

FATAL_ERRNOS = {errno.EXDEV, errno.EROFS, errno.EACCES, errno.EPERM, errno.ENOSPC}
# A link failing with these says something about the files; any other errno (EPERM,
# EOPNOTSUPP, EINVAL... on shares without hard links) means: use a checked rename.
LINK_DEFINITE = {errno.EEXIST, errno.ENOENT, errno.EXDEV, errno.ENOSPC, errno.EROFS, getattr(errno, "EDQUOT", -1)}
JOURNAL_DIR = "moves"
log = logging.getLogger(__name__)
_links = {"work": True}  # set once a link fails as unsupported: the mount has no hard links


@dataclass(frozen=True)
class ApplyResult:
    moved: int = 0  # packs moved (or put back, for an undo)
    problems: tuple[Problem, ...] = ()
    stopped: bool = False
    error: str | None = None  # why the run stopped early


class Fatal(Exception):
    """Stop the whole run: the file system or the index cannot be trusted to go on."""


@dataclass(frozen=True)
class _Txn:
    id: int
    kind: str  # "move" | "undo"
    unit: str  # the unit before
    new_unit: str  # the unit after
    target: str  # organize_units.target after
    status: str  # organize_units.status after
    steps: tuple[Rename, ...]
    undoes: int | None = None  # for an undo: the move it reverses


# ---- file system --------------------------------------------------------------------


def _taken(path: Path) -> bool:
    """Exists, or a name differing only in case does (the share may not care)."""
    if os.path.lexists(path):
        return True
    try:
        lowered = path.name.casefold()
        return any(name.casefold() == lowered for name in os.listdir(path.parent))
    except FileNotFoundError:
        return False


def move_no_replace(src: str, dst: str, is_dir: bool) -> None:
    """Move without ever replacing what is at `dst` (see the module doc)."""
    if sys.platform == "win32":  # rename refuses an existing target here
        os.rename(src, dst)
        return
    if is_dir:
        os.mkdir(dst)  # fails if anything has the name: our claim on it
        try:
            os.rename(src, dst)  # onto our empty folder; ENOTEMPTY if someone wrote in it
        except OSError:
            _rmdir_quietly(dst)
            raise
        return
    if _links["work"]:
        try:
            os.link(src, dst)  # fails if the name exists
        except OSError as err:
            if err.errno == errno.EEXIST and _same(src, dst):
                pass  # a retried NFS link: it did happen
            elif err.errno in LINK_DEFINITE:
                raise
            else:
                log.info("no hard links on this mount (%s): checked renames from now on", err)
                _links["work"] = False
        if _links["work"]:
            try:
                os.unlink(src)
            except OSError:
                if os.path.lexists(src) and _same(src, dst):
                    os.unlink(dst)  # back to one name, at the old place
                raise
            return
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "已经有了", dst)
    os.rename(src, dst)


def _same(a, b) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _rmdir_quietly(path: str) -> None:
    try:
        os.rmdir(path)
    except OSError:
        pass


def _remove_empty_parents(base: Path, rel: str) -> None:
    """After a pack left a folder: remove it if empty, up the tree (not the root)."""
    folder = (base / rel).parent
    while folder != base and base in folder.parents:
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


def _missing_parents(base: Path, rel: str) -> list[Path]:
    """The folders a move into `rel` will create, deepest first."""
    missing, folder = [], (base / rel).parent
    while folder != base and base in folder.parents and not os.path.lexists(folder):
        missing.append(folder)
        folder = folder.parent
    return missing


# ---- journal ------------------------------------------------------------------------


class _Journal:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            ends_clean = path.read_bytes().endswith(b"\n") if path.stat().st_size else True
        except FileNotFoundError:
            ends_clean = True
        self._f = open(path, "a", encoding="utf-8")
        if not ends_clean:  # a line cut short by a crash: keep the next one separate
            self._f.write("\n")

    def write(self, **record) -> None:
        try:
            self._f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._f.flush()
            os.fsync(self._f.fileno())
        except OSError as err:
            raise Fatal(f"写不了移动记录：{err}") from err

    def close(self) -> None:
        self._f.close()


_parsed: dict[Path, tuple[tuple[int, int], list[dict]]] = {}  # the page polls: parse a journal once per change


def _records(path: Path) -> list[dict]:
    try:
        stat = path.stat()
        key = (stat.st_size, stat.st_mtime_ns)
    except OSError:
        key = None
    cached = _parsed.get(path)
    if key is not None and cached is not None and cached[0] == key:
        return cached[1]
    records = _parse(path)
    if key is not None:
        _parsed[path] = (key, records)
    return records


def _parse(path: Path) -> list[dict]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:  # cut short by a crash
            continue
    return records


def _steps(raw: list) -> tuple[Rename, ...]:
    return tuple(Rename(s[0], s[1], bool(s[2])) for s in raw)


def _txns(records: list[dict]) -> tuple[dict[int, _Txn], dict[int, str]]:
    """Transactions in a journal, and how each ended ("commit", "abort", "stuck", or
    absent: still open)."""
    txns, ends = {}, {}
    for r in records:
        op = r.get("op")
        if op == "begin":
            txns[r["txn"]] = _Txn(r["txn"], r["kind"], r["unit"], r["new_unit"], r["target"], r["status"],
                                  _steps(r["steps"]), r.get("undoes"))
        elif op in ("commit", "abort"):
            ends[r["txn"]] = op
        elif op == "stuck":
            ends.setdefault(r["txn"], "stuck")
    return txns, ends


def open_transactions(journal: Path) -> list[_Txn]:
    txns, ends = _txns(_records(journal))
    return [t for t in txns.values() if ends.get(t.id) in (None, "stuck")]


def journals(data_dir: Path) -> list[Path]:
    folder = data_dir / JOURNAL_DIR
    return sorted(folder.glob("*.jsonl")) if folder.is_dir() else []


def summary(journal: Path) -> dict:
    """One run, for the history list: when, how many packs moved, how many are still
    moved (undo would put them back), and whether a pack is left half done."""
    records = _records(journal)
    txns, ends = _txns(records)
    moves = [t for t in txns.values() if t.kind == "move" and ends.get(t.id) == "commit"]
    undone = {t.undoes for t in txns.values() if t.kind == "undo" and ends.get(t.id) == "commit"}
    start = next((r for r in records if r.get("op") == "start"), {})
    return {
        "root": start.get("root"),
        "started": start.get("at"),
        "moved": len(moves),
        "still_moved": sum(1 for t in moves if t.id not in undone),
        "open": sum(1 for t in txns.values() if ends.get(t.id) in (None, "stuck")),
    }


def journal_root(journal: Path) -> str | None:
    """The root a journal's run moved packs in."""
    return next((r.get("root") for r in _records(journal) if r.get("op") == "start"), None)


def unsettled(data_dir: Path) -> list[Path]:
    """Journals with a transaction still open: the scan must not run until settled."""
    return [j for j in journals(data_dir) if open_transactions(j)]


# ---- index --------------------------------------------------------------------------


def rekey(conn: sqlite3.Connection, root: str, txn: _Txn) -> None:
    """Point the index at the new paths: files (and their sequence keys), the unit's
    organize row (with its target and status) and anything naming it. Rows a stale
    entry left at the new paths go first, so nothing collides."""
    if _rekeyed(conn, root, txn.steps[0]):
        return  # a crash between the index update and its journal line: already done
    with conn:
        moved_ids = []
        conn.execute("DELETE FROM organize_units WHERE root = ? AND path = ? AND path != ?", (root, txn.new_unit, txn.unit))
        conn.execute("DELETE FROM dup_keep WHERE root = ? AND path = ?", (root, txn.new_unit))
        for step in txn.steps:
            if step.is_dir:
                for table in ("unit_splits", "unit_joins"):  # the user's choices inside move with it
                    conn.execute(f"DELETE FROM {table} WHERE root = ? AND (path = ? OR (path >= ? AND path < ?))",
                                 (root, step.dst, *under(step.dst)))
                    conn.execute(
                        f"UPDATE {table} SET path = ? || substr(path, ?)"
                        " WHERE root = ? AND (path = ? OR (path >= ? AND path < ?))",
                        (step.dst, len(step.src) + 1, root, step.src, *under(step.src)),
                    )
                rows = conn.execute("SELECT id, rel_path, kind FROM files WHERE root = ? AND rel_path >= ? AND rel_path < ?",
                                    (root, *under(step.src))).fetchall()
                pairs = [(r, step.dst + r["rel_path"][len(step.src) :]) for r in rows]
            else:
                row = conn.execute("SELECT id, rel_path, kind FROM files WHERE root = ? AND rel_path = ?",
                                   (root, step.src)).fetchone()
                pairs = [(row, step.dst)] if row else []
            for row, new_path in pairs:
                seq_key, seq_index = sequence_of(new_path, row["kind"]) or (None, None)
                conn.execute("UPDATE files SET rel_path = ?, name = ?, seq_key = ?, seq_index = ? WHERE id = ?",
                             (new_path, new_path.rpartition("/")[2], seq_key, seq_index, row["id"]))
                moved_ids.append(row["id"])
        if txn.kind == "move":  # the folder the pack left stays a folder of packs
            conn.executemany("INSERT OR IGNORE INTO unit_splits (root, path) VALUES (?, ?)",
                             [(root, folder) for folder in left_folders([txn.steps[0]])])
            if txn.steps[0].is_dir:  # and a moved folder pack stays one pack where it lands
                conn.execute("INSERT OR IGNORE INTO unit_joins (root, path) VALUES (?, ?)", (root, txn.steps[0].dst))
        conn.executemany("DELETE FROM embeddings WHERE source = 'text' AND file_id = ?", [(i,) for i in moved_ids])
        conn.execute("UPDATE organize_units SET path = ?, target = ?, status = ? WHERE root = ? AND path = ?",
                     (txn.new_unit, txn.target, txn.status, root, txn.unit))
        conn.execute("UPDATE organize_units SET dup_of = ? WHERE root = ? AND dup_of = ?", (txn.new_unit, root, txn.unit))
        conn.execute("UPDATE dup_keep SET path = ? WHERE root = ? AND path = ?", (txn.new_unit, root, txn.unit))


def _rekeyed(conn: sqlite3.Connection, root: str, step: Rename) -> bool:
    """The index already has `step` at its new path and nothing at the old one."""
    def has(path: str) -> bool:
        if step.is_dir:
            return conn.execute("SELECT 1 FROM files WHERE root = ? AND rel_path >= ? AND rel_path < ? LIMIT 1",
                                (root, *under(path))).fetchone() is not None
        return conn.execute("SELECT 1 FROM files WHERE root = ? AND rel_path = ?", (root, path)).fetchone() is not None
    return not has(step.src) and has(step.dst)


# ---- transactions -------------------------------------------------------------------


def _moved(base: Path, step: Rename) -> bool:
    return not os.path.lexists(base / step.src) and os.path.lexists(base / step.dst)


def _where(base: Path, step: Rename) -> str:
    """Where a step's file is, by evidence: "moved", "at_src", "claimed" (still at its
    old place, with a leftover at the new one: a hard link or our empty folder, which
    only needs removing) or "unknown" (neither place, or both with different files:
    the NAS may not be mounted, so nothing is decided on it)."""
    src, dst = base / step.src, base / step.dst
    at_src, at_dst = os.path.lexists(src), os.path.lexists(dst)
    if at_dst and not at_src:
        return "moved"
    if at_src and not at_dst:
        return "at_src"
    if at_src and at_dst:
        if step.is_dir and dst.is_dir() and not any(dst.iterdir()):
            return "claimed"
        if not step.is_dir and _same(src, dst):
            return "claimed"
    return "unknown"


def _unclaim(base: Path, step: Rename) -> None:
    dst = base / step.dst
    if step.is_dir:
        dst.rmdir()
    else:
        dst.unlink()


def _turn_back(base: Path, done: list[Rename], move, created: list[Path] = ()) -> list[str]:
    """Undo the renames done, newest first; what could not be turned back. Only checks
    that the old name is free (a case-insensitive check would trip on a sibling). Then
    removes the folders the move created, if empty."""
    failed = []
    for step in reversed(done):
        if not _moved(base, step):
            continue
        try:
            if os.path.lexists(base / step.src):
                raise FileExistsError(errno.EEXIST, "原位置被占了", step.src)
            move(str(base / step.dst), str(base / step.src), step.is_dir)
        except OSError as err:
            if not _moved(base, step):  # it did go back (NFS can report a retried rename as failed)
                continue
            failed.append(f"{step.dst}: {err.strerror or err}")
    if not failed:
        for folder in created:
            _rmdir_quietly(str(folder))
    return failed


def _end_failed(journal: _Journal, base: Path, txn: _Txn, done: list[Rename], move, why: str,
                created: list[Path] = ()) -> None:
    """Turn back what a failed transaction did; record how it ended."""
    failed = _turn_back(base, done, move, created)
    if failed:
        journal.write(op="stuck", txn=txn.id, detail=failed)
        raise Fatal(f"「{txn.unit}」没能还原，已停止，见移动记录：{'；'.join(failed)}（原因：{why}）")
    journal.write(op="abort", txn=txn.id, why=why)


def _run(conn: sqlite3.Connection, base: Path, root: str, journal: _Journal, txn: _Txn, move) -> None:
    """One pack, all or nothing. Raises OSError (this pack failed, turned back) or Fatal."""
    journal.write(op="begin", txn=txn.id, kind=txn.kind, unit=txn.unit, new_unit=txn.new_unit, target=txn.target,
                  status=txn.status, undoes=txn.undoes, steps=[[s.src, s.dst, s.is_dir] for s in txn.steps])
    done: list[Rename] = []
    created: list[Path] = []
    for i, step in enumerate(txn.steps):
        source, target = base / step.src, base / step.dst
        # Moving in: no name like it may exist. Putting back: only the exact name (its
        # case-only sibling may be on its way back too).
        if not os.path.lexists(source):
            _fail(journal, base, txn, done, move, created, FileNotFoundError(errno.ENOENT, "不见了", step.src))
        if _taken(target) if txn.kind == "move" else os.path.lexists(target):
            _fail(journal, base, txn, done, move, created, FileExistsError(errno.EEXIST, "已经有了", step.dst))
        try:
            created += _missing_parents(base, step.dst)
            target.parent.mkdir(parents=True, exist_ok=True)
            move(str(source), str(target), step.is_dir)
        except OSError as err:
            if not _moved(base, step):  # a retried NFS rename may report failure after all
                _fail(journal, base, txn, done, move, created, err)
        except Exception as err:  # anything else: the same turn back, then stop
            _end_failed(journal, base, txn, done, move, f"{step.src}: {err!r}", created)
            raise Fatal(f"移动出错，已停止：{err!r}") from err
        done.append(step)
        journal.write(op="did", txn=txn.id, step=i)
    try:
        rekey(conn, root, txn)
    except Exception as err:  # the index refused (locked, ...): put the files back, stop
        _end_failed(journal, base, txn, done, move, f"索引没能更新：{err!r}", created)
        raise Fatal(f"索引没能更新，已把「{txn.unit}」放回原处并停止：{err!r}") from err
    journal.write(op="commit", txn=txn.id)


def _fail(journal: _Journal, base: Path, txn: _Txn, done: list[Rename], move, created, err: OSError) -> None:
    """End a pack that failed with `err`: turn it back, then raise (Fatal if the errno
    says the run cannot go on)."""
    _end_failed(journal, base, txn, done, move, f"{err.filename}: {err.strerror or err}", created)
    if err.errno in FATAL_ERRNOS:
        raise Fatal(f"移动出错，已停止：{os.strerror(err.errno)}：{err.filename}") from err
    raise err


def _next_id(journal_path: Path) -> int:
    txns, _ = _txns(_records(journal_path)) if journal_path.exists() else ({}, {})
    return max(txns, default=0) + 1


def apply_plan(
    conn: sqlite3.Connection,
    base: Path,
    root: str,
    plan: MovePlan,
    journal_path: Path,
    should_stop: Callable[[], bool] = lambda: False,
    report: Callable[[int, int], None] = lambda done, total: None,
    move: Callable[[str, str, bool], None] = move_no_replace,
) -> ApplyResult:
    journal = _Journal(journal_path)
    next_id = _next_id(journal_path)
    moved, problems = 0, []
    try:
        journal.write(op="start", root=root, at=time.time(), units=len(plan.moves))
        for i, unit_move in enumerate(plan.moves):
            if should_stop():
                return ApplyResult(moved, tuple(problems), stopped=True)
            report(i, len(plan.moves))
            txn = _Txn(next_id + i, "move", unit_move.path, unit_move.new_path, unit_move.new_path, "accepted",
                       unit_move.renames)
            try:
                _run(conn, base, root, journal, txn, move)
            except OSError as err:
                problems.append(Problem(unit_move.path, err.strerror or str(err)))
                continue
            # the planned target, for an undo to restore the proposal
            journal.write(op="planned", txn=txn.id, target=unit_move.target)
            moved += 1
        report(len(plan.moves), len(plan.moves))
        return ApplyResult(moved, tuple(problems))
    except Fatal as err:
        return ApplyResult(moved, tuple(problems), error=str(err))
    finally:
        _finish(conn, root, journal, moved)


def _finish(conn: sqlite3.Connection, root: str, journal: _Journal, changed: int) -> None:
    try:
        if changed:
            group_sequences(conn, root)  # moved frames: sequence heads and counts; the next scan redoes it
    except Exception:
        log.exception("regrouping sequences after the moves failed")
    try:
        journal.write(op="end", at=time.time())
    except Fatal:
        pass
    finally:
        journal.close()


def undo(
    conn: sqlite3.Connection,
    base: Path,
    root: str,
    journal_path: Path,
    move: Callable[[str, str, bool], None] = move_no_replace,
    should_stop: Callable[[], bool] = lambda: False,
    report: Callable[[int, int], None] = lambda done, total: None,
) -> ApplyResult:
    """Put back every pack the journal moved, newest first, as a proposal to confirm
    again. Only where all its files are where the move left them and their old places
    are free; running it again retries only what could not be put back."""
    records = _records(journal_path)
    txns, ends = _txns(records)
    planned = {r["txn"]: r["target"] for r in records if r.get("op") == "planned"}
    undone = {t.undoes for t in txns.values() if t.kind == "undo" and ends.get(t.id) == "commit"}
    todo = [t for t in txns.values() if t.kind == "move" and ends.get(t.id) == "commit" and t.id not in undone]
    journal = _Journal(journal_path)
    next_id = max(txns, default=0) + 1
    restored, problems = 0, []
    try:
        for n, done in enumerate(sorted(todo, key=lambda t: -t.id)):
            if should_stop():
                return ApplyResult(restored, tuple(problems), stopped=True)
            report(n, len(todo))
            back = tuple(Rename(s.dst, s.src, s.is_dir) for s in reversed(done.steps))
            if not all(os.path.lexists(base / s.src) and not os.path.lexists(base / s.dst) for s in back):
                problems.append(Problem(done.unit, "文件已经不在移动后的位置，或者原来的位置被占了，没有撤销"))
                continue
            txn = _Txn(next_id + n, "undo", done.new_unit, done.unit, planned.get(done.id, done.target), "proposed",
                       back, undoes=done.id)
            try:
                _run(conn, base, root, journal, txn, move)
            except OSError as err:
                problems.append(Problem(done.unit, f"撤销失败：{err.strerror or err}"))
                continue
            for step in back:
                _remove_empty_parents(base, step.src)  # the category folders the move made, if empty now
            restored += 1
        return ApplyResult(restored, tuple(problems))
    except Fatal as err:
        return ApplyResult(restored, tuple(problems), error=str(err))
    finally:
        _finish(conn, root, journal, restored)


def recover(
    conn: sqlite3.Connection,
    base: Path,
    root: str,
    journal_path: Path,
    move: Callable[[str, str, bool], None] | None = move_no_replace,
) -> list[Problem]:
    """Settle transactions a crash left open. All renames done: finish the index; none:
    abort; some: turn them back (needs `move`; without it, read-only, they stay open).
    Returns what is still open."""
    left = []
    if not base.is_dir():  # the NAS is not mounted: decide nothing
        return [Problem(t.unit, "看不到根目录（NAS 没挂上？），这个包先不处理") for t in open_transactions(journal_path)]
    journal = _Journal(journal_path)
    try:
        for txn in open_transactions(journal_path):
            where = [_where(base, s) for s in txn.steps]
            if "unknown" in where:
                left.append(Problem(txn.unit, "找不到这个包的文件（NAS 没挂上，或被别人动过），先不处理"))
                continue
            if "claimed" in where:  # a link or an empty folder left at the new place
                if move is None:
                    left.append(Problem(txn.unit, "移动到一半中断了：在整理页点「恢复」放回原处"))
                    continue
                for step, state in zip(txn.steps, where, strict=True):
                    if state == "claimed":
                        _unclaim(base, step)
            done = [s for s, state in zip(txn.steps, where, strict=True) if state == "moved"]
            if len(done) == len(txn.steps):
                try:
                    rekey(conn, root, txn)
                except Exception as err:
                    left.append(Problem(txn.unit, f"文件已移动，索引没能更新：{err!r}"))
                    continue
                journal.write(op="commit", txn=txn.id, recovered=True)
            elif not done:
                journal.write(op="abort", txn=txn.id, why="恢复：一个文件都没移动")
            elif move is None:
                left.append(Problem(txn.unit, "移动到一半中断了：在整理页点「恢复」放回原处"))
            else:
                try:
                    _end_failed(journal, base, txn, done, move, "恢复：移动到一半中断了")
                except Fatal as err:
                    left.append(Problem(txn.unit, str(err)))
    finally:
        journal.close()
    return left
