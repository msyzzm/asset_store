"""Command line: `python -m assetindex scan|serve`."""

import argparse
import logging
import signal
import sqlite3
import subprocess
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from assetindex import (aria2, attempts, caption_job, downloads, ext_keys, fetch_job, move_job, organize, organize_apply,
                        scan_request, tags)
from assetindex.caption import CaptionClient
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
from assetindex.scan_request import ScanStatus, ScanStopped
from assetindex.scanlock import ScanLocked, is_scan_running, scan_lock
from assetindex.scanner import scan_root

log = logging.getLogger("assetindex")

# The web server takes the scan lock for an instant to see whether a scan is running.
LOCK_WAIT_SECONDS = 2
# A web request never starts a scan sooner than this after the previous one ended.
MIN_SCAN_GAP_SECONDS = 60
# How soon the captioner notices a job started from the web UI.
CAPTION_POLL_SECONDS = 3
MOUNT_EVERY_SECONDS = 90
FETCH_POLL_SECONDS = 2  # how often the fetcher looks at the queue and asks aria2 how far it got
ARIA2_STOP_SECONDS = 5  # within docker stop's 10 s
# A running scan rewrites scan.status at most this often per step (plus at each new step).
PROGRESS_SECONDS = 1.0


class _Progress:
    """Shows the web UI which root and step a scan is on, and how far it got."""

    def __init__(self, data_dir: Path, started: float) -> None:
        self.data_dir = data_dir
        self.started = started
        self.root: str | None = None  # None for the steps that cover every root
        self._phase: str | None = None
        self._phase_started: float | None = None
        self._last = 0.0

    def __call__(self, phase: str, done: int, total: int | None) -> None:
        now = time.monotonic()
        if phase == self._phase and now - self._last < PROGRESS_SECONDS:
            return
        if phase != self._phase:
            self._phase_started = time.time()
        self._phase, self._last = phase, now
        _set_status(self.data_dir, ScanStatus(
            running=True, started=self.started, root=self.root, phase=phase, done=done, total=total,
            phase_started=self._phase_started,
        ))


def _scan_one(
    conn: sqlite3.Connection,
    settings: Settings,
    name: str,
    path: Path,
    tool: str | None,
    retry_errors: bool,
    should_stop: Callable[[], bool] = scan_request.never,
    progress: Callable[[str, int, int | None], None] = scan_request.no_progress,
) -> bool:
    """Index one root; False if it couldn't be read at all. Raises ScanStopped."""
    started = time.monotonic()
    try:
        stats = scan_root(conn, name, path, should_stop, progress)
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
    archived = index_archives(conn, name, path, retry_errors=retry_errors, should_stop=should_stop, progress=progress)
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
    previews = build_previews(conn, name, path, settings.thumb_dir, tool, should_stop=should_stop, progress=progress)
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
            data_dir = settings.data_dir
            scan_request.clear_stop(data_dir)  # pressed while idle: not meant for this scan
            started = time.time()
            _set_status(data_dir, ScanStatus(running=True, started=started))
            # Only now: had the lock been busy, the request would stay for the next round.
            scan_request.take_request(data_dir)

            def should_stop() -> bool:
                return scan_request.stop_requested(data_dir)

            ok = stopped = False  # ok stays False if the scan is interrupted
            progress = _Progress(data_dir, started)
            conn = connect(settings.db_path)
            try:
                if retry_errors:
                    attempts.clear(conn)
                left = _settle_moves(conn, settings)
                if left:  # a scan would drop the moved files' rows, captions and previews with them
                    log.error("moves left half done, not scanning until they are settled on the organize page: %s", left)
                    return 1
                results = []
                for name, path in roots.items():
                    progress.root = name
                    results.append(_scan_one(conn, settings, name, path, tool, retry_errors, should_stop, progress))
                progress.root = None
                progress("prune", 0, None)
                pruned = prune_thumbs(conn, settings.thumb_dir, settings.thumb_cache_bytes)
                log.info("thumbnail cache: removed %d files, %.1f MB", pruned.removed, pruned.freed_bytes / 1024**2)
                # Only the roots scanned.
                _embed(conn, replace(settings, roots=roots), retry_errors, should_stop, progress)
                ok = all(results)
            except ScanStopped as err:
                stopped = True
                log.info("scan stopped from the web UI (%s); the rest waits for the next round", err)
            finally:
                conn.close()
                scan_request.clear_stop(data_dir)
                _set_status(data_dir, ScanStatus(started=started, finished=time.time(), ok=ok, stopped=stopped))
    except ScanLocked as err:
        log.error("%s; not starting another", err)
        return 3
    return 0 if ok or stopped else 1


def _settle_moves(conn: sqlite3.Connection, settings: Settings) -> list[str]:
    """Finish the index of moves a crash cut short, as far as a read-only scanner can
    (all of a pack's renames done); the packs still half moved."""
    left = []
    for journal in organize_apply.unsettled(settings.data_dir):
        root = organize_apply.journal_root(journal)
        if root not in settings.roots:  # a root taken out of the settings must not block every scan
            log.warning("move journal %s is for root %r, no longer configured: left as it is", journal.name, root)
            continue
        problems = organize_apply.recover(conn, settings.roots[root].resolve(), root, journal, move=None)
        left += [f"{journal.name}: {p.path}: {p.reason}" for p in problems]
    return left


def _embed(
    conn: sqlite3.Connection,
    settings: Settings,
    retry_errors: bool,
    should_stop: Callable[[], bool] = scan_request.never,
    progress: Callable[[str, int, int | None], None] = scan_request.no_progress,
) -> None:
    """Semantic search vectors, if switched on in the web UI. An unreachable API only
    postpones them to the next scan; it doesn't fail this one. Raises ScanStopped."""
    config = load_config(settings.data_dir)
    if not config.active:
        return
    started = time.monotonic()
    try:
        stats = embed_pending(
            conn, settings, config, retry_errors=retry_errors, should_stop=should_stop, progress=progress
        )
    except ScanStopped:
        raise
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


def _first_wait(data_dir: Path, seconds: int) -> float:
    """How long a freshly started scheduler waits: starting up (an update, a reboot) never
    scans by itself. The schedule goes on from the last scan; if that is overdue or
    unknown, it starts over from now."""
    last = scan_request.read_status(data_dir)
    ended = last.finished or last.started
    if ended is None:
        return seconds
    remaining = ended + seconds - time.time()
    return remaining if 0 < remaining <= seconds else seconds


def _scan_every(only_root: str | None, retry_errors: bool, seconds: int) -> int:
    """Wait, scan, repeat: the container's scheduler. A scan requested from the web UI
    ends the wait early. Stops on Ctrl-C."""
    data_dir = load_settings().data_dir
    wait = _first_wait(data_dir, seconds)
    log.info("next scheduled scan in %.0f min (or when asked from the web UI)", wait / 60)
    try:
        while True:
            if scan_request.wait_for_request(data_dir, wait, min_seconds=MIN_SCAN_GAP_SECONDS):
                log.info("scan requested from the web UI")
            try:
                _scan(only_root, retry_errors)
            except Exception:  # e.g. "database is locked": log, keep the schedule going
                log.exception("scan round failed; next try in %ds", seconds)
            retry_errors = False  # a one-off request, not every round
            wait = seconds
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
            try:
                _tags_once(settings)
            except Exception:
                log.exception("tags job failed")
            time.sleep(CAPTION_POLL_SECONDS)
    except KeyboardInterrupt:
        return 0


def _move_watch() -> int:
    """The mover service: waits for move jobs from the web UI."""
    settings = load_settings()
    log.info("mover waiting for move jobs from the web UI")
    last_mount = 0.0
    try:
        while True:
            if time.time() - last_mount > MOUNT_EVERY_SECONDS:  # also a heartbeat: the page knows a mover runs
                try:
                    move_job.write_mount(settings.data_dir, {n: move_job.writable(p) for n, p in settings.roots.items()})
                except OSError as err:
                    log.warning("cannot write move.mount: %r", err)
                last_mount = time.time()
            try:
                _move_once(settings)
            except Exception:
                log.exception("move job failed")
            time.sleep(CAPTION_POLL_SECONDS)
    except KeyboardInterrupt:
        return 0


def _fetch_watch() -> int:
    """The fetcher service: runs aria2c and works through the download queue."""
    settings = load_settings()
    binary = aria2.find_aria2c()
    if binary is None:
        log.error("aria2c not found: downloads cannot run (rebuild the image)")
    secret = aria2.new_secret()
    ext_keys.use(settings.data_dir)  # sites' keys: Sketchfab links, Poly Pizza files
    process = aria2.start_daemon(binary, secret) if binary else None
    conn = connect(settings.db_path)
    fetcher = fetch_job.Fetcher(
        conn, settings, aria2.Aria2(secret=secret),
        mover_busy=lambda: is_scan_running(settings.data_dir, move_job.LOCK_NAME),
    )
    log.info("fetcher waiting for downloads from the web UI")
    last_mount = 0.0
    try:
        while True:
            if process is not None and process.poll() is not None:
                log.warning("aria2c exited (%s): starting it again", process.returncode)
                process = aria2.start_daemon(binary, secret)
            if time.time() - last_mount > MOUNT_EVERY_SECONDS:  # also a heartbeat: the page knows a fetcher runs
                try:
                    roots = {n: move_job.writable(p) for n, p in settings.roots.items()}
                    downloads.write_mount(settings.data_dir, roots, has_aria2=process is not None)
                except OSError as err:
                    log.warning("cannot write fetch.mount: %r", err)
                last_mount = time.time()
            if process is not None:
                try:
                    downloads.write_progress(settings.data_dir, fetcher.tick())
                except aria2.Aria2Error as err:  # starting up, or restarting
                    log.warning("aria2 not answering: %s", err)
                except Exception:
                    log.exception("fetcher pass failed")
            time.sleep(FETCH_POLL_SECONDS)
    except KeyboardInterrupt:
        return 0
    finally:
        if process is not None:
            process.terminate()  # aria2 saves its control files on the way out: resumable
            try:
                process.wait(ARIA2_STOP_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
        conn.close()


def _move_once(settings: Settings) -> bool:
    """Run the requested move job, if any; True if one ran. It waits (the request
    stays) while a scan or a model job holds its lock: neither may see files move."""
    data_dir = settings.data_dir
    if not move_job.is_requested(data_dir):
        return False
    try:
        # Its own lock (the page's "running"), then the model's, then the scan's without
        # waiting: never hold a lock a scheduled scan needs while waiting for another.
        with scan_lock(data_dir, wait_seconds=LOCK_WAIT_SECONDS, name=move_job.LOCK_NAME), \
                scan_lock(data_dir, wait_seconds=LOCK_WAIT_SECONDS, name=organize.LOCK_NAME), \
                scan_lock(data_dir):
            raw = move_job.take(data_dir)
            if raw is None:
                return False
            _run_move(settings, raw)
    except ScanLocked:
        return False
    return True


def _run_move(settings: Settings, raw) -> None:
    data_dir = settings.data_dir

    def report(status: move_job.MoveStatus) -> None:
        try:
            move_job.write_status(data_dir, status)
        except OSError as err:
            log.warning("cannot write move status: %r", err)

    log.info("move job %r", {k: v for k, v in raw.items() if k != "password"} if isinstance(raw, dict) else raw)
    conn = connect(settings.db_path)
    try:
        try:
            final = move_job.run(conn, settings, raw, should_stop=lambda: move_job.stop_requested(data_dir), report=report)
        except Exception as err:
            log.exception("move job failed")
            final = move_job.MoveStatus(error=f"出错中断：{err!r}（中途的包下次会先收尾）", finished=time.time())
        report(final)
        log.info("move job done: moved %d, %d problems, error %r", final.moved, len(final.problems), final.error)
        root = move_job.follow_up(conn, settings, final)
        if root:
            log.info("packs in %s confirmed meanwhile: moving them next", root)
    finally:
        conn.close()


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


def _tags_once(settings: Settings) -> bool:
    """Run the tagging requested from the web UI, if any (it holds the model, like organizing)."""
    if not tags.is_requested(settings.data_dir):
        return False
    try:
        with scan_lock(settings.data_dir, wait_seconds=LOCK_WAIT_SECONDS, name=organize.LOCK_NAME):
            raw = tags.take_job(settings.data_dir)
            if raw is None:
                return False
            _run_tags(settings, raw)
    except ScanLocked:
        return False  # another job holds the model: try again next round
    return True


def _run_tags(settings: Settings, raw: dict) -> None:
    data_dir = settings.data_dir

    last = [tags.TagStatus()]

    def report(status: tags.TagStatus) -> None:
        last[0] = status
        try:
            tags.write_status(data_dir, status)
        except OSError as err:
            log.warning("cannot write tags status: %r", err)

    root, paths = raw.get("root"), raw.get("paths")
    if root not in settings.roots or not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        report(tags.TagStatus(error="请求格式不对", finished=time.time()))
        return
    config = load_caption_config(data_dir)
    if not config.active:
        report(tags.TagStatus(error="打标签要用看图模型：先设置好接口", finished=time.time()))
        return
    conn = connect(settings.db_path)
    try:
        patience = tags.Patience(config.retries, config.retry_wait, config.max_failures)
        final = tags.run_tags(conn, settings, root, paths, CaptionClient(config), config.model,
                              should_stop=lambda: tags.stop_requested(data_dir), report=report, patience=patience)
    except Exception as err:  # keeps the counts so far
        log.exception("tagging failed")
        final = replace(last[0], running=False, current=None, error=f"出错中断：{err!r}", finished=time.time())
    finally:
        conn.close()
    report(final)
    log.info("tags: %d of %d packs, %d failed%s", final.done, final.total, final.failed,
             f"; stopped: {final.error}" if final.error else "")


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
    mover = sub.add_parser("move", help="carry out confirmed proposals on request from the web UI (needs the NAS writable)")
    mover.add_argument("--watch", action="store_true", required=True, help="wait for move jobs started from the web UI")
    fetch = sub.add_parser("fetch", help="download queued links with aria2 into each root's _下载 (needs the NAS writable)")
    fetch.add_argument("--watch", action="store_true", required=True, help="wait for downloads queued from the web UI")
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
    if args.cmd == "move":
        _exit_on_sigterm()  # mid-move: the journal leaves the pack open, settled on the next job or scan
        return _move_watch()
    if args.cmd == "fetch":
        _exit_on_sigterm()  # mid-download: aria2 keeps the file's progress, resumed on the next start
        return _fetch_watch()
    return _serve(args.host, args.port)
