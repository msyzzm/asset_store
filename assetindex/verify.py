"""Testing archives whole: every member read and checked against its CRC, by 7-Zip
(7zz, in the image for this; it reads RAR too) or else bsdtar (libarchive, there for
previews; it cannot decode some RAR methods). For copies of one pack that have the same
size but not the same bytes: which one is damaged. It only reads; deciding is left to
the user (see the move job's action "verify").

Three outcomes: whole, damaged (the tool found bad data), or not tested (no tool, too
slow, an empty file, a password, a method it cannot decode): never call a copy damaged
because the test itself could not run."""

import re
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
NAME_WARNING = re.compile(r"multibyte or wide character|Pathname can't be converted", re.IGNORECASE)
UNSUPPORTED = re.compile(r"Can't decompress|unsupported|not supported|Unknown (compression|filter)", re.IGNORECASE)
DAMAGE_7Z = re.compile(r"CRC Failed|Data Error|Headers Error|Unexpected end of (archive|data)", re.IGNORECASE)
PASSWORD_7Z = re.compile(r"Wrong password|encrypted", re.IGNORECASE)
NO_PASSWORD = "-pnone"  # never prompt: an encrypted archive fails as "Wrong password"
WINDOWS_TAR = Path(r"C:\Windows\System32\tar.exe")  # bsdtar too; a "tar" on PATH may be GNU tar


class Untested(Exception):
    """The test could not run: says nothing about the archive."""


def sevenzip() -> str | None:
    return shutil.which("7zz")


def bsdtar() -> str | None:
    return shutil.which("bsdtar") or (str(WINDOWS_TAR) if WINDOWS_TAR.is_file() else None)


def _run(cmd: list[str], size: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=BASE_TIMEOUT_SECONDS + size / READ_BYTES_PER_SECOND, check=False)
    except subprocess.TimeoutExpired:
        raise Untested("读了太久，没读完") from None
    except OSError as err:
        raise Untested(f"没法运行 {cmd[0]}：{err}") from None


def _said(done: subprocess.CompletedProcess) -> str:
    text = (done.stdout + b"\n" + done.stderr).decode("utf-8", "replace")
    return "\n".join(line for line in text.splitlines() if line.strip() and not NAME_WARNING.search(line))


def _with_7zz(tool: str, path: Path, size: int) -> str | None:
    done = _run([tool, "t", "-bd", NO_PASSWORD, str(path)], size)
    if done.returncode == 0:
        return None
    said = _said(done)
    damage = [line for line in said.splitlines() if DAMAGE_7Z.search(line)]
    if damage:
        return "\n".join(damage)[-ERROR_CHARS:]
    if PASSWORD_7Z.search(said):
        raise Untested("有密码")
    raise Untested(f"7-Zip 读不了：{said[-ERROR_CHARS:]}")


def _with_bsdtar(tool: str, path: Path, size: int) -> str | None:
    done = _run([tool, "-xOf", str(path)], size)
    said = "\n".join(line for line in done.stderr.decode("utf-8", "replace").splitlines()
                     if line.strip() and not NAME_WARNING.search(line))
    if UNSUPPORTED.search(said):  # e.g. newer RAR methods libarchive cannot decode
        raise Untested(f"bsdtar 读不了这种压缩：{said[-ERROR_CHARS:]}")
    if done.returncode == 0 and not said:  # some versions only warn on a bad CRC: a warning counts
        return None
    return said[-ERROR_CHARS:] or f"bsdtar 退出码 {done.returncode}"


def test_archive(path: Path) -> str | None:
    """None if every member reads back with a matching CRC, else what the tool said.
    Raises Untested when it cannot tell."""
    size = path.stat().st_size
    if size == 0:
        raise Untested("空文件")
    tool = sevenzip()
    if tool:
        return _with_7zz(tool, path, size)
    tool = bsdtar()
    if tool:
        return _with_bsdtar(tool, path, size)
    raise Untested("没有 7zz 或 bsdtar")


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
