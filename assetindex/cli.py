"""Command line: `python -m assetindex scan|serve`."""

import argparse
import logging
import signal
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

from assetindex import attempts, caption_job, organize, scan_request
from assetindex.archive_index import index_archives
from assetindex.archive_previews import build_previews
from assetindex.cache_prune import prune_thumbs
from assetindex.caption import load_config as load_caption_config
from assetindex.check import run_checks
from assetindex.config import Settings, load_settings
from assetindex.db import connect
from assetindex.embed_stage import embed_pending
from assetindex.embedding import load_config
from assetindex.extract import find_bsdtar
from assetindex.scan_request import ScanStatus
from assetindex.scanlock import ScanLocked, scan_lock
from assetindex.scanner import scan_root

log = logging.getLogger("assetindex")

# The web server takes the scan lock for an instant to see whether a scan is running.
LOCK_WAIT_SECONDS = 2
# A web request never starts a scan sooner than this after the previous one ended.
MIN_SCAN_GAP_SECONDS = 60
# How soon the captioner notices a job started from the web UI.
CAPTION_POLL_SECONDS = 3


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
        with scan_lock(settings.data_dir, wait_seconds=LOCK_WAIT_SECONDS):
            started = time.time()
            _set_status(settings.data_dir, ScanStatus(running=True, started=started))
            # Only now: had the lock been busy, the request would stay for the next round.
            scan_request.take_request(settings.data_dir)
            ok = False  # stays False if the scan is interrupted
            conn = connect(settings.db_path)
            try:
                if retry_errors:
                    attempts.clear(conn)
                results = [_scan_one(conn, settings, name, path, tool, retry_errors) for name, path in roots.items()]
                pruned = prune_thumbs(conn, settings.thumb_dir, settings.thumb_cache_bytes)
                log.info("thumbnail cache: removed %d files, %.1f MB", pruned.removed, pruned.freed_bytes / 1024**2)
                _embed(conn, replace(settings, roots=roots), retry_errors)  # only the roots scanned
                ok = all(results)
            finally:
                conn.close()
                _set_status(settings.data_dir, ScanStatus(started=started, finished=time.time(), ok=ok))
    except ScanLocked as err:
        log.error("%s; not starting another", err)
        return 3
    return 0 if ok else 1


def _embed(conn: sqlite3.Connection, settings: Settings, retry_errors: bool) -> None:
    """Semantic search vectors, if switched on in the web UI. An unreachable API only
    postpones them to the next scan; it doesn't fail this one."""
    config = load_config(settings.data_dir)
    if not config.active:
        return
    started = time.monotonic()
    try:
        stats = embed_pending(conn, settings, config, retry_errors=retry_errors)
    except Exception:  # e.g. "database is locked": vectors are extra, the scan itself worked
        log.exception("embedding failed, will try again next scan")
        return
    log.info(
        "embeddings (%s) in %.1fs: text %d, image %d, rejected %d",
        config.model, time.monotonic() - started, stats.texts, stats.images, stats.rejected,
    )
    if stats.error:
        log.warning("embedding stopped early, will continue next scan: %s", stats.error)


def _set_status(data_dir: Path, status: ScanStatus) -> None:
    """Status is only for the web UI: failing to write it must not stop or mask a scan."""
    try:
        scan_request.write_status(data_dir, status)
    except OSError as err:
        log.warning("cannot write scan status: %r", err)


def _exit_on_sigterm() -> None:
    """Turn SIGTERM (docker stop) into SystemExit. Python's default dies on the spot,
    skipping `finally`, so an interrupted archive would count as one that crashed us."""

    def handler(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handler)


def _scan_every(only_root: str | None, retry_errors: bool, seconds: int) -> int:
    """Scan, wait, repeat: the container's scheduler. A scan requested from the web UI
    ends the wait early. Stops on Ctrl-C."""
    data_dir = load_settings().data_dir
    try:
        while True:
            try:
                _scan(only_root, retry_errors)
            except Exception:  # e.g. "database is locked": log, keep the schedule going
                log.exception("scan round failed; next try in %ds", seconds)
            retry_errors = False  # a one-off request, not every round
            if scan_request.wait_for_request(data_dir, seconds, min_seconds=MIN_SCAN_GAP_SECONDS):
                log.info("scan requested from the web UI")
    except KeyboardInterrupt:
        return 0


def _run_caption(settings: Settings, raw) -> None:
    """One describe-pictures job, holding the caption lock. Its progress goes to
    caption.status for the web UI."""
    data_dir = settings.data_dir

    def report(status: caption_job.CaptionStatus) -> None:
        try:
            caption_job.write_status(data_dir, status)
        except OSError as err:
            log.warning("cannot write caption status: %r", err)

    try:
        scope = caption_job.parse_scope(raw, settings.roots)
    except ValueError as err:
        report(caption_job.CaptionStatus(error=f"范围不对：{err}", finished=time.time()))
        return
    config = load_caption_config(data_dir)
    if not config.active:
        report(caption_job.CaptionStatus(error="看图描述还没设置好接口", finished=time.time()))
        return
    log.info("describing pictures in %s/%s%s", scope.root, scope.prefix, " (redo)" if scope.redo else "")
    # Show "running" before counting the pictures, so the web UI can't queue a second job meanwhile.
    report(caption_job.CaptionStatus(running=True, root=scope.root, prefix=scope.prefix, started=time.time()))
    conn = connect(settings.db_path)
    try:
        final = caption_job.run_job(
            conn, settings, scope, config,
            should_stop=lambda: caption_job.stop_requested(data_dir),
            report=report,
            after_batch=lambda: _embed(conn, settings, retry_errors=False),
        )
    except Exception as err:  # e.g. "database is locked": say so instead of "running" forever
        log.exception("describing pictures failed")
        report(caption_job.CaptionStatus(root=scope.root, prefix=scope.prefix, error=f"出错中断：{err!r}", finished=time.time()))
        return
    finally:
        conn.close()
    report(final)
    log.info("described %d of %d pictures, %d failed%s", final.done, final.total, final.failed,
             f"; stopped: {final.error}" if final.error else " (stopped from the web UI)" if final.stopped else "")


def _caption_once(settings: Settings) -> bool:
    """Run the job requested from the web UI, if any; True if one ran."""
    if not caption_job.is_requested(settings.data_dir):
        return False
    try:
        with scan_lock(settings.data_dir, wait_seconds=LOCK_WAIT_SECONDS, name=caption_job.LOCK_NAME):
            raw = caption_job.take_job(settings.data_dir)
            if raw is None:
                return False
            _run_caption(settings, raw)
    except ScanLocked as err:
        log.error("%s; not starting another", err)
        return False
    return True


def _caption_watch() -> int:
    """The captioner container: wait for jobs started from the web UI."""
    settings = load_settings()
    try:
        while True:
            try:
                _caption_once(settings)
            except Exception:
                log.exception("caption job failed")
            try:
                _organize_once(settings)
            except Exception:
                log.exception("organize job failed")
            time.sleep(CAPTION_POLL_SECONDS)
    except KeyboardInterrupt:
        return 0


def _run_organize(settings: Settings, raw) -> None:
    """One organize analysis (units, hashes, duplicates, proposals), holding the model lock."""
    data_dir = settings.data_dir

    def report(status: organize.OrganizeStatus) -> None:
        try:
            organize.write_status(data_dir, status)
        except OSError as err:
            log.warning("cannot write organize status: %r", err)

    try:
        scope = caption_job.parse_scope(raw, settings.roots)
    except ValueError as err:
        report(organize.OrganizeStatus(error=f"范围不对：{err}", finished=time.time()))
        return
    config = load_caption_config(data_dir)
    if not config.active:
        report(organize.OrganizeStatus(error="整理要用看图模型：先在「看图描述」里设置好接口", finished=time.time()))
        return
    log.info("organizing %s/%s", scope.root, scope.prefix)
    report(organize.OrganizeStatus(running=True, root=scope.root, prefix=scope.prefix, phase="units", started=time.time()))
    conn = connect(settings.db_path)
    try:
        final = organize.run_analysis(
            conn, settings, scope, config, should_stop=lambda: organize.stop_requested(data_dir), report=report
        )
    except Exception as err:  # e.g. "database is locked": say so instead of "running" forever
        log.exception("organizing failed")
        report(organize.OrganizeStatus(root=scope.root, prefix=scope.prefix, error=f"出错中断：{err!r}", finished=time.time()))
        return
    finally:
        conn.close()
    report(final)
    log.info("organize: proposed %d of %d packs, %d failed%s", final.done, final.total, final.failed,
             f"; stopped: {final.error}" if final.error else "")


def _organize_once(settings: Settings) -> bool:
    """Run the analysis requested from the web UI, if any; True if one ran."""
    if not organize.is_requested(settings.data_dir):
        return False
    try:
        with scan_lock(settings.data_dir, wait_seconds=LOCK_WAIT_SECONDS, name=organize.LOCK_NAME):
            raw = organize.take_job(settings.data_dir)
            if raw is None:
                return False
            _run_organize(settings, raw)
    except ScanLocked:
        return False  # a caption job holds the model: try again next round
    return True


def _organize_now(root: str, prefix: str, redo: bool) -> int:
    settings = load_settings()
    try:
        with scan_lock(settings.data_dir, name=organize.LOCK_NAME):
            (settings.data_dir / organize.STOP_NAME).unlink(missing_ok=True)
            _run_organize(settings, {"root": root, "prefix": prefix, "redo": redo})
    except ScanLocked as err:
        log.error("%s; not starting another", err)
        return 3
    return 1 if organize.read_status(settings.data_dir).error else 0


def _caption_now(root: str, prefix: str, redo: bool) -> int:
    settings = load_settings()
    try:
        with scan_lock(settings.data_dir, name=caption_job.LOCK_NAME):
            (settings.data_dir / caption_job.STOP_NAME).unlink(missing_ok=True)  # a stale stop from the web UI
            _run_caption(settings, {"root": root, "prefix": prefix, "redo": redo})
    except ScanLocked as err:
        log.error("%s; not starting another", err)
        return 3
    status = caption_job.read_status(settings.data_dir)
    return 1 if status.error else 0


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
    describe = sub.add_parser("caption", help="describe pictures with the vision model set up in the web UI")
    describe.add_argument("--watch", action="store_true", help="wait for jobs started from the web UI")
    describe.add_argument("--root", help="describe pictures under this root now")
    describe.add_argument("--prefix", default="", help="only this folder under the root")
    describe.add_argument("--redo", action="store_true", help="also pictures that already have a description")
    tidy = sub.add_parser("organize", help="find duplicate packs and propose where each pack belongs (moves nothing)")
    tidy.add_argument("--root", required=True)
    tidy.add_argument("--prefix", default="", help="only propose for packs in this folder")
    tidy.add_argument("--redo", action="store_true", help="also packs that already have an undecided proposal")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.cmd == "scan":
        _exit_on_sigterm()
        if args.every:
            return _scan_every(args.root, args.retry_errors, args.every)
        return _scan(args.root, args.retry_errors)
    if args.cmd == "check":
        return _check()
    if args.cmd == "caption":
        _exit_on_sigterm()
        if args.watch:
            return _caption_watch()
        if not args.root:
            parser.error("caption needs --watch or --root")
        return _caption_now(args.root, args.prefix, args.redo)
    if args.cmd == "organize":
        _exit_on_sigterm()
        return _organize_now(args.root, args.prefix, args.redo)
    return _serve(args.host, args.port)
