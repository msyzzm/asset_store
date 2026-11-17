"""Testing archives whole: every member read and checked against its CRC by bsdtar
(libarchive, in the image for previews). For copies of one pack that have the same size
but not the same bytes: which one is damaged. It only reads; deciding is left to the
user (see the move job's action "verify").

Three outcomes: whole, damaged (bsdtar complained), or not tested (no bsdtar, too slow,
an empty file): never call a copy damaged because the test itself could not run."""

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from assetindex.organize_move import Problem

WHOLE = "完好"
MISSING = "找不到这个文件"
DAMAGED = "损坏："
UNTESTED = "无法校验："
BASE_TIMEOUT_SECONDS = 1800
READ_BYTES_PER_SECOND = 5_000_000  # slow NFS: a 5 GB archive gets ~47 minutes
ERROR_CHARS = 200
WINDOWS_TAR = Path(r"C:\Windows\System32\tar.exe")  # bsdtar too; a "tar" on PATH may be GNU tar


class Untested(Exception):
    """The test could not run: says nothing about the archive."""


def bsdtar() -> str | None:
    return shutil.which("bsdtar") or (str(WINDOWS_TAR) if WINDOWS_TAR.is_file() else None)


def test_archive(path: Path) -> str | None:
    """None if every member reads back with a matching CRC, else what bsdtar said.
    Raises Untested when it cannot tell."""
    tar = bsdtar()
    if tar is None:
        raise Untested("没有 bsdtar（libarchive-tools）")
    size = path.stat().st_size
    if size == 0:
        raise Untested("空文件")
    try:
        done = subprocess.run([tar, "-xOf", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              timeout=BASE_TIMEOUT_SECONDS + size / READ_BYTES_PER_SECOND, check=False)
    except subprocess.TimeoutExpired:
        raise Untested("读了太久，没读完") from None
    except OSError as err:
        raise Untested(f"没法运行 bsdtar：{err}") from None
    said = done.stderr.decode("utf-8", "replace").strip()
    if done.returncode == 0 and not said:  # some versions only warn on a bad CRC: a warning counts
        return None
    return said[-ERROR_CHARS:] or f"bsdtar 退出码 {done.returncode}"


def verify_all(
    base: Path, paths: list[str], should_stop: Callable[[], bool], report: Callable[[int], None],
    test: Callable[[Path], str | None] = test_archive,
) -> tuple[list[Problem], int, bool]:
    """(a result per path, how many are whole, whether stopped)."""
    results, whole = [], 0
    for done, rel in enumerate(paths):
        if should_stop():
            return results, whole, True
        report(done)
        path = base / rel
        if not path.is_file():
            results.append(Problem(rel, MISSING))
            continue
        try:
            error = test(path)
        except Untested as err:
            results.append(Problem(rel, f"{UNTESTED}{err}"))
            continue
        whole += error is None
        results.append(Problem(rel, WHOLE if error is None else f"{DAMAGED}{error}"))
    report(len(paths))
    return results, whole, False
