"""Carry out a move plan on disk, and undo it.

Rules, because this is the one place that changes the library:
- only os.rename: on one file system it is atomic and never copies. A rename that would
  cross file systems (EXDEV), or a read-only mount, stops the run;
- never overwrite: os.rename silently replaces a file on Linux, so every target is
  checked first, without case (NAS shares often ignore it);
- a pack moves whole or not at all: if one of its renames fails, the ones done are
  turned back;
- every rename is written to the journal (and flushed to disk) before it happens, so a
  crash leaves a record of what may have moved; undo reads the journal backwards;
- the index follows: files keep their ids (captions, previews, hashes stay attached),
  only paths change; text vectors, which include the path, are dropped to be redone.
The caller holds the scan lock and the model lock, so nothing else touches the index.
"""

import errno
import json
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from assetindex.classify import under
from assetindex.organize_move import MovePlan, Problem, Rename
from assetindex.sequences import sequence_of

FATAL_ERRNOS = {errno.EXDEV, errno.EROFS, errno.EACCES, errno.EPERM, errno.ENOSPC}


@dataclass(frozen=True)
class ApplyResult:
    moved: int = 0  # packs moved (or put back, for an undo)
    problems: tuple[Problem, ...] = ()
    stopped: bool = False
    error: str | None = None  # why the run stopped early


class _Fatal(Exception):
    pass


def _taken(path: Path) -> bool:
    """Exists, or a name differing only in case does (the share may not care)."""
    if os.path.lexists(path):
        return True
    try:
        lowered = path.name.lower()
        return any(name.lower() == lowered for name in os.listdir(path.parent))
    except FileNotFoundError:
        return False


class _Journal:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(path, "a", encoding="utf-8")

    def write(self, **record) -> None:
        self._f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._f.flush()
        os.fsync(self._f.fileno())

    def close(self) -> None:
        self._f.close()


def _rename(base: Path, src: str, dst: str, rename: Callable[[str, str], None]) -> None:
    source, target = base / src, base / dst
    if not os.path.lexists(source):
        raise FileNotFoundError(errno.ENOENT, f"「{src}」不见了")
    if _taken(target):
        raise FileExistsError(errno.EEXIST, f"「{dst}」已经有了")
    target.parent.mkdir(parents=True, exist_ok=True)
    rename(str(source), str(target))


def _move_all(base: Path, unit: str, steps: list[Rename], journal: _Journal, rename, op: str) -> None:
    """All of a pack's renames, or none: turn back the done ones on any failure."""
    done: list[Rename] = []
    try:
        for step in steps:
            journal.write(op=op, unit=unit, src=step.src, dst=step.dst, is_dir=step.is_dir)
            _rename(base, step.src, step.dst, rename)
            done.append(step)
    except OSError as err:
        for step in reversed(done):
            journal.write(op="revert", unit=unit, src=step.dst, dst=step.src, is_dir=step.is_dir)
            _rename(base, step.dst, step.src, rename)
        if err.errno in FATAL_ERRNOS:
            raise _Fatal(f"{os.strerror(err.errno)}：{err.filename or unit}") from err
        raise


def rekey(
    conn: sqlite3.Connection, root: str, renames: list[Rename], old_unit: str, new_unit: str,
    target: str, status: str = "accepted",
) -> None:
    """Point the index at the new paths: files (and their sequence keys), the unit's
    organize row (with its target and status) and anything naming it."""
    with conn:
        moved_ids = []
        for step in renames:
            if step.is_dir:
                rows = conn.execute(
                    "SELECT id, rel_path, kind FROM files WHERE root = ? AND rel_path >= ? AND rel_path < ?",
                    (root, *under(step.src)),
                ).fetchall()
                pairs = [(r, step.dst + r["rel_path"][len(step.src) :]) for r in rows]
                for table in ("unit_splits", "unit_joins"):  # the user's choices inside move with it
                    conn.execute(
                        f"UPDATE {table} SET path = ? || substr(path, ?)"
                        " WHERE root = ? AND (path = ? OR (path >= ? AND path < ?))",
                        (step.dst, len(step.src) + 1, root, step.src, *under(step.src)),
                    )
            else:
                row = conn.execute(
                    "SELECT id, rel_path, kind FROM files WHERE root = ? AND rel_path = ?", (root, step.src)
                ).fetchone()
                pairs = [(row, step.dst)] if row else []
            for row, new_path in pairs:
                seq_key, seq_index = sequence_of(new_path, row["kind"]) or (None, None)
                conn.execute(
                    "UPDATE files SET rel_path = ?, name = ?, seq_key = ?, seq_index = ? WHERE id = ?",
                    (new_path, new_path.rpartition("/")[2], seq_key, seq_index, row["id"]),
                )
                moved_ids.append(row["id"])
        conn.executemany("DELETE FROM embeddings WHERE source = 'text' AND file_id = ?", [(i,) for i in moved_ids])
        conn.execute(
            "UPDATE organize_units SET path = ?, target = ?, status = ? WHERE root = ? AND path = ?",
            (new_unit, target, status, root, old_unit),
        )
        conn.execute("UPDATE organize_units SET dup_of = ? WHERE root = ? AND dup_of = ?", (new_unit, root, old_unit))
        conn.execute("UPDATE dup_keep SET path = ? WHERE root = ? AND path = ?", (new_unit, root, old_unit))


def apply_plan(
    conn: sqlite3.Connection,
    base: Path,
    root: str,
    plan: MovePlan,
    journal_path: Path,
    should_stop: Callable[[], bool] = lambda: False,
    report: Callable[[int, int], None] = lambda done, total: None,
    rename: Callable[[str, str], None] = os.rename,
) -> ApplyResult:
    journal = _Journal(journal_path)
    journal.write(op="start", root=root, at=time.time(), units=len(plan.moves))
    moved, problems = 0, []
    try:
        for i, move in enumerate(plan.moves):
            if should_stop():
                return ApplyResult(moved, tuple(problems), stopped=True)
            report(i, len(plan.moves))
            try:
                _move_all(base, move.path, list(move.renames), journal, rename, op="rename")
            except _Fatal as err:
                return ApplyResult(moved, tuple(problems), error=f"移动出错，已停止：{err}")
            except OSError as err:
                problems.append(Problem(move.path, err.strerror or str(err)))
                continue
            rekey(conn, root, list(move.renames), move.path, move.new_path, target=move.new_path)
            journal.write(op="indexed", unit=move.path, new=move.new_path, target=move.target)
            moved += 1
        report(len(plan.moves), len(plan.moves))
        return ApplyResult(moved, tuple(problems))
    finally:
        journal.write(op="end", at=time.time())
        journal.close()


def _read_journal(path: Path) -> list[dict]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:  # a line cut short by a crash: nothing after it happened
            break
    return records


def _moved_units(records: list[dict]) -> list[tuple[str, dict, list[Rename]]]:
    """Packs the journal moved and indexed, not undone yet: (old path, its "indexed"
    record, renames)."""
    renames: dict[str, list[Rename]] = {}
    indexed: dict[str, dict] = {}
    for r in records:
        op = r.get("op")
        if op == "rename":
            renames.setdefault(r["unit"], []).append(Rename(r["src"], r["dst"], r["is_dir"]))
        elif op == "indexed":
            indexed[r["unit"]] = r
        elif op == "unindexed":
            indexed.pop(r["unit"], None)
    return [(unit, indexed[unit], renames[unit]) for unit in renames if unit in indexed]


def undo(
    conn: sqlite3.Connection,
    base: Path,
    root: str,
    journal_path: Path,
    rename: Callable[[str, str], None] = os.rename,
) -> ApplyResult:
    """Put back every pack the journal moved, newest first. A pack is put back only if
    all its files are still where the move left them and their old places are free.
    Running it again only retries what could not be put back."""
    moved_units = _moved_units(_read_journal(journal_path))
    journal = _Journal(journal_path)
    journal.write(op="undo-start", at=time.time())
    restored, problems = 0, []
    try:
        for unit, done, steps in reversed(moved_units):
            if not all(os.path.lexists(base / s.dst) and not _taken(base / s.src) for s in steps):
                problems.append(Problem(unit, "文件已经不在移动后的位置，或者原来的位置被占了，没有撤销"))
                continue
            back = [Rename(s.dst, s.src, s.is_dir) for s in steps]
            try:
                _move_all(base, unit, back, journal, rename, op="undo")
            except (OSError, _Fatal) as err:
                problems.append(Problem(unit, f"撤销失败：{err}"))
                continue
            # Back to a proposal: it moves again only once confirmed again.
            rekey(conn, root, back, done["new"], unit, target=done.get("target", done["new"]), status="proposed")
            journal.write(op="unindexed", unit=unit)
            restored += 1
        return ApplyResult(restored, tuple(problems))
    finally:
        journal.write(op="undo-end", at=time.time())
        journal.close()
