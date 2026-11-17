"""Emptying _待删除 for good: what the user set aside for deleting, after checking it. Runs
in the mover, the only service that may write the NAS, as a move job with action
"purge". Files go one by one, then the emptied folders (_待删除 itself stays). Links are
removed, never followed. Deleting over NFS bypasses the NAS's recycle bin: the page says
so and wants the word 删除 typed before it starts.
"""

import os
import sys
from collections.abc import Callable
from pathlib import Path

from assetindex.categories import TO_DELETE
from assetindex.organize_move import Problem

Remover = Callable[[str], None]


class Refused(Exception):
    """_待删除 is a link (or not a folder): deleting through it could reach anything."""


def _trash(base: Path) -> Path | None:
    """`base`'s _待删除 when it is a real folder, None when there is none."""
    trash = base / TO_DELETE
    if os.path.islink(trash):
        raise Refused(f"{TO_DELETE} 是一个链接，不从这里删除")
    if not os.path.lexists(trash):
        return None
    if not trash.is_dir():
        raise Refused(f"{TO_DELETE} 不是文件夹")
    return trash


def _size(path: str) -> int | None:
    try:
        return os.lstat(path).st_size
    except FileNotFoundError:  # gone meanwhile
        return None


def _items(folder: Path, unreadable: list[str]) -> list[tuple[str, int]]:
    """(path, size) of every file and link under `folder`, links to folders included;
    folders that cannot be read go to `unreadable`."""
    items = []
    walk = os.walk(folder, onerror=lambda err: unreadable.append(err.filename))  # never into linked folders
    for dirpath, dirnames, filenames in walk:
        for name in filenames:
            path = os.path.join(dirpath, name)
            size = _size(path)
            if size is not None:
                items.append((path, size))
        items += [(os.path.join(dirpath, n), 0) for n in dirnames if os.path.islink(os.path.join(dirpath, n))]
    return sorted(items)


def contents(base: Path) -> tuple[int, int]:
    """(files, bytes) in `base`'s _待删除. Raises Refused if it is a link."""
    trash = _trash(base)
    items = _items(trash, []) if trash else []
    return len(items), sum(size for _, size in items)


def remove(path: str) -> None:
    """Remove a file or a link (a link to a folder is removed, its folder kept)."""
    if sys.platform == "win32" and os.path.islink(path) and os.path.isdir(path):
        os.rmdir(path)  # how Windows removes a directory link
    else:
        os.remove(path)


def _remove_empty_folders(trash: Path) -> None:
    for dirpath, _, _ in os.walk(trash, topdown=False):
        if Path(dirpath) != trash:
            try:
                os.rmdir(dirpath)
            except OSError:  # not empty: something in it could not go
                pass


def purge(
    base: Path, should_stop: Callable[[], bool], report: Callable[[int], None], remove: Remover = remove,
) -> tuple[int, list[Problem], bool]:
    """(files removed, the ones that could not be, whether stopped). Raises Refused if
    _待删除 is a link."""
    trash = _trash(base)
    if trash is None:
        return 0, [], False
    unreadable: list[str] = []
    items = _items(trash, unreadable)
    removed = 0
    problems = [Problem(Path(p).relative_to(base).as_posix(), "读不了这个文件夹，里面的没删") for p in unreadable]
    for done, (path, _) in enumerate(items):
        if should_stop():
            _remove_empty_folders(trash)
            return removed, problems, True
        report(done)
        try:
            remove(path)
            removed += 1
        except OSError as err:
            problems.append(Problem(Path(path).relative_to(base).as_posix(), f"删不掉：{err}"))
    _remove_empty_folders(trash)
    report(len(items))
    return removed, problems, False
