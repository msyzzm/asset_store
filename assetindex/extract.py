"""Read one file out of an archive (zip, rar, 7z) with bsdtar in a subprocess.

A subprocess, not an in-process parser: a hostile archive can at worst hang or bloat
bsdtar, which is killed on timeout, never the web server. Only bsdtar (libarchive) is
accepted: GNU tar has options that run commands and can't read rar or 7z.
"""

import functools
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

EXTRACT_TIMEOUT_SECONDS = 30
VERSION_TIMEOUT_SECONDS = 10
# bsdtar matches operands as shell globs; escape so names like "a[1].png" match literally.
_GLOB_CHARS = re.compile(r"([\\\[\]*?])")
_WINDOWS_TAR = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ExtractError(Exception):
    """The member couldn't be extracted (missing, too large, corrupt)."""


class ExtractTimeout(ExtractError):
    """bsdtar was killed for taking too long: transient, worth retrying later."""


def _candidates() -> list[str]:
    found = [os.environ.get("ASSET_BSDTAR"), shutil.which("bsdtar")]
    if sys.platform == "win32":
        found.append(str(_WINDOWS_TAR))  # Windows 10+ ships libarchive's bsdtar as tar.exe
    return [c for c in found if c]


def _version(tool: str) -> str:
    try:
        result = subprocess.run(
            [tool, "--version"], capture_output=True, stdin=subprocess.DEVNULL,
            timeout=VERSION_TIMEOUT_SECONDS, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.decode(errors="replace")


@functools.cache
def find_bsdtar() -> str | None:
    """Path of a usable bsdtar, or None (archive previews are then skipped)."""
    for candidate in _candidates():
        if "bsdtar" in _version(candidate):
            return candidate
    return None


def _env() -> dict[str, str]:
    env = dict(os.environ)
    if sys.platform != "win32":
        env["LC_ALL"] = "C.UTF-8"  # so libarchive matches non-ASCII member names
    return env


def read_member(
    archive: Path, member: str, max_bytes: int, tool: str, timeout: float = EXTRACT_TIMEOUT_SECONDS
) -> bytes:
    """Return the bytes of one archive member, or raise ExtractError."""
    pattern = _GLOB_CHARS.sub(r"\\\1", member)
    try:
        proc = subprocess.Popen(
            [tool, "-xOf", str(archive), "--", pattern],  # "--": a member named "-x" is not an option
            stdin=subprocess.DEVNULL,  # encrypted archives may prompt for a password
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_env(),
            creationflags=_NO_WINDOW,
        )
    except OSError as err:
        raise ExtractError(f"cannot run bsdtar: {err}") from err
    timed_out = threading.Event()

    def kill_on_timeout() -> None:
        timed_out.set()
        proc.kill()

    timer = threading.Timer(timeout, kill_on_timeout)
    timer.start()
    try:
        data = proc.stdout.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ExtractError(f"member larger than {max_bytes} bytes")
        code = proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
    # A zero exit means bsdtar finished on its own even if the timer fired just after.
    if timed_out.is_set() and code != 0:
        raise ExtractTimeout(f"bsdtar timed out after {timeout}s")
    # A non-zero exit alone isn't fatal: bsdtar exits 1 when *other* entries have
    # unreadable names, yet still streams the wanted member. The caller validates the bytes.
    if not data:
        raise ExtractError(f"bsdtar produced no data (exit {code})")
    return data
