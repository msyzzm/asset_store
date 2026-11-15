"""Let the web UI ask the scheduled scanner to scan now, and show how scans are going.

The web server and the scanner run as separate processes (separate containers) that
share only the data dir, so they talk through two small files in it: an empty request
file the web server creates and the scanner deletes, and a JSON status file the scanner
writes and the web server reads.
"""

import json
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

REQUEST_NAME = "scan.request"
STATUS_NAME = "scan.status"
POLL_SECONDS = 5  # how soon a waiting scanner notices a request


@dataclass(frozen=True)
class ScanStatus:
    running: bool = False
    started: float | None = None  # epoch seconds
    finished: float | None = None
    ok: bool | None = None  # False if any root was unreadable or the scan was interrupted


def request_scan(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / REQUEST_NAME).touch()


def is_requested(data_dir: Path) -> bool:
    return (data_dir / REQUEST_NAME).exists()


def take_request(data_dir: Path) -> bool:
    """Clear the request; True if there was one."""
    try:
        (data_dir / REQUEST_NAME).unlink()
    except FileNotFoundError:
        return False
    return True


def wait_for_request(
    data_dir: Path,
    seconds: float,
    min_seconds: float = 0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Sleep up to `seconds`; return True once a scan is requested, but never before
    `min_seconds`, so pressing the button over and over can't keep the NAS busy."""
    start = clock()
    while True:
        elapsed = clock() - start
        requested = is_requested(data_dir)
        if requested and elapsed >= min_seconds:
            return True
        if elapsed >= seconds:
            return False
        until = min_seconds if requested else seconds
        sleep(min(POLL_SECONDS, until - elapsed))


def write_status(data_dir: Path, status: ScanStatus) -> None:
    target = data_dir / STATUS_NAME
    tmp = target.with_name(f"{STATUS_NAME}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(asdict(status)), encoding="utf-8")
    os.replace(tmp, target)  # readers never see a half-written file


def _number_or_none(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def read_status(data_dir: Path) -> ScanStatus:
    """The last status written; a fresh ScanStatus if none, or if it can't be parsed."""
    try:
        raw = json.loads((data_dir / STATUS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ScanStatus()
    if not isinstance(raw, dict) or not isinstance(raw.get("running"), bool):
        return ScanStatus()
    ok = raw.get("ok")
    return ScanStatus(
        running=raw["running"],
        started=_number_or_none(raw.get("started")),
        finished=_number_or_none(raw.get("finished")),
        ok=ok if isinstance(ok, bool) else None,
    )
