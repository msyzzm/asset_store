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
import os
import re
import shutil
import subprocess
import zipfile
import zlib
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from assetindex import archives, unpack, verify
from assetindex.categories import TO_DELETE
from assetindex.organize_apply import _taken, move_no_replace
from assetindex.scanner import UNPACKING_SUFFIX

CHUNK = 1 << 20
ZIP_LOCKED = 0x1  # general purpose flag bit 0: the member is encrypted
NO_PASSWORD = "none"  # for packs not locked: 7zz must never stop to ask for one
BAD_MEMBER = re.compile(r"^ERROR: (?:CRC Failed|Data Error)(?: in encrypted file\. Wrong password\?)? : (.+)$", re.MULTILINE)
ERROR_COUNT = re.compile(r"Sub items Errors: (\d+)")
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


def _zip_files(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    return [i for i in zf.infolist() if not i.is_dir()]


def _open_zip(src: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(src)
    except zipfile.BadZipFile:
        raise RepairError(f"「{src.name}」的目录读不出来，这份不能用") from None


def _listing(zf: zipfile.ZipFile) -> dict[str, int]:
    return {i.filename: i.file_size for i in _zip_files(zf)}


def _rebuild(copies: Copies, names: list[str], tmp: Path, should_stop, report) -> None:
    for done, name in enumerate(names):
        if should_stop():
            raise Stopped("手动停止了，什么都没改")
        report(done, len(names))
        target = tmp / _safe(archives._zip_name(copies[0][1].getinfo(name)))  # GBK names as such
        target.parent.mkdir(parents=True, exist_ok=True)
        if not _read_whole(copies, name, target):
            raise RepairError(f"「{name}」每一份都坏了，修不了")
    report(len(names), len(names))


def _build(base: Path, srcs: list[Path], tmp: Path, should_stop, report, free) -> None:
    """Every member, whole, under `tmp`; on any failure `tmp` is gone again."""
    with ExitStack() as stack:
        copies = [(src, stack.enter_context(_open_zip(src))) for src in srcs]
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


def _bad_members(said: str) -> list[str] | None:
    """The members 7zz named as damaged, or None unless it named every error it counted."""
    names = sorted({m.rstrip("\r") for m in BAD_MEMBER.findall(said)})
    counted = ERROR_COUNT.search(said)
    if not names or not counted or int(counted.group(1)) != len(names):
        return None
    return names


def _said(done: subprocess.CompletedProcess) -> str:
    return (done.stdout + b"\n" + done.stderr).decode("utf-8", "replace")


def _parts(path: str) -> tuple[str, ...]:
    return PurePosixPath(path.replace("\\", "/")).parts


def _counterpart(name: str, size: int, crc: str, entries: list[tuple[str, int, str]]) -> str | None:
    """The member of another copy that is `name`: same file name, size and CRC (the CRC the
    damaged copy's header expects: another file of the same name and size will not have
    it), and the most folders in common counting from the end (copies may sit under other
    top folders, "太空Pack/Pack/a" and "Pack/Pack/a"). None if there is none, or two as good."""
    want = _parts(name)
    best, best_score, tied = None, 0, False
    for path, their_size, their_crc in entries:
        other = _parts(path)
        if their_size != size or not other or other[-1] != want[-1]:
            continue
        if crc and their_crc and crc != their_crc:
            continue
        score = 0
        while score < min(len(want), len(other)) and want[-1 - score] == other[-1 - score]:
            score += 1
        if score > best_score:
            best, best_score, tied = path, score, False
        elif score == best_score:
            tied = True
    if best is None or tied or best_score < min(2, len(want)):
        return None
    return best


def _take_from_others(srcs: list[Path], names: list[str], expected: dict[str, tuple[int, str]], tmp: Path,
                      password: str, should_stop) -> list[str]:
    """The damaged members taken whole from the other copies into `tmp`, one 7zz run per
    copy, each found there by `_counterpart`; those no copy holds whole. Their scratch
    folder is hidden from the scanner too."""
    scratch = tmp.parent / (tmp.name.removesuffix(UNPACKING_SUFFIX) + ".one" + UNPACKING_SUFFIX)
    left = list(names)
    try:
        for src in srcs:
            if not left:
                break
            if should_stop():
                raise Stopped("手动停止了，什么都没改")
            entries = unpack.entries_7zz(src, password)
            pairs = {n: c for n in left if (c := _counterpart(n, *expected.get(n, (-1, "")), entries))}
            if not pairs:
                continue
            for theirs in pairs.values():
                _safe(theirs)
            shutil.rmtree(scratch, ignore_errors=True)
            scratch.mkdir()
            run = ["x", "-bd", "-y", "-spd", f"-p{password}", f"-o{scratch}", "--", str(src), *pairs.values()]
            done = unpack.run_7zz(run)
            still_bad = set() if done.returncode == 0 else set(_bad_members(_said(done)) or pairs.values())
            for name, theirs in pairs.items():
                if theirs in still_bad or not (scratch / _safe(theirs)).is_file():
                    continue
                target = tmp / _safe(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(scratch / _safe(theirs), target)
                left.remove(name)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return left


def _by_zipfile(src: Path) -> bool:
    """A zip zipfile reads itself: one with locked members still goes to 7zz, which takes
    the password (zipfile has none, and no AES)."""
    if src.suffix.lower() != ".zip":
        return False
    with _open_zip(src) as zf:
        return not any(i.flag_bits & ZIP_LOCKED for i in zf.infolist())


def _first_entries(src: Path, password: str) -> list[tuple[str, int, str]]:
    """(path, size, CRC) of the first copy's files. A zip's come from zipfile, named as the
    indexer names them (GBK unless flagged UTF-8): 7zz on Linux writes such names to disk
    as the raw bytes, not as the names it prints (2026-11-17, Space Creator Pro)."""
    if not _by_zipfile(src):
        return unpack.entries_7zz(src, password)  # also refuses volume sets
    with _open_zip(src) as zf:
        return [(archives._zip_name(i), i.file_size, f"{i.CRC:08X}") for i in _zip_files(zf)]


def _unzip_first(src: Path, tmp: Path, should_stop, report) -> list[str]:
    """Every member of the zip at `src` written under `tmp` by its real name; the names of
    those that did not read whole."""
    bad = []
    with _open_zip(src) as zf:
        infos = _zip_files(zf)
        for done, info in enumerate(infos):
            if should_stop():
                raise Stopped("手动停止了，什么都没改")
            report(done, len(infos))
            name = archives._zip_name(info)
            target = tmp / _safe(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not _read_whole([(src, zf)], info.filename, target):
                bad.append(name)
    return bad


def _unpack_first(src: Path, tmp: Path, password: str) -> list[str]:
    """The copy at `src` unpacked under `tmp` by 7zz, which writes a damaged member too and
    names it; those names."""
    done = unpack.run_7zz(["x", "-bd", "-y", f"-p{password}", f"-o{tmp}", "--", str(src)])
    bad = [] if done.returncode == 0 else _bad_members(_said(done))
    if bad is None:
        raise RepairError(f"第一份解不开：{unpack._said(done, password)}")
    return bad


def _build_7zz(base: Path, srcs: list[Path], tmp: Path, password: str, should_stop, report, free) -> None:
    """The first copy unpacked whole, damaged members too, each of those then taken from
    another copy, of any format. The copies need not list the same files: the first one's
    list is what the result must hold. On any failure `tmp` is gone again."""
    entries = _first_entries(srcs[0], password)
    wanted = {path: (size, crc) for path, size, crc in entries}
    expected = (len(entries), sum(size for _, size, _ in entries))
    if free(base) < expected[1] + unpack.MIN_FREE_BYTES:
        raise RepairError(f"NAS 剩余空间不够：要 {expected[1] / 1e9:.1f} GB，还要留 {unpack.MIN_FREE_BYTES / 1e9:.0f} GB")
    shutil.rmtree(tmp, ignore_errors=True)  # ours, left by a crash
    tmp.mkdir()
    try:
        report(0, expected[0])
        if _by_zipfile(srcs[0]):
            bad = _unzip_first(srcs[0], tmp, should_stop, report)
        else:
            bad = _unpack_first(srcs[0], tmp, password)
        if bad and len(bad) >= expected[0]:
            raise RepairError("每个文件都报错，多半是密码不对")
        for name in bad:
            _safe(name)
        left = _take_from_others(srcs[1:], bad, wanted, tmp, password, should_stop)
        if left:
            raise RepairError(f"「{left[0]}」每一份都坏了，修不了")
        got = unpack._counted(tmp)
        if got != expected:
            raise RepairError(f"拼出来的文件对不上：应有 {expected[0]} 个、{expected[1]} 字节，实际 {got[0]} 个、{got[1]} 字节")
        report(expected[0], expected[0])
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def repair_one(
    base: Path, rels: list[str], companions: dict[str, list[str]], move: Mover = move_no_replace,
    free=unpack.free_bytes, should_stop: Callable[[], bool] = lambda: False,
    report: Callable[[int, int], None] = lambda done, total: None, password: str | None = None,
) -> str:
    """The rebuilt folder's path (the first copy's name without its suffix). Copies are
    zip, 7z or rar, mixed or not (any rar needs `password`); all-zip goes through
    zipfile, the rest through 7zz."""
    srcs = [base / rel for rel in rels]
    suffixes = {src.suffix.lower() for src in srcs}
    if len(srcs) < 2 or any(not src.is_file() for src in srcs) or not suffixes <= {".zip", ".7z", ".rar"}:
        raise RepairError("要两份以上还在的 zip、7z 或 rar")
    folder_rel = rels[0].rpartition(".")[0]
    dest = base / folder_rel
    if _taken(dest):
        raise RepairError(f"「{folder_rel}」已经有了，不往里面写")
    tmp = dest.parent / f".{dest.name}{UNPACKING_SUFFIX}"
    if suffixes == {".zip"}:
        _build(base, srcs, tmp, should_stop, report, free)  # the zips are closed again: they can move
    else:
        if ".rar" in suffixes and not password:
            raise RepairError("修复 rar 要带密码（没加密的包，7-Zip 会忽略它）")
        try:
            _build_7zz(base, srcs, tmp, password or NO_PASSWORD, should_stop, report, free)
        except unpack.UnpackError as err:
            raise RepairError(str(err)) from None
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
