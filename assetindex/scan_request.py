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
STOP_NAME = "scan.stop"  # created by the "停止" button, deleted when the scanner sees it
POLL_SECONDS = 5  # how soon a waiting scanner notices a request


class ScanStopped(Exception):
    """Raised between items once a stop was asked for: what's done so far is kept."""


def never() -> bool:
    """Default `should_stop` for scan stages run outside a scan (tests, the captioner)."""
    return False


def no_progress(phase: str, done: int, total: int | None) -> None:
    """Default `progress` for scan stages: (phase, items done, items in all or None)."""


@dataclass(frozen=True)
class ScanStatus:
    running: bool = False
    started: float | None = None  # epoch seconds
    finished: float | None = None
    ok: bool | None = None  # False if any root was unreadable or the scan was interrupted
    stopped: bool = False  # ended early by the "停止" button
    # While running: which root and step (walk | archives | previews | prune | embed_text |
    # embed_image), and how far; total is None while walking (not known in advance).
    root: str | None = None
    phase: str | None = None
    done: int = 0
    total: int | None = None


def request_stop(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / REQUEST_NAME).unlink(missing_ok=True)  # one not started yet is dropped too
    (data_dir / STOP_NAME).touch()


def stop_requested(data_dir: Path) -> bool:
    return (data_dir / STOP_NAME).exists()


def clear_stop(data_dir: Path) -> None:
    (data_dir / STOP_NAME).unlink(missing_ok=True)


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


def write_json_atomic(target: Path, data: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, target)  # readers never see a half-written file


def write_status(data_dir: Path, status: ScanStatus) -> None:
    write_json_atomic(data_dir / STATUS_NAME, asdict(status))


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
        stopped=raw.get("stopped") is True,
        root=_text_or_none(raw.get("root")),
        phase=_text_or_none(raw.get("phase")),
        done=_count(raw.get("done")) or 0,
        total=_count(raw.get("total")),
    )


def _text_or_none(value) -> str | None:
    return value[:200] if isinstance(value, str) else None


def _count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
