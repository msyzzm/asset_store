"""An archive that kills the scanner (OOM, SIGKILL) must not block every later scan."""

import io
import sqlite3
import zipfile

import pytest
from PIL import Image

from assetindex import archive_index, archive_previews
from assetindex.archive_index import CRASH_ERROR, index_archives
from assetindex.archive_previews import build_previews
from assetindex.attempts import MAX_ATTEMPTS, begin, clear, finish
from assetindex.extract import find_bsdtar
from assetindex.scanner import scan_root


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (0, 200, 0)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "packs"
    root.mkdir()
    with zipfile.ZipFile(root / "bomb.zip", "w") as z:
        z.writestr("img/a.png", _png())
    (root / "readme.txt").write_bytes(b"x")
    return root


def _archive_id(conn):
    return conn.execute("SELECT id FROM files WHERE name = 'bomb.zip'").fetchone()[0]


def _attempts(conn, file_id, stage):
    row = conn.execute(
        "SELECT attempts FROM scan_attempts WHERE file_id = ? AND stage = ?", (file_id, stage)
    ).fetchone()
    return row[0] if row else 0


def test_begin_counts_and_finish_clears(conn):
    assert begin(conn, 7, "list") is True
    assert _attempts(conn, 7, "list") == 1

    finish(conn, 7, "list")

    assert _attempts(conn, 7, "list") == 0


def test_begin_refuses_after_max_attempts(conn):
    for _ in range(MAX_ATTEMPTS):
        assert begin(conn, 7, "list") is True

    assert begin(conn, 7, "list") is False


def test_attempt_is_committed_before_the_archive_is_parsed(conn, root, settings, monkeypatch):
    # Seen from another connection: a SIGKILL during parsing must leave the count on disk.
    scan_root(conn, "art", root)
    file_id = _archive_id(conn)
    seen = []

    def spy(path, ext):
        other = sqlite3.connect(settings.db_path)
        seen.append(_attempts(other, file_id, "list"))
        other.close()
        raise archive_index.ArchiveError("stop here")

    monkeypatch.setattr(archive_index, "list_archive", spy)
    index_archives(conn, "art", root)

    assert seen == [1]
    assert _attempts(conn, file_id, "list") == 0  # finished normally: cleared


def test_archive_that_crashed_twice_is_recorded_as_bad_and_skipped(conn, root, monkeypatch):
    scan_root(conn, "art", root)
    file_id = _archive_id(conn)
    for _ in range(MAX_ATTEMPTS):
        begin(conn, file_id, "list")  # two runs died while listing it
    monkeypatch.setattr(archive_index, "list_archive", lambda *a: pytest.fail("must not parse again"))

    stats = index_archives(conn, "art", root)

    row = conn.execute("SELECT status, error FROM archives WHERE file_id = ?", (file_id,)).fetchone()
    assert (row["status"], row["error"]) == ("error", CRASH_ERROR)
    assert stats.failed == 1
    assert _attempts(conn, file_id, "list") == 0  # a changed file gets a fresh chance


def test_transient_errors_do_not_count_as_crashes(conn, root, monkeypatch):
    scan_root(conn, "art", root)
    file_id = _archive_id(conn)

    def nas_hiccup(path, ext):
        raise OSError("stale NFS handle")

    monkeypatch.setattr(archive_index, "list_archive", nas_hiccup)
    for _ in range(MAX_ATTEMPTS + 1):
        index_archives(conn, "art", root)

    assert _attempts(conn, file_id, "list") == 0


@pytest.mark.skipif(find_bsdtar() is None, reason="needs bsdtar")
def test_preview_that_crashed_twice_is_given_up(conn, root, tmp_path, monkeypatch):
    scan_root(conn, "art", root)
    index_archives(conn, "art", root)
    file_id = _archive_id(conn)
    for _ in range(MAX_ATTEMPTS):
        begin(conn, file_id, "preview")
    monkeypatch.setattr(archive_previews, "_member_images", lambda *a: pytest.fail("must not extract again"))

    stats = build_previews(conn, "art", root, tmp_path / "thumbs", find_bsdtar())

    state = conn.execute("SELECT preview_state FROM archives WHERE file_id = ?", (file_id,)).fetchone()[0]
    assert state == 0
    assert stats.empty == 1


def test_clear_forgets_all_attempts(conn):
    begin(conn, 1, "list")
    begin(conn, 2, "preview")

    clear(conn)

    assert conn.execute("SELECT COUNT(*) FROM scan_attempts").fetchone()[0] == 0


def test_deleting_a_file_forgets_its_attempts(conn, root):
    scan_root(conn, "art", root)
    file_id = _archive_id(conn)
    begin(conn, file_id, "list")

    with conn:
        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))

    assert _attempts(conn, file_id, "list") == 0  # ids are reused: no inherited count
