import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from assetindex import thumbs
from assetindex.scanner import scan_root
from assetindex.web import create_app


@pytest.fixture
def client(settings, conn, asset_root):
    scan_root(conn, "art", asset_root)
    return TestClient(create_app(settings))


def _id_of(client, query):
    items = client.get("/api/search", params={"q": query}).json()["items"]
    assert len(items) == 1
    return items[0]["id"]


def test_search_endpoint_returns_items(client):
    body = client.get("/api/search", params={"q": "刀剑"}).json()
    assert body["total"] == 1
    assert body["items"][0]["rel_path"] == "特效/刀剑特效.7z"


def test_raw_supports_range_requests(client):
    file_id = _id_of(client, "Battle")
    resp = client.get(f"/api/files/{file_id}/raw", headers={"Range": "bytes=0-9"})
    assert resp.status_code == 206
    assert resp.content == b"0123456789"


def test_raw_is_sandboxed_so_asset_html_or_svg_cannot_run_scripts(client):
    file_id = _id_of(client, "Battle")
    resp = client.get(f"/api/files/{file_id}/raw")
    csp = resp.headers["content-security-policy"]
    assert csp.startswith("sandbox;")
    assert "default-src 'none'" in csp
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_thumbnail_rejects_oversized_source_before_decoding(client, monkeypatch):
    monkeypatch.setattr(thumbs, "MAX_THUMB_SOURCE_PIXELS", 100)  # icon is 64x32
    file_id = _id_of(client, "sword_icon")
    assert client.get(f"/api/files/{file_id}/thumb").status_code == 415


def test_thumbnail_rejects_format_outside_allowlist_despite_extension(client, asset_root):
    # An .ico renamed to .png must not reach a decoder we didn't opt into.
    Image.new("RGBA", (16, 16)).save(asset_root / "2D" / "Icons" / "sword_icon.png", format="ICO")
    file_id = _id_of(client, "sword_icon")
    assert client.get(f"/api/files/{file_id}/thumb").status_code == 415


def test_concurrent_thumbnail_requests_all_succeed(client):
    file_id = _id_of(client, "sword_icon")
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: client.get(f"/api/files/{file_id}/thumb").status_code, range(8)))
    assert codes == [200] * 8


def test_out_of_range_id_is_422_not_500(client):
    assert client.get("/api/files/99999999999999999999/raw").status_code == 422


def test_overlong_query_is_rejected(client):
    assert client.get("/api/search", params={"q": "a" * 500}).status_code == 422


def test_download_sets_attachment_with_utf8_filename(client):
    file_id = _id_of(client, "刀剑")
    resp = client.get(f"/api/files/{file_id}/download")
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert urllib.parse.quote("刀剑特效.7z") in disposition


def test_unknown_id_is_404(client):
    assert client.get("/api/files/999999/raw").status_code == 404


def test_file_removed_after_scan_is_404(client, asset_root):
    file_id = _id_of(client, "刀剑")
    (asset_root / "特效" / "刀剑特效.7z").unlink()
    assert client.get(f"/api/files/{file_id}/download").status_code == 404


def test_thumbnail_is_webp_for_image(client):
    file_id = _id_of(client, "sword_icon")
    resp = client.get(f"/api/files/{file_id}/thumb")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/webp"


def test_thumbnail_rejects_non_image(client):
    file_id = _id_of(client, "刀剑")
    assert client.get(f"/api/files/{file_id}/thumb").status_code == 415


def test_stats_counts_by_kind(client):
    stats = client.get("/api/stats").json()
    assert stats["image"] == 1
    assert stats["archive"] == 1


@pytest.mark.parametrize(
    "path",
    ["/model-viewer.js", "/vendor/three/build/three.module.min.js", "/vendor/three/examples/jsm/loaders/FBXLoader.js"],
)
def test_model_viewer_modules_are_served_as_javascript(client, path):
    # Browsers refuse ES modules served with a non-JavaScript MIME type (e.g. text/plain
    # from a misconfigured Windows registry), which would silently break 3D previews.
    resp = client.get(path)

    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


def test_scan_button_records_a_request_the_scanner_will_see(client, settings):
    from assetindex import scan_request

    assert client.get("/api/scan").json() == {
        "requested": False, "running": False, "started": None, "finished": None, "ok": None,
        "stopped": False, "stopping": False,
    }

    assert client.post("/api/scan").status_code == 403  # plain cross-site POST
    assert not scan_request.is_requested(settings.data_dir)

    resp = client.post("/api/scan", headers={"X-Requested-With": "fetch"})

    assert resp.status_code == 202
    assert resp.json()["requested"] is True
    assert scan_request.is_requested(settings.data_dir)


def test_scan_status_reports_what_the_scanner_wrote(client, settings):
    from assetindex import scan_request
    from assetindex.scan_request import ScanStatus
    from assetindex.scanlock import scan_lock

    scan_request.write_status(settings.data_dir, ScanStatus(running=True, started=1700000000.0))

    with scan_lock(settings.data_dir):
        body = client.get("/api/scan").json()
    assert body["running"] is True and body["started"] == 1700000000.0

    # Lock free but status still says running: the scanner was killed mid-scan.
    body = client.get("/api/scan").json()
    assert body["running"] is False and body["ok"] is False


def test_scan_request_fails_cleanly_when_data_dir_unwritable(client, monkeypatch):
    from assetindex import web

    def deny(_data_dir):
        raise PermissionError("read-only")

    monkeypatch.setattr(web, "request_scan", deny)
    resp = client.post("/api/scan", headers={"X-Requested-With": "fetch"})
    assert resp.status_code == 503


def test_scan_stop_button(client, settings):
    from assetindex import scan_request
    from assetindex.scan_request import ScanStatus
    from assetindex.scanlock import scan_lock

    headers = {"X-Requested-With": "fetch"}
    assert client.post("/api/scan/stop", headers=headers).status_code == 409  # nothing to stop

    scan_request.request_scan(settings.data_dir)
    assert client.post("/api/scan/stop").status_code == 403  # plain cross-site POST
    body = client.post("/api/scan/stop", headers=headers).json()
    assert body["requested"] is False  # a scan not started yet is simply dropped

    scan_request.write_status(settings.data_dir, ScanStatus(running=True, started=1.0))
    with scan_lock(settings.data_dir):
        body = client.post("/api/scan/stop", headers=headers).json()
    assert body["running"] is True and body["stopping"] is True



# --- semantic search settings ------------------------------------------------------

XRW = {"X-Requested-With": "fetch"}


@pytest.fixture
def embed_server():
    from tests.fake_embed import FakeEmbedServer

    with FakeEmbedServer() as server:
        yield server


def test_embedding_settings_start_off_and_hide_the_key(client, embed_server):
    body = client.get("/api/settings/embedding").json()
    assert body["enabled"] is False and body["has_key"] is False

    update = {"enabled": True, "base_url": embed_server.base_url, "model": "m", "api_key": "sk-secret"}
    assert client.put("/api/settings/embedding", json=update).status_code == 403  # no header
    resp = client.put("/api/settings/embedding", json=update, headers=XRW)

    assert resp.status_code == 200
    assert resp.json()["has_key"] is True and "sk-secret" not in resp.text
    assert "sk-secret" not in client.get("/api/settings/embedding").text


def test_bad_embedding_settings_are_rejected_with_a_reason(client):
    resp = client.put("/api/settings/embedding", json={"base_url": "ftp://x"}, headers=XRW)
    assert resp.status_code == 422 and "http" in resp.json()["detail"]
    resp = client.put("/api/settings/embedding", json=["not", "an", "object"], headers=XRW)
    assert resp.status_code == 422


def test_connection_test_button(client, embed_server):
    update = {"enabled": True, "base_url": embed_server.base_url, "model": "m", "image_format": "input_object"}
    client.put("/api/settings/embedding", json=update, headers=XRW)

    assert client.post("/api/settings/embedding/test").status_code == 403
    body = client.post("/api/settings/embedding/test", headers=XRW).json()
    assert body["text"]["ok"] and body["image"]["ok"]


def test_semantic_search_endpoint(client, settings, embed_server):
    from assetindex.embed_stage import embed_pending
    from assetindex.embedding import load_config

    assert client.get("/api/search", params={"q": "剑", "mode": "semantic"}).status_code == 409  # not set up

    update = {"enabled": True, "base_url": embed_server.base_url, "model": "m", "image_format": "input_object"}
    client.put("/api/settings/embedding", json=update, headers=XRW)
    from assetindex.db import connect

    conn = connect(settings.db_path)
    try:
        embed_pending(conn, settings, load_config(settings.data_dir))
    finally:
        conn.close()

    progress = client.get("/api/settings/embedding").json()["progress"]
    assert progress == {"cards": 5, "text": 5, "image": 1, "image_cards": 1, "rejected": 0}

    body = client.get("/api/search", params={"q": "剑", "mode": "semantic", "kind": "model"}).json()
    assert [i["rel_path"] for i in body["items"]] == ["3D/Knight/knight_sword.fbx"]

    embed_server.fail_status = 500
    resp = client.get("/api/search", params={"q": "new words", "mode": "semantic"})
    assert resp.status_code == 502 and "500" in resp.json()["detail"]



def test_saving_settings_clears_rejections_so_they_are_retried(client, settings, embed_server):
    from assetindex.db import connect

    conn = connect(settings.db_path)
    try:
        file_id = conn.execute("SELECT id FROM files LIMIT 1").fetchone()[0]
        with conn:
            conn.execute(
                "INSERT INTO embeddings (file_id, source, model, mtime, vector, error) VALUES (?, 'image', 'm', 0, NULL, 'x')",
                (file_id,),
            )
        client.put("/api/settings/embedding", json={"image_format": "messages"}, headers=XRW)
        assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
    finally:
        conn.close()


def test_semantic_search_before_any_vectors_says_so(client, embed_server):
    update = {"enabled": True, "base_url": embed_server.base_url, "model": "m"}
    client.put("/api/settings/embedding", json=update, headers=XRW)
    resp = client.get("/api/search", params={"q": "剑", "mode": "semantic"})
    assert resp.status_code == 409 and "向量" in resp.json()["detail"]


def test_only_one_connection_test_at_a_time(client, embed_server, monkeypatch):
    from assetindex import web

    update = {"enabled": True, "base_url": embed_server.base_url, "model": "m"}
    client.put("/api/settings/embedding", json=update, headers=XRW)
    monkeypatch.setattr(web, "PROBE_SLOTS", 0)  # pretend one is already running
    assert client.post("/api/settings/embedding/test", headers=XRW).status_code == 429
