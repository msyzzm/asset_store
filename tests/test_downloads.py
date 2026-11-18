import sqlite3

import pytest

from assetindex import downloads
from assetindex.db import connect


def test_add_keeps_a_link_with_its_details(conn):
    download_id = downloads.add(conn, "art", " https://example.org/a.zip ", title=" 石墙 ", source="ambientcg",
                                md5="ABCDEF0123456789ABCDEF0123456789", size=10, now=5.0)

    row = downloads.get(conn, download_id)
    assert row["url"] == "https://example.org/a.zip"
    assert row["title"] == "石墙"
    assert row["md5"] == "abcdef0123456789abcdef0123456789"
    assert (row["status"], row["created"], row["size"]) == ("queued", 5.0, 10)


@pytest.mark.parametrize("url", ["", "ftp://example.org/a.zip", "file:///etc/passwd", "javascript:alert(1)",
                                 "https://", "example.org/a.zip", None, 3, "https://x.org/" + "a" * 5000])
def test_add_refuses_what_is_not_an_http_link(conn, url):
    with pytest.raises(ValueError):
        downloads.add(conn, "art", url)


@pytest.mark.parametrize("fields", [{"md5": "xyz"}, {"size": -1}, {"size": "10"}, {"title": 5}, {"source": ""}])
def test_add_refuses_malformed_details(conn, fields):
    with pytest.raises(ValueError):
        downloads.add(conn, "art", "https://example.org/a.zip", **fields)


def test_recent_lists_unfinished_first_in_run_order_then_ended_newest_first(conn):
    ids = [downloads.add(conn, "art", f"https://example.org/{n}.zip") for n in range(4)]
    conn.execute("UPDATE downloads SET status = 'done' WHERE id IN (?, ?)", (ids[0], ids[2]))

    assert [r["id"] for r in downloads.recent(conn)] == [ids[1], ids[3], ids[2], ids[0]]


def test_cancel_only_stops_unfinished_downloads(conn):
    queued = downloads.add(conn, "art", "https://example.org/a.zip")
    done = downloads.add(conn, "art", "https://example.org/b.zip")
    conn.execute("UPDATE downloads SET status = 'done' WHERE id = ?", (done,))

    assert downloads.cancel(conn, queued, now=9.0)
    assert not downloads.cancel(conn, done)
    assert downloads.get(conn, queued)["status"] == "cancelled"
    assert downloads.get(conn, done)["status"] == "done"


def test_retry_queues_a_failed_download_again_once_the_fetcher_let_go(conn):
    download_id = downloads.add(conn, "art", "https://example.org/a.zip")
    conn.execute("UPDATE downloads SET status = 'failed', error = 'x', gid = 'g' WHERE id = ?", (download_id,))
    assert not downloads.retry(conn, download_id)  # aria2 still holds it

    conn.execute("UPDATE downloads SET gid = NULL WHERE id = ?", (download_id,))
    assert downloads.retry(conn, download_id)
    row = downloads.get(conn, download_id)
    assert (row["status"], row["error"]) == ("queued", None)


def test_wanted_lists_assets_queued_or_downloaded_but_not_failed_ones(conn):
    downloads.add(conn, "art", "https://example.org/a.zip", source="ambientcg", source_id="Bricks113A")
    failed = downloads.add(conn, "art", "https://example.org/b.zip", source="ambientcg", source_id="Rock01")
    downloads.add(conn, "art", "https://example.org/c.zip", source="polyhaven", source_id="Rock01")
    conn.execute("UPDATE downloads SET status = 'failed' WHERE id = ?", (failed,))

    assert downloads.wanted(conn, "ambientcg", ["Bricks113A", "Rock01", "Other"]) == {"Bricks113A"}
    assert downloads.wanted(conn, "ambientcg", []) == set()


def test_a_database_from_before_source_ids_gets_the_column(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE downloads (id INTEGER PRIMARY KEY, root TEXT NOT NULL, url TEXT NOT NULL, title TEXT, "
                "source TEXT, md5 TEXT, size INTEGER, status TEXT NOT NULL DEFAULT 'queued', gid TEXT, path TEXT, "
                "dup_of TEXT, error TEXT, created REAL NOT NULL, finished REAL)")
    old.execute("INSERT INTO downloads (root, url, created) VALUES ('art', 'https://example.org/a.zip', 1)")
    old.commit()
    old.close()

    conn = connect(path)
    try:
        download_id = downloads.add(conn, "art", "https://example.org/b.zip", source="polyhaven", source_id="x")
        assert downloads.get(conn, download_id)["source_id"] == "x"
        assert downloads.get(conn, 1)["source_id"] is None
    finally:
        conn.close()


def test_progress_round_trips_and_tolerates_garbage(tmp_path):
    downloads.write_progress(tmp_path, {3: {"done": 1, "total": 2, "speed": 3}})
    assert downloads.read_progress(tmp_path) == {3: {"done": 1, "total": 2, "speed": 3}}

    (tmp_path / downloads.PROGRESS_NAME).write_text("[1, 2]", encoding="utf-8")
    assert downloads.read_progress(tmp_path) == {}


def test_mount_is_unknown_when_stale_or_missing(tmp_path):
    assert downloads.read_mount(tmp_path) is None
    downloads.write_mount(tmp_path, {"art": True}, has_aria2=True)

    assert downloads.read_mount(tmp_path) == {"roots": {"art": True}, "aria2": True}
    assert downloads.read_mount(tmp_path, now=10**12) is None
