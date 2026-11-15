"""Command line: `python -m assetindex scan|serve`."""

import argparse
import logging
import signal
import sqlite3
import time
from pathlib import Path

from assetindex import attempts
from assetindex.archive_index import index_archives
from assetindex.archive_previews import build_previews
from assetindex.cache_prune import prune_thumbs
from assetindex.check import run_checks
from assetindex.config import Settings, load_settings
from assetindex.db import connect
from assetindex.extract import find_bsdtar
from assetindex.scanlock import ScanLocked, scan_lock
from assetindex.scanner import scan_root

log = logging.getLogger("assetindex")


def _scan_one(
    conn: sqlite3.Connection, settings: Settings, name: str, path: Path, tool: str | None, retry_errors: bool
) -> bool:
    """Index one root; False if it couldn't be read at all."""
    started = time.monotonic()
    try:
        stats = scan_root(conn, name, path)
    except OSError as err:
        # e.g. NAS not mounted; keep going so other roots still get indexed.
        log.error("scan of %s (%s) failed: %s", name, path, err)
        return False
    log.info(
        "scanned %s (%s) in %.1fs: +%d ~%d -%d =%d%s",
        name, path, time.monotonic() - started,
        stats.added, stats.updated, stats.removed, stats.unchanged,
        " (stale entries kept: read errors or empty root)" if stats.deletion_skipped else "",
    )
    started = time.monotonic()
    archived = index_archives(conn, name, path, retry_errors=retry_errors)
    log.info(
        "archives in %s listed in %.1fs: new/changed %d, bad %d, unreadable %d, unchanged %d",
        name, time.monotonic() - started,
        archived.listed, archived.failed, archived.unreadable, archived.unchanged,
    )
    if retry_errors:
        with conn:
            conn.execute(
                "UPDATE archives SET preview_state = NULL WHERE preview_state = 0"
                " AND file_id IN (SELECT id FROM files WHERE root = ?)",
                (name,),
            )
    started = time.monotonic()
    previews = build_previews(conn, name, path, settings.thumb_dir, tool)
    log.info(
        "archive previews in %s built in %.1fs: sheets %d, nothing to show %d, pending %d",
        name, time.monotonic() - started, previews.built, previews.empty, previews.skipped,
    )
    return True


def _scan(only_root: str | None, retry_errors: bool = False) -> int:
    settings = load_settings()
    roots = {k: v for k, v in settings.roots.items() if only_root in (None, k)}
    if not roots:
        log.error("no root named %r; configured: %s", only_root, ", ".join(settings.roots))
        return 2
    tool = find_bsdtar()
    if tool is None:
        log.warning("bsdtar not found: zip/rar/7z previews are skipped until it is installed")
    try:
        with scan_lock(settings.data_dir):
            conn = connect(settings.db_path)
            try:
                if retry_errors:
                    attempts.clear(conn)
                ok = [_scan_one(conn, settings, name, path, tool, retry_errors) for name, path in roots.items()]
                pruned = prune_thumbs(conn, settings.thumb_dir, settings.thumb_cache_bytes)
                log.info("thumbnail cache: removed %d files, %.1f MB", pruned.removed, pruned.freed_bytes / 1024**2)
            finally:
                conn.close()
    except ScanLocked as err:
        log.error("%s; not starting another", err)
        return 3
    return 0 if all(ok) else 1


def _exit_on_sigterm() -> None:
    """Turn SIGTERM (docker stop) into SystemExit. Python's default dies on the spot,
    skipping `finally`, so an interrupted archive would count as one that crashed us."""

    def handler(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handler)


def _scan_every(only_root: str | None, retry_errors: bool, seconds: int) -> int:
    """Scan, sleep, repeat: the container's scheduler. Stops on Ctrl-C."""
    try:
        while True:
            try:
                _scan(only_root, retry_errors)
            except Exception:  # e.g. "database is locked": log, keep the schedule going
                log.exception("scan round failed; next try in %ds", seconds)
            retry_errors = False  # a one-off request, not every round
            time.sleep(seconds)
    except KeyboardInterrupt:
        return 0


def _positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number of seconds, got {value}")
    return value


def _check() -> int:
    results = run_checks(load_settings())
    for r in results:
        print(f"{'OK  ' if r.ok else 'FAIL'}  {r.name}: {r.detail}")
    return 0 if all(r.ok for r in results) else 1


def _serve(host: str, port: int) -> int:
    import uvicorn

    from assetindex.web import create_app

    uvicorn.run(create_app(load_settings()), host=host, port=port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="assetindex")
    sub = parser.add_subparsers(dest="cmd", required=True)
    scan = sub.add_parser("scan", help="index files under the configured roots")
    scan.add_argument("--root", help="scan only this root name")
    scan.add_argument(
        "--retry-errors", action="store_true", help="relist unreadable archives and retry archives without a preview"
    )
    scan.add_argument("--every", type=_positive_int, metavar="SECONDS", help="keep scanning, pausing this long between runs")
    serve = sub.add_parser("serve", help="run the web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("check", help="verify the deployment (roots, data dir, bsdtar, viewer files)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.cmd == "scan":
        _exit_on_sigterm()
        if args.every:
            return _scan_every(args.root, args.retry_errors, args.every)
        return _scan(args.root, args.retry_errors)
    if args.cmd == "check":
        return _check()
    return _serve(args.host, args.port)
