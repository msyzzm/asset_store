"""Rebuilding a pack from copies that are each damaged in a different member (2026-11-17:
SteamPunk Victorian, three 11 GB zips with bad CRCs in different textures). Runs in the
mover as a move job with action "repair" and the copies as `paths`, first one named.

The copies must list the same files. Every member is read from the first copy whose data
matches its CRC (zipfile checks it; methods zipfile cannot decode, like Deflate64, go
through 7zz and a CRC of our own). All of it lands in a hidden folder; only when every
member is whole does that folder (or its one top folder) take the first copy's name. The
first copy's previews move into it; all copies and the other previews go to _待删除/.
"""

import lzma
import shutil
import subprocess
import zipfile
import zlib
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from assetindex import unpack, verify
from assetindex.categories import TO_DELETE
from assetindex.organize_apply import _taken, move_no_replace
from assetindex.scanner import UNPACKING_SUFFIX

CHUNK = 1 << 20
Mover = Callable[[str, str, bool], None]
Copies = list[tuple[Path, zipfile.ZipFile]]


class RepairError(Exception):
    """Nothing was changed."""


class Stopped(RepairError):
    """Stopped by hand; nothing was changed."""


class HalfDone(RepairError):
    """The rebuilt folder is in place; moving the copies or previews away failed."""


def _with_zipfile(zf: zipfile.ZipFile, info: zipfile.ZipInfo, out: BinaryIO) -> bool:
    """True if the member read whole (zipfile raises on a CRC that does not fit). Bad data
    under bzip2 or lzma raises OSError or LZMAError: also just this copy's problem."""
    try:
        src = zf.open(info)
    except (zipfile.BadZipFile, RuntimeError):  # a broken local header, an encrypted member
        return False
    with src:
        while True:
            try:
                chunk = src.read(CHUNK)
            except (zipfile.BadZipFile, zlib.error, EOFError, lzma.LZMAError, OSError):
                return False
            if not chunk:
                return True
            out.write(chunk)  # a full disk is no copy's fault: it stops the repair


def _with_7zz(src: Path, info: zipfile.ZipInfo, out: BinaryIO) -> bool:
    tool = verify.sevenzip()
    if not tool:
        return False
    crc = 0
    cmd = [tool, "e", "-so", "-bd", "-spd", verify.NO_PASSWORD, "--", str(src), info.filename]  # -spd: no wildcards
    with subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        for chunk in iter(lambda: proc.stdout.read(CHUNK), b""):
            crc = zlib.crc32(chunk, crc)
            out.write(chunk)
    return proc.returncode == 0 and crc == info.CRC


def _read_whole(copies: Copies, name: str, dest: Path) -> bool:
    for src, zf in copies:
        info = zf.getinfo(name)
        with open(dest, "wb") as out:
            try:
                whole = _with_zipfile(zf, info, out)
            except NotImplementedError:  # a method zipfile cannot decode
                out.seek(0)
                out.truncate()
                whole = _with_7zz(src, info, out)
        if whole:
            return True
    return False


def _safe(name: str) -> str:
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise RepairError(f"包里有不安全的路径：{name}")
    return str(path)


def _listing(zf: zipfile.ZipFile) -> dict[str, int]:
    return {i.filename: i.file_size for i in zf.infolist() if not i.is_dir()}


def _rebuild(copies: Copies, names: list[str], tmp: Path, should_stop, report) -> None:
    for done, name in enumerate(names):
        if should_stop():
            raise Stopped("手动停止了，什么都没改")
        report(done, len(names))
        target = tmp / _safe(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not _read_whole(copies, name, target):
            raise RepairError(f"「{name}」每一份都坏了，修不了")
    report(len(names), len(names))


def _build(base: Path, srcs: list[Path], tmp: Path, should_stop, report, free) -> None:
    """Every member, whole, under `tmp`; on any failure `tmp` is gone again."""
    with ExitStack() as stack:
        copies = []
        for src in srcs:
            try:
                copies.append((src, stack.enter_context(zipfile.ZipFile(src))))
            except zipfile.BadZipFile:
                raise RepairError(f"「{src.name}」的目录读不出来，这份不能用") from None
        listings = [_listing(zf) for _, zf in copies]
        if any(listing != listings[0] for listing in listings[1:]):
            raise RepairError("几份包里的文件不一样，不能拼成一份")
        names = sorted(listings[0])
        for name in names:
            _safe(name)
        needed = sum(listings[0].values())
        if free(base) < needed + unpack.MIN_FREE_BYTES:
            raise RepairError(f"NAS 剩余空间不够：要 {needed / 1e9:.1f} GB，还要留 {unpack.MIN_FREE_BYTES / 1e9:.0f} GB")
        shutil.rmtree(tmp, ignore_errors=True)  # ours, left by a crash
        tmp.mkdir()
        try:
            _rebuild(copies, names, tmp, should_stop, report)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise


def repair_one(
    base: Path, rels: list[str], companions: dict[str, list[str]], move: Mover = move_no_replace,
    free=unpack.free_bytes, should_stop: Callable[[], bool] = lambda: False,
    report: Callable[[int, int], None] = lambda done, total: None,
) -> str:
    """The rebuilt folder's path (the first copy's name without ".zip")."""
    srcs = [base / rel for rel in rels]
    if len(srcs) < 2 or any(not src.is_file() or src.suffix.lower() != ".zip" for src in srcs):
        raise RepairError("要两份以上还在的 zip")
    folder_rel = rels[0].rpartition(".")[0]
    dest = base / folder_rel
    if _taken(dest):
        raise RepairError(f"「{folder_rel}」已经有了，不往里面写")
    tmp = dest.parent / f".{dest.name}{UNPACKING_SUFFIX}"
    _build(base, srcs, tmp, should_stop, report, free)  # the zips are closed again: they can move
    top = list(tmp.iterdir())
    keep = top[0] if len(top) == 1 and top[0].is_dir() else tmp
    move(str(keep), str(dest), True)
    try:
        if keep != tmp:
            tmp.rmdir()
        _set_aside(base, rels, companions, dest, move)
    except OSError as err:
        raise HalfDone(f"「{folder_rel}」已经修好，但移走副本或预览图时出错，剩下的要手动移：{err!r}") from None
    return folder_rel


def _set_aside(base: Path, rels: list[str], companions: dict[str, list[str]], dest: Path, move: Mover) -> None:
    for companion in companions.get(rels[0], []):  # its previews go inside: the folder shows them
        if (base / companion).is_file():
            move(str(base / companion), str(unpack._free_name(dest, companion.rpartition("/")[2])), False)
    trash = base / TO_DELETE
    trash.mkdir(parents=True, exist_ok=True)
    for rel in rels:
        for path in [rel] + (companions.get(rel, []) if rel != rels[0] else []):
            if (base / path).is_file():
                move(str(base / path), str(unpack._free_name(trash, path.rpartition("/")[2])), False)
