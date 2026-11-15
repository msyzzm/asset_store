"""Operational helpers: scan lock, thumbnail cache pruning, deployment self-check."""

import os
import time
from pathlib import Path

import pytest

from assetindex.cache_prune import prune_thumbs
from assetindex.check import run_checks
from assetindex.config import Settings
from assetindex.extract import find_bsdtar
from assetindex.scanlock import ScanLocked, scan_lock
from assetindex.scanner import scan_root
from assetindex.thumbs import (
    entry_thumb_path,
    file_thumb_path,
    sheet_path,
    unity_preview_path,
)

OLD = time.time() - 3600  # older than the "fresh file" grace period


def _put(path: Path, size: int = 100, mtime: float = OLD) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"w" * size)
    os.utime(path, (mtime, mtime))
    return path


# --- scan lock -------------------------------------------------------------------------


def test_second_scan_is_refused_while_first_holds_lock(tmp_path):
    with scan_lock(tmp_path):
        with pytest.raises(ScanLocked):
            with scan_lock(tmp_path):
                pass


def test_lock_is_released_after_scan(tmp_path):
    with scan_lock(tmp_path):
        pass
    with scan_lock(tmp_path):  # no ScanLocked
        pass


# --- cache pruning ---------------------------------------------------------------------


@pytest.fixture
def indexed(conn, asset_root):
    scan_root(conn, "art", asset_root)
    row = conn.execute("SELECT id, mtime FROM files WHERE name = 'sword_icon.png'").fetchone()
    return row["id"], row["mtime"]


def test_prune_removes_thumbs_of_deleted_or_changed_files(conn, indexed, tmp_path):
    file_id, mtime = indexed
    thumbs = tmp_path / "thumbs"
    current = _put(file_thumb_path(thumbs, file_id, mtime))
    stale = _put(file_thumb_path(thumbs, file_id, mtime - 100))
    deleted = _put(file_thumb_path(thumbs, 999_999, mtime))

    stats = prune_thumbs(conn, thumbs, max_bytes=10**9)

    assert current.exists()
    assert not stale.exists() and not deleted.exists()
    assert stats.removed == 2


def test_prune_removes_previews_of_relisted_or_deleted_archives(conn, tmp_path):
    thumbs = tmp_path / "thumbs"
    conn.execute(
        "INSERT INTO archives (file_id, size, mtime, status) VALUES (5, 1, 2000.0, 'ok')"
    )
    current = _put(sheet_path(thumbs, 5, 2000.0))
    current_unity = _put(unity_preview_path(thumbs, 5, 2000.0, 3))
    stale = _put(sheet_path(thumbs, 5, 1000.0))
    gone = _put(sheet_path(thumbs, 6, 2000.0))

    prune_thumbs(conn, thumbs, max_bytes=10**9)

    assert current.exists() and current_unity.exists()
    assert not stale.exists() and not gone.exists()
    assert not gone.parent.exists()  # empty archive dir removed too


def test_size_cap_evicts_oldest_on_demand_thumbs_but_never_sheets(conn, tmp_path):
    thumbs = tmp_path / "thumbs"
    conn.execute("INSERT INTO archives (file_id, size, mtime, status) VALUES (5, 1, 2000.0, 'ok')")
    sheet = _put(sheet_path(thumbs, 5, 2000.0), size=5000)  # over the cap on its own
    oldest = _put(entry_thumb_path(thumbs, 5, 2000.0, 1, "a.png"), size=400, mtime=OLD - 300)
    middle = _put(entry_thumb_path(thumbs, 5, 2000.0, 1, "b.png"), size=400, mtime=OLD - 200)
    newest = _put(entry_thumb_path(thumbs, 5, 2000.0, 1, "c.png"), size=400, mtime=OLD - 100)

    prune_thumbs(conn, thumbs, max_bytes=900)

    assert sheet.exists()
    assert not oldest.exists()
    assert middle.exists() and newest.exists()


def test_prune_spares_fresh_files_and_foreign_files(conn, tmp_path):
    thumbs = tmp_path / "thumbs"
    fresh_orphan = _put(file_thumb_path(thumbs, 999_999, 1.0), mtime=time.time())
    foreign = _put(thumbs / "0" / "notes.txt")  # not a name we generate
    stale_tmp = _put(thumbs / "0" / "1_2.abcdef.tmp")  # left by a crashed writer

    prune_thumbs(conn, thumbs, max_bytes=0)

    assert fresh_orphan.exists()
    assert foreign.exists()
    assert not stale_tmp.exists()


def test_prune_of_missing_cache_dir_is_a_noop(conn, tmp_path):
    stats = prune_thumbs(conn, tmp_path / "nope", max_bytes=0)

    assert stats.removed == 0


# --- deployment self-check -------------------------------------------------------------


def _by_name(results):
    return {r.name: r for r in results}


def test_check_passes_on_healthy_setup(settings):
    results = _by_name(run_checks(settings))

    assert results["data dir writable"].ok
    assert results["root art readable"].ok
    assert results["3D viewer files"].ok
    if find_bsdtar():
        assert results["bsdtar"].ok
        assert results["bsdtar non-ASCII names"].ok, results["bsdtar non-ASCII names"].detail


def test_check_reports_missing_and_empty_roots(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    settings = Settings(roots={"gone": tmp_path / "missing", "blank": empty}, data_dir=tmp_path / "data")

    results = _by_name(run_checks(settings))

    assert not results["root gone readable"].ok
    assert not results["root blank readable"].ok  # an unmounted NFS share looks like this


def test_file_vanishing_mid_sweep_does_not_abort_prune(conn, tmp_path, monkeypatch):
    # The web process renames/unlinks its temp files while the scanner sweeps.
    from assetindex import cache_prune

    thumbs = tmp_path / "thumbs"
    _put(file_thumb_path(thumbs, 999_999, 1.0))
    real_files = cache_prune._files

    class Vanished:
        def __init__(self, entry):
            self.path, self.name = entry.path, entry.name

        def stat(self, follow_symlinks=True):
            raise FileNotFoundError(self.path)

    def files_then_vanish(directory, pattern):
        return [(match, Vanished(entry)) for match, entry in real_files(directory, pattern)]

    monkeypatch.setattr(cache_prune, "_files", files_then_vanish)

    assert prune_thumbs(conn, thumbs, max_bytes=0).removed == 0

