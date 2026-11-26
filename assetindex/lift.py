"""Archives that only wrap another archive (2026-11-17: FightGameSelectCharacter.7z held
FightGameSelectCharacter.zip and two ad links): the inner one takes the outer's place and
name, a picture in it becomes the cover unless one is there already, the rest (links,
readmes) stays in the outer, which goes to _待删除/. Runs as the move job "lift".
"""

import logging
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath

from assetindex import unpack
from assetindex.categories import TO_DELETE
from assetindex.organize_apply import _taken, move_no_replace
from assetindex.scanner import UNPACKING_SUFFIX

log = logging.getLogger(__name__)

ARCHIVES = frozenset({"zip", "7z", "rar"})
PICTURES = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp"})
EXTRAS = PICTURES | {"url", "txt", "html", "htm", "lnk", "website", "nfo", "ini"}  # what a wrapper adds
NO_PASSWORD = "none"  # 7zz must never stop to ask for one
Mover = Callable[[str, str, bool], None]


class LiftError(Exception):
    """This archive stays as it is."""


class LiftHalfDone(LiftError):
    """The NAS changed but not all the way: the message says what is where."""


def _ext(path: str) -> str:
    name = PurePosixPath(path.replace("\\", "/")).name
    return name.rpartition(".")[2].lower() if "." in name else ""


def _members(src: Path, password: str) -> tuple[tuple[str, int], list[str]]:
    """(the inner archive with its size, the pictures) of a wrapper; else LiftError."""
    entries = unpack.entries_7zz(src, password)
    inner = [(path, size) for path, size, _ in entries if _ext(path) in ARCHIVES]
    rest = [path for path, _, _ in entries if _ext(path) not in ARCHIVES]
    if len(inner) != 1 or any(_ext(path) not in EXTRAS for path in rest):
        raise LiftError("不是只套了一个压缩包（里面还有别的素材），没动")
    return inner[0], [path for path in rest if _ext(path) in PICTURES]


def lift_one(base: Path, rel: str, password: str | None, companions: list[str],
             move: Mover = move_no_replace, free=unpack.free_bytes, known: Sequence[str] = ()) -> str:
    """The inner archive's new path: the outer's, with the inner's suffix. With no
    `password`, no password is tried first, then each `known` one (archive_passwords.py)."""
    src = base / rel
    if not src.is_file():
        raise LiftError("压缩包已经不在了（可能已经处理过，等扫描更新）")
    if src.suffix.lower().lstrip(".") not in ARCHIVES or unpack.VOLUME.search(rel):
        raise LiftError("只处理 7z、rar、zip（不处理分卷）")
    if src.suffix.lower() == ".zip" and unpack._has_gbk_names(src):
        raise LiftError("zip 里的中文文件名不是 UTF-8，7-Zip 会解成乱码，没动")
    candidates = [password] if password else [NO_PASSWORD, *known]
    try:
        (inner, size), pictures, tries = _members_with_any(src, candidates)
    except unpack.UnpackError as err:
        raise LiftError(str(err)) from None
    dest = src.with_name(f"{src.stem}.{_ext(inner)}")
    same = dest.name.casefold() == src.name.casefold()  # X.zip in X.zip (or X.ZIP): it takes the outer's very place
    if same:
        dest = src
    elif _taken(dest):
        raise LiftError(f"「{dest.name}」已经有了，没动")
    if free(base) < size + unpack.MIN_FREE_BYTES:
        raise LiftError(f"NAS 剩余空间不够：要 {size / 1e9:.1f} GB，还要留 {unpack.MIN_FREE_BYTES / 1e9:.0f} GB")
    tmp = src.parent / f".{src.name}{UNPACKING_SUFFIX}"  # hidden from the scanner
    shutil.rmtree(tmp, ignore_errors=True)  # ours, left by a crash
    tmp.mkdir()
    keep_tmp = False
    try:
        try:
            _take_out_with_any(src, tmp, inner, size, pictures, tries)
        except unpack.UnpackError as err:  # no 7zz, or it ran too long
            raise LiftError(str(err)) from None
        aside = unpack._free_name(base / TO_DELETE, src.name)
        aside.parent.mkdir(parents=True, exist_ok=True)
        try:
            _swap(src, tmp / inner, dest, aside, same, move)
        except LiftHalfDone:
            keep_tmp = same  # the inner archive is only there now
            raise
        if pictures and not any((base / c).is_file() for c in companions):
            cover = unpack._free_name(dest.parent, f"{src.stem}.{_ext(pictures[0])}")
            try:
                move(str(tmp / pictures[0]), str(cover), False)
            except OSError as err:  # the pack is done; only its cover is missing
                log.warning("cover for %s not placed: %r", dest, err)
    finally:
        if not keep_tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return dest.relative_to(base).as_posix()


def _swap(src: Path, inner: Path, dest: Path, aside: Path, same: bool, move: Mover) -> None:
    """The inner archive in, the outer aside, in the order that leaves the least to mend
    if a move fails: inner first, unless it takes the outer's place. Raises LiftHalfDone
    when the NAS changed but the job did not finish."""
    if not same:
        _first(move, inner, dest)
        try:
            move(str(src), str(aside), False)
        except OSError as err:
            raise LiftHalfDone(f"里面的包已放好（{dest.name}），外层没能移进待删除，要手动移：{err!r}") from None
        return
    _first(move, src, aside)
    try:
        move(str(inner), str(dest), False)
    except OSError as err:
        try:
            move(str(aside), str(src), False)
        except OSError:
            raise LiftHalfDone(f"外层已在待删除（{aside.name}），里面的包没放回，留在 {inner}，要手动恢复：{err!r}") from None
        raise LiftError(f"里面的包没放回，外层已还原，没动：{err!r}") from None


def _first(move: Mover, src: Path, dst: Path) -> None:
    """The first move: if it fails, nothing changed."""
    try:
        move(str(src), str(dst), False)
    except OSError as err:
        raise LiftError(f"移动时出错，没动：{err!r}") from None


def _members_with_any(src: Path, candidates: list[str]) -> tuple[tuple[str, int], list[str], list[str]]:
    """_members by the first password that lists the archive, with the passwords left to
    try from it on (a RAR lists its names with any password; one with locked headers not)."""
    for i, password in enumerate(candidates):
        try:
            return (*_members(src, password), candidates[i:])
        except unpack.UnpackError:
            if i == len(candidates) - 1:
                raise
    raise AssertionError("no candidates")  # lift_one always passes one


def _take_out_with_any(src: Path, tmp: Path, inner: str, size: int, pictures: list[str], tries: list[str]) -> None:
    """_take_out by the first password that extracts; `tmp` is emptied between tries."""
    for path in (inner, *pictures):
        if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            raise LiftError(f"包里有不安全的路径：{path}")
    for i, password in enumerate(tries):
        try:
            _take_out(src, tmp, inner, size, pictures, password)
            return
        except LiftError:
            if i == len(tries) - 1:
                if len(tries) > 1:
                    raise LiftError(f"试了 {len(tries)} 个密码（含不用密码）都取不出里面的压缩包：密码不对，或者包坏了") from None
                raise
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir()


def _take_out(src: Path, tmp: Path, inner: str, size: int, pictures: list[str], password: str) -> None:
    """The inner archive and the pictures, whole, under `tmp`."""
    done = unpack.run_7zz(["x", "-bd", "-y", "-spd", f"-p{password}", f"-o{tmp}", "--", str(src), inner, *pictures])
    got = tmp / inner
    if done.returncode != 0 or not got.is_file() or got.stat().st_size != size:
        raise LiftError(f"取不出里面的压缩包：{unpack._said(done, password)}")
