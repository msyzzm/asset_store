import os
import zipfile

import pytest
from fastapi.testclient import TestClient

from assetindex import archive_index
from assetindex.archive_index import index_archives
from assetindex.config import Settings
from assetindex.db import connect
from assetindex.scanner import scan_root
from assetindex.search import search
from assetindex.web import create_app

SWORD_ENTRIES = (
    "5.1/SwordEffects/Blueprints/BP_sword1.uasset",
    "5.1/SwordEffects/Blueprints/BP_sword2.uasset",
    "5.1/SwordEffects/Materials/MI_blue.uasset",
    "5.1/SwordEffects/Maps/Demo.umap",
)


def _zip(path, names):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name in names:
            z.writestr(name, b"data")
    return path


@pytest.fixture
def pack_root(tmp_path):
    root = tmp_path / "packs"
    _zip(root / "特效" / "刀剑特效.zip", SWORD_ENTRIES)
    _zip(root / "Audio" / "SFX.zip", ["Hits/sword_hit_01.wav", "Hits/punch.wav"])
    (root / "broken.zip").write_bytes(b"not a zip")
    (root / "readme.txt").write_bytes(b"keeps the root non-empty after deletions")
    return root


def _index(conn, root):
    scan_root(conn, "art", root)
    return index_archives(conn, "art", root)


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_index_lists_archives_and_records_engine(conn, pack_root):
    stats = _index(conn, pack_root)

    assert (stats.listed, stats.failed) == (2, 1)
    row = conn.execute(
        "SELECT a.* FROM archives a JOIN files f ON f.id = a.file_id WHERE f.name = '刀剑特效.zip'"
    ).fetchone()
    assert (row["status"], row["entry_count"], row["engine"], row["engine_version"]) == ("ok", 4, "unreal", "5.1")
    broken = conn.execute(
        "SELECT a.status, a.error FROM archives a JOIN files f ON f.id = a.file_id WHERE f.name = 'broken.zip'"
    ).fetchone()
    assert broken["status"] == "error" and broken["error"]


def test_unchanged_archives_are_not_relisted(conn, pack_root):
    _index(conn, pack_root)

    stats = _index(conn, pack_root)

    assert (stats.listed, stats.failed, stats.unchanged) == (0, 0, 3)


def test_changed_archive_is_relisted(conn, pack_root):
    _index(conn, pack_root)
    sfx = _zip(pack_root / "Audio" / "SFX.zip", ["Hits/kick.wav"])
    os.utime(sfx, (1_000_000_000, 1_000_000_000))

    stats = _index(conn, pack_root)

    assert stats.listed == 1
    assert [i["name"] for i in search(conn, "kick").items] == ["SFX.zip"]
    assert search(conn, "punch").total == 0


def test_unreadable_archive_keeps_old_entries(conn, pack_root, monkeypatch):
    _index(conn, pack_root)
    sfx = pack_root / "Audio" / "SFX.zip"
    os.utime(sfx, (1_000_000_000, 1_000_000_000))

    def flaky(path, ext):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(archive_index, "list_archive", flaky)
    stats = _index(conn, pack_root)

    assert stats.unreadable == 1
    assert [i["name"] for i in search(conn, "punch").items] == ["SFX.zip"]


def test_deleted_archive_leaves_no_orphans(conn, pack_root):
    _index(conn, pack_root)
    for path in (pack_root / "特效" / "刀剑特效.zip", pack_root / "Audio" / "SFX.zip", pack_root / "broken.zip"):
        path.unlink()

    _index(conn, pack_root)

    assert _count(conn, "archive_entries") == 0
    assert _count(conn, "archives") == 0
    assert conn.execute("SELECT COUNT(*) FROM entries_fts WHERE path LIKE '%sword%'").fetchone()[0] == 0


def test_search_finds_archive_by_inner_path_with_hits(conn, pack_root):
    _index(conn, pack_root)

    result = search(conn, "BP_sword")

    assert [i["name"] for i in result.items] == ["刀剑特效.zip"]
    item = result.items[0]
    assert item["hits"] == list(SWORD_ENTRIES[:2])
    assert (item["engine"], item["engine_version"], item["entry_count"]) == ("unreal", "5.1", 4)


def test_terms_may_match_archive_name_and_entry_separately(conn, pack_root):
    _index(conn, pack_root)

    result = search(conn, "刀剑 MI_blue")

    assert [i["name"] for i in result.items] == ["刀剑特效.zip"]
    assert result.items[0]["hits"] == [SWORD_ENTRIES[2]]


def test_term_matching_only_archive_name_has_no_hits(conn, pack_root):
    _index(conn, pack_root)

    assert search(conn, "刀剑").items[0]["hits"] == []


def test_entries_endpoint_pages_and_filters(pack_root, tmp_path):
    settings = Settings(roots={"art": pack_root}, data_dir=tmp_path / "data")
    with TestClient(create_app(settings)) as client:
        conn = connect(settings.db_path)
        _index(conn, pack_root)
        ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM files")}
        conn.close()

        sword = ids["刀剑特效.zip"]
        page = client.get(f"/api/files/{sword}/entries", params={"limit": 2, "offset": 1}).json()
        assert page["total"] == 4
        assert [e["path"] for e in page["entries"]] == sorted(SWORD_ENTRIES)[1:3]
        assert page["archive"]["engine"] == "unreal"

        filtered = client.get(f"/api/files/{sword}/entries", params={"q": "blueprints sword2"}).json()
        assert [e["path"] for e in filtered["entries"]] == [SWORD_ENTRIES[1]]

        broken = client.get(f"/api/files/{ids['broken.zip']}/entries").json()
        assert broken["archive"]["status"] == "error" and broken["entries"] == []

        assert client.get("/api/files/999999/entries").status_code == 404


def test_hits_fall_back_to_any_term_when_terms_are_in_different_files(conn, pack_root):
    _index(conn, pack_root)

    result = search(conn, "punch sword_hit")

    assert [i["name"] for i in result.items] == ["SFX.zip"]
    assert result.items[0]["hits"] == ["Hits/punch.wav", "Hits/sword_hit_01.wav"]


def test_memory_error_while_listing_is_retried_later(conn, pack_root, monkeypatch):
    scan_root(conn, "art", pack_root)

    def oom(path, ext):
        raise MemoryError

    monkeypatch.setattr(archive_index, "list_archive", oom)
    stats = index_archives(conn, "art", pack_root)

    assert stats.unreadable == 3
    assert _count(conn, "archives") == 0


def test_retry_errors_relists_failed_archives(conn, pack_root):
    _index(conn, pack_root)

    stats = index_archives(conn, "art", pack_root, retry_errors=True)

    assert (stats.failed, stats.unchanged) == (1, 2)


def test_stop_comes_before_an_archive_so_it_is_not_counted_as_a_crash(conn, pack_root):
    from assetindex.scan_request import ScanStopped

    scan_root(conn, "art", pack_root)

    with pytest.raises(ScanStopped):
        index_archives(conn, "art", pack_root, should_stop=lambda: True)

    assert _count(conn, "archives") == 0 and _count(conn, "scan_attempts") == 0
