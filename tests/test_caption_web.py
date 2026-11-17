import pytest
from fastapi.testclient import TestClient
from PIL import Image

from assetindex import caption_job, cli
from assetindex.caption import CaptionConfig, save_config
from assetindex.scanner import scan_root
from assetindex.web import create_app
from tests.fake_embed import FakeEmbedServer

PAGE = {"X-Requested-With": "fetch"}


@pytest.fixture
def server():
    with FakeEmbedServer() as fake:
        yield fake


@pytest.fixture
def client(settings, conn, asset_root):
    blue = asset_root / "2D" / "Ice" / "ice.png"
    blue.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), (0, 0, 255)).save(blue)
    scan_root(conn, "art", asset_root)
    return TestClient(create_app(settings))


def _configure(settings, server):
    save_config(settings.data_dir, CaptionConfig(base_url=server.base_url, model="qwen", api_key="k"))


def test_settings_round_trip_without_leaking_the_key(client, server):
    assert client.put("/api/settings/caption", json={"base_url": server.base_url}).status_code == 403

    saved = client.put("/api/settings/caption", headers=PAGE, json={"base_url": server.base_url, "model": "q", "api_key": "k"})

    assert saved.status_code == 200
    body = client.get("/api/settings/caption").json()
    assert body["has_key"] and body["active"] and "api_key" not in body and body["roots"] == ["art"]
    assert client.put("/api/settings/caption", headers=PAGE, json={"base_url": "nope"}).status_code == 422


def test_job_limits_are_saved_from_the_page(client, server):
    saved = client.put("/api/settings/caption", headers=PAGE, json={"workers": 2, "max_failures": 7})

    assert saved.status_code == 200 and saved.json()["workers"] == 2
    assert client.get("/api/settings/caption").json()["max_failures"] == 7
    assert client.put("/api/settings/caption", headers=PAGE, json={"workers": 99}).status_code == 422


def test_connection_test_describes_a_sample_picture(client, settings, server):
    assert client.post("/api/settings/caption/test", headers=PAGE).status_code == 409
    _configure(settings, server)

    result = client.post("/api/settings/caption/test", headers=PAGE).json()

    assert result["ok"] and "火焰" in result["text"] and result["seconds"] >= 0


def test_scope_picker_lists_folders_and_counts(client):
    assert client.get("/api/captions/folders", params={"root": "art", "prefix": "2D"}).json() == {
        "folders": ["2D/Ice", "2D/Icons"]
    }
    counts = client.get("/api/captions/scope", params={"root": "art", "prefix": "2D/Ice"}).json()
    assert counts == {"cards": 1, "done": 0, "todo": 1}
    assert client.get("/api/captions/scope", params={"root": "nope"}).status_code == 422


def test_start_and_stop_a_job(client, settings, server):
    scope = {"root": "art", "prefix": "2D"}
    assert client.post("/api/captions", json=scope).status_code == 403
    assert client.post("/api/captions", headers=PAGE, json=scope).status_code == 409  # not set up
    _configure(settings, server)
    assert client.post("/api/captions", headers=PAGE, json={"root": "../etc"}).status_code == 422

    started = client.post("/api/captions", headers=PAGE, json=scope)

    assert started.status_code == 202 and started.json()["requested"]
    assert client.post("/api/captions", headers=PAGE, json=scope).status_code == 409  # one at a time
    stopped = client.post("/api/captions/stop", headers=PAGE).json()
    assert not stopped["requested"] and caption_job.stop_requested(settings.data_dir)


def test_status_of_a_job_whose_captioner_died(client, settings):
    caption_job.write_status(settings.data_dir, caption_job.CaptionStatus(running=True, total=5, done=2))

    status = client.get("/api/captions").json()

    assert not status["running"] and "中途断了" in status["error"] and status["done"] == 2


def test_captioner_runs_the_requested_job(client, settings, server):
    _configure(settings, server)
    client.post("/api/captions", headers=PAGE, json={"root": "art", "prefix": "2D"})

    assert cli._caption_once(settings) is True
    assert cli._caption_once(settings) is False  # nothing left to do

    status = client.get("/api/captions").json()
    assert status["done"] == 2 and not status["running"] and not status["requested"]
    file_id = client.get("/api/search", params={"q": "冰晶"}).json()["items"][0]["id"]
    assert client.get(f"/api/files/{file_id}").json()["caption"].startswith("一块蓝色的冰晶")


def test_captioner_reports_bad_requests_instead_of_crashing(settings, server):
    caption_job.request_job(settings.data_dir, caption_job.Scope("art"))
    assert cli._caption_once(settings) is True
    assert "还没设置" in caption_job.read_status(settings.data_dir).error

    _configure(settings, server)
    caption_job.request_job(settings.data_dir, caption_job.Scope("gone"))
    cli._caption_once(settings)
    assert "范围不对" in caption_job.read_status(settings.data_dir).error


def test_command_line_job(settings, conn, asset_root, server, monkeypatch):
    scan_root(conn, "art", asset_root)
    _configure(settings, server)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    assert cli.main(["caption", "--root", "art", "--prefix", "2D"]) == 0
    assert caption_job.read_status(settings.data_dir).done == 1

    server.fail_status = 503
    assert cli.main(["caption", "--root", "art", "--redo"]) == 1
    with pytest.raises(SystemExit):
        cli.main(["caption"])


def test_command_line_job_ignores_a_stale_stop(settings, conn, asset_root, server, monkeypatch):
    scan_root(conn, "art", asset_root)
    _configure(settings, server)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    caption_job.request_stop(settings.data_dir)  # "停止" pressed with nothing running

    assert cli.main(["caption", "--root", "art"]) == 0
    assert caption_job.read_status(settings.data_dir).done == 1
