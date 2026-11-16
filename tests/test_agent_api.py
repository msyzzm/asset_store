import zipfile

import pytest
from fastapi.testclient import TestClient

from assetindex.archive_index import index_archives
from assetindex.embedding import EmbedConfig, save_config
from assetindex.embed_stage import embed_pending
from assetindex.extract import find_bsdtar
from assetindex.scanner import scan_root
from assetindex.web import create_app
from tests.fake_embed import FakeEmbedServer

needs_bsdtar = pytest.mark.skipif(find_bsdtar() is None, reason="bsdtar not installed")


@pytest.fixture
def client(settings, conn, asset_root):
    pack = asset_root / "Packs" / "weapons.zip"
    pack.parent.mkdir(parents=True)
    with zipfile.ZipFile(pack, "w") as z:
        z.writestr("Weapons/Swords/longsword.fbx", b"FBX-longsword")
        z.writestr("Weapons/readme.txt", b"hello")
    scan_root(conn, "art", asset_root)
    index_archives(conn, "art", asset_root)
    return TestClient(create_app(settings))


def _item(client, q, **params):
    body = client.get("/api/agent/search", params={"q": q, **params}).json()
    return body, body["items"][0] if body["items"] else None


def test_search_returns_compact_items_with_links(client):
    body, item = _item(client, "sword_icon")

    assert body["mode"] == "text" and body["total"] == 1
    assert item["path"] == "art:/2D/Icons/sword_icon.png" and item["kind"] == "image"
    assert item["links"]["download"] == f"/api/files/{item['id']}/download"
    assert item["links"]["thumb"] == f"/api/files/{item['id']}/thumb"
    assert item["modified"].endswith("Z") and item["archive"] is None


def test_archive_hits_explain_the_match(client):
    _, item = _item(client, "longsword")

    assert item["name"] == "weapons.zip"
    assert item["archive"]["entries"] == 2
    assert item["matches_inside"] == ["Weapons/Swords/longsword.fbx"]
    assert "thumb" not in item["links"]  # no picture inside, so no contact sheet


def test_auto_mode_falls_back_to_text_when_semantic_search_is_off(client):
    body, _ = _item(client, "sword", mode="auto")
    assert body["mode"] == "text" and body["note"]

    assert client.get("/api/agent/search", params={"q": "sword", "mode": "semantic"}).status_code == 409


def test_auto_mode_uses_semantic_search_when_it_works(client, settings, conn):
    with FakeEmbedServer() as server:
        config = EmbedConfig(enabled=True, base_url=server.base_url, model="m")
        save_config(settings.data_dir, config)
        embed_pending(conn, settings, config)

        body, item = _item(client, "剑", mode="auto")

    assert body["mode"] == "semantic" and body["note"] is None
    assert "sword" in item["name"].lower() or "剑" in item["path"]


def test_detail_lists_archive_contents_with_download_links(client):
    _, item = _item(client, "weapons")

    detail = client.get(f"/api/agent/files/{item['id']}").json()

    assert detail["entries_total"] == 2
    entry = next(e for e in detail["entries"] if e["path"].endswith("longsword.fbx"))
    assert entry["download"] == f"/api/agent/files/{item['id']}/entries/{entry['id']}/download"
    filtered = client.get(f"/api/agent/files/{item['id']}/entries", params={"q": "readme"}).json()
    assert [e["path"] for e in filtered["entries"]] == ["Weapons/readme.txt"]
    assert client.get("/api/agent/files/999999").status_code == 404


@needs_bsdtar
def test_one_file_can_be_taken_out_of_an_archive(client):
    _, item = _item(client, "weapons")
    entry = next(e for e in client.get(f"/api/agent/files/{item['id']}").json()["entries"] if e["path"].endswith(".fbx"))

    resp = client.get(entry["download"])

    assert resp.status_code == 200 and resp.content == b"FBX-longsword"
    assert 'filename="longsword.fbx"' in resp.headers["content-disposition"]
    assert client.get(f"/api/agent/files/{item['id']}/entries/999999/download").status_code == 404


def test_oversized_members_are_refused(client, monkeypatch):
    from assetindex import agent_api

    monkeypatch.setattr(agent_api, "MAX_ENTRY_BYTES", 3)
    _, item = _item(client, "weapons")
    entry = client.get(f"/api/agent/files/{item['id']}").json()["entries"][0]

    assert client.get(entry["download"]).status_code == 413


def test_tasks_show_scan_and_caption_state(client):
    tasks = client.get("/api/agent/tasks").json()
    assert tasks["scan"]["running"] is False and tasks["caption"]["running"] is False


def test_awkward_member_names_get_a_valid_header(client, settings, conn, asset_root):
    pack = asset_root / "Packs" / "odd.zip"
    with zipfile.ZipFile(pack, "w") as z:
        z.writestr('dir/a"b\c 宝箱.txt', b"x")
        z.writestr("dir/empty.txt", b"")
    scan_root(conn, "art", asset_root)
    index_archives(conn, "art", asset_root)
    _, item = _item(client, "odd")
    entries = client.get(f"/api/agent/files/{item['id']}").json()["entries"]

    empty = next(e for e in entries if e["path"].endswith("empty.txt"))
    resp = client.get(empty["download"])
    assert resp.status_code == 200 and resp.content == b""  # nothing to extract, not an error

    from assetindex.agent_api import _disposition

    header = _disposition('a"b\c 宝箱.txt')
    assert '"' not in header.split('filename="', 1)[1].split('";', 1)[0]
    assert "filename*=UTF-8''" in header
