"""Unpacking password-protected archives into plain folders: the user wants no locked
archives in the library (2026-11-17). It runs in the mover, the only service that may
write the NAS, as a move job with action "unpack" and the password in the request
(never stored).

One archive: unpacked into a hidden folder next to it and checked (file count and bytes
against the archive's own list); then that folder (or the archive's one top folder, if
it has just one) takes the archive's name, the archive's previews move into it, and the
archive itself goes to _待删除/ for the user to delete. Nothing visible changes before
the rename; a crash leaves only the hidden folder, cleared on the next try.
"""

import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import py7zr

from assetindex.categories import DUPLICATES, TO_DELETE
from assetindex.organize_apply import _taken, move_no_replace
from assetindex.organize_move import Problem
from assetindex.scanner import UNPACKING_SUFFIX
from assetindex.units import companions_by_unit, stored_layout

MIN_FREE_BYTES = 20 * 1024**3  # left free on the NAS after an archive is unpacked
LOCKED_ERROR = "%Password%"  # how py7zr's PasswordRequired is recorded by the scanner

Mover = Callable[[str, str, bool], None]


class UnpackError(Exception):
    """This archive stays as it is; the others go on."""


class NoSpace(UnpackError):
    """The NAS is nearly full: the job stops."""


@dataclass(frozen=True)
class Locked:
    path: str
    size: int


@dataclass(frozen=True)
class UnpackResult:
    unpacked: int = 0
    problems: list[Problem] = field(default_factory=list)
    stopped: bool = False
    error: str | None = None


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def locked_archives(conn: sqlite3.Connection, root: str, base: Path | None = None) -> list[Locked]:
    """7z the scanner could not list for a password and not set aside (in, or confirmed
    for, _待删除 or _Duplicates). One inside a folder pack becomes a folder in it. With
    `base`, only those still there: the index learns of an unpacking at the next scan."""
    rows = conn.execute(
        "SELECT f.rel_path, f.size FROM archives a JOIN files f ON f.id = a.file_id"
        " WHERE f.root = ? AND a.status = 'error' AND a.error LIKE ? AND lower(f.ext) = '7z' ORDER BY f.rel_path",
        (root, LOCKED_ERROR),
    ).fetchall()
    leaving = {r[0] for r in conn.execute(
        "SELECT path FROM organize_units WHERE root = ? AND target LIKE ?", (root, TO_DELETE + "/%"))}
    aside = (TO_DELETE + "/", DUPLICATES + "/")
    return [
        Locked(path, size) for path, size in rows
        if not path.startswith(aside) and path not in leaving and (base is None or (base / path).is_file())
    ]


def contents(src: Path, password: str) -> tuple[int, int]:
    """(files, bytes) the archive says it holds."""
    with py7zr.SevenZipFile(src, password=password) as z:
        files = [i for i in z.list() if not i.is_directory]
    return len(files), sum(i.uncompressed for i in files)


def extract_7z(src: Path, out: Path, password: str) -> None:
    with py7zr.SevenZipFile(src, password=password) as z:
        z.extractall(path=out)


def _counted(folder: Path, skip_top: frozenset[str] = frozenset()) -> tuple[int, int]:
    """(files, bytes) under `folder`, leaving out its own files named in `skip_top`."""
    files = [p for p in folder.rglob("*") if p.is_file() and not (p.parent == folder and p.name in skip_top)]
    return len(files), sum(p.stat().st_size for p in files)


def _free_name(folder: Path, name: str) -> Path:
    """`name` in `folder`, numbered "x 2.7z" if taken."""
    stem, dot, ext = name.rpartition(".")
    stem, ext = (stem, dot + ext) if dot and stem else (name, "")
    path, n = folder / name, 2
    while _taken(path):
        path, n = folder / f"{stem} {n}{ext}", n + 1
    return path


def _unpacked_into(tmp: Path, src: Path, password: str, expected: tuple[int, int], extract) -> Path:
    """Unpack and check; the folder to keep (the archive's one top folder, or `tmp`)."""
    extract(src, tmp, password)
    got = _counted(tmp)
    if got != expected:
        raise UnpackError(f"解出来的文件对不上：应有 {expected[0]} 个、{expected[1]} 字节，实际 {got[0]} 个、{got[1]} 字节")
    top = list(tmp.iterdir())
    return top[0] if len(top) == 1 and top[0].is_dir() else tmp


def unpack_one(
    base: Path, rel: str, password: str, companions: list[str],
    move: Mover = move_no_replace, extract=extract_7z, free=free_bytes,
) -> str:
    """Unpack the archive at `rel` into a folder of its name; that folder's path. A run
    cut short after the folder got its name is finished: the folder holds what the
    archive does (its previews aside), the previews and the archive still have to move."""
    src = base / rel
    if not src.is_file():
        raise UnpackError("压缩包已经不在了（可能已经解开，等扫描更新）")
    folder_rel = rel.rpartition(".")[0]
    dest = base / folder_rel
    try:
        expected = contents(src, password)
    except Exception as err:  # py7zr has no single error for a wrong password
        raise UnpackError(f"密码不对，或者包坏了（{type(err).__name__}）") from None
    previews = frozenset(c.rpartition("/")[2] for c in companions)
    if _taken(dest):
        if not (dest.is_dir() and _counted(dest, previews) == expected):
            raise UnpackError(f"「{folder_rel}」已经有了，不往里面写")
    else:
        _unpack_to(base, rel, src, dest, password, expected, move, extract, free)
    for companion in companions:  # its previews go inside: the folder shows them
        if (base / companion).is_file():
            move(str(base / companion), str(_free_name(dest, companion.rpartition("/")[2])), False)
    aside = _free_name(base / TO_DELETE, src.name)
    aside.parent.mkdir(parents=True, exist_ok=True)
    move(str(src), str(aside), False)
    return folder_rel


def _unpack_to(base: Path, rel: str, src: Path, dest: Path, password: str, expected: tuple[int, int],
               move: Mover, extract, free) -> None:
    if free(base) < expected[1] + MIN_FREE_BYTES:
        raise NoSpace(f"NAS 剩余空间不够：解开「{rel}」要 {expected[1] / 1e9:.1f} GB，还要留 {MIN_FREE_BYTES / 1e9:.0f} GB")
    tmp = dest.parent / f".{dest.name}{UNPACKING_SUFFIX}"
    shutil.rmtree(tmp, ignore_errors=True)  # ours, left by a crash
    tmp.mkdir()
    try:
        keep = _unpacked_into(tmp, src, password, expected, extract)
        move(str(keep), str(dest), True)
    except Exception as err:
        shutil.rmtree(tmp, ignore_errors=True)
        if isinstance(err, UnpackError):
            raise
        raise UnpackError(f"解压失败：{err!r}") from None
    if keep != tmp:
        tmp.rmdir()


def unpack_all(
    conn: sqlite3.Connection, base: Path, root: str, password: str, items: list[Locked],
    should_stop: Callable[[], bool], report: Callable[[int], None],
    move: Mover = move_no_replace, free=free_bytes,
) -> UnpackResult:
    companions = companions_by_unit(*stored_layout(conn, root))
    unpacked, problems = 0, []
    for done, item in enumerate(items):
        if should_stop():
            return UnpackResult(unpacked, problems, stopped=True)
        report(done)
        try:
            unpack_one(base, item.path, password, companions.get(item.path, []), move=move, free=free)
            unpacked += 1
        except NoSpace as err:
            return UnpackResult(unpacked, problems, error=str(err))
        except UnpackError as err:
            problems.append(Problem(item.path, str(err)))
        except OSError as err:  # moving the previews or the archive: the folder is already there
            problems.append(Problem(item.path, f"解开了，但收尾时出错：{err!r}"))
    report(len(items))
    return UnpackResult(unpacked, problems)
