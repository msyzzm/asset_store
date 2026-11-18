import os
import time

import pytest
from fastapi.testclient import TestClient

from assetindex import downloads, scan_request, upload_web, uploads
from assetindex.categories import DOWNLOADS
from assetindex.fetch_job import partial_dir
from assetindex.scanner import scan_root
from assetindex.web import create_app

HEADERS = {"X-Requested-With": "fetch"}


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


def _start(client, body, status=201):
    response = client.post("/api/uploads", json=body, headers=HEADERS)
    assert response.status_code == status, response.text
    return response.json()


def _put(client, upload_id, idx, data, status=200):
    response = client.put(f"/api/uploads/{upload_id}/files/{idx}", content=data, headers=HEADERS)
    assert response.status_code == status, response.text
    return response.json()


def _finish(client, upload_id, status=200):
    response = client.post(f"/api/uploads/{upload_id}/finish", headers=HEADERS)
    assert response.status_code == status, response.text
    return response.json()


def test_one_file_lands_in_the_downloads_folder(client, settings, asset_root):
    row = _start(client, {"files": [{"path": "a.zip", "size": 3}]})
    assert (row["root"], row["status"], row["title"], row["source"]) == ("art", "uploading", "a.zip", "upload")

    _put(client, row["id"], 0, b"abc")
    done = _finish(client, row["id"])

    assert done["status"] == "done" and done["path"] == f"{DOWNLOADS}/a.zip"
    assert (asset_root / DOWNLOADS / "a.zip").read_bytes() == b"abc"
    assert not partial_dir(asset_root, row["id"]).exists()
    assert scan_request.is_requested(settings.data_dir)


def test_a_folder_lands_as_one_pack_with_its_subfolders(client, asset_root):
    row = _start(client, {"folder": True, "name": "My Pack",
                          "files": [{"path": "sub/a.png", "size": 1}, {"path": "b.txt", "size": 0}]})

    _put(client, row["id"], 0, b"x")
    _put(client, row["id"], 1, b"")
    done = _finish(client, row["id"])

    pack = asset_root / DOWNLOADS / "My Pack"
    assert done["path"] == f"{DOWNLOADS}/My Pack"
    assert (pack / "sub" / "a.png").read_bytes() == b"x" and (pack / "b.txt").read_bytes() == b""


def test_a_taken_name_gets_a_number(client, asset_root):
    (asset_root / DOWNLOADS).mkdir()
    (asset_root / DOWNLOADS / "a.zip").write_bytes(b"old")
    row = _start(client, {"files": [{"path": "a.zip", "size": 1}]})
    _put(client, row["id"], 0, b"n")

    assert _finish(client, row["id"])["path"] == f"{DOWNLOADS}/a-2.zip"
    assert (asset_root / DOWNLOADS / "a.zip").read_bytes() == b"old"


def test_an_identical_file_is_marked_duplicate(client, settings, conn, asset_root):
    scan_root(conn, "art", asset_root)
    data = (asset_root / "Audio" / "Battle_Theme.mp3").read_bytes()
    row = _start(client, {"files": [{"path": "theme.mp3", "size": len(data)}]})
    _put(client, row["id"], 0, data)
    _finish(client, row["id"])

    item = downloads.get(conn, row["id"])
    assert (item["status"], item["dup_of"]) == ("duplicate", "Audio/Battle_Theme.mp3")


def test_finishing_before_every_file_arrived_is_refused(client):
    row = _start(client, {"folder": True, "name": "P", "files": [{"path": "a", "size": 1}, {"path": "b", "size": 1}]})
    _put(client, row["id"], 0, b"1")

    assert "还有 1 个文件" in _finish(client, row["id"], status=409)["detail"]


@pytest.mark.parametrize("data", [b"ab", b"abcd"])
def test_a_body_of_the_wrong_size_is_refused_and_not_kept(client, asset_root, data):
    row = _start(client, {"files": [{"path": "a.bin", "size": 3}]})

    _put(client, row["id"], 0, data, status=422)

    assert not (partial_dir(asset_root, row["id"]) / "a.bin").exists()
    _put(client, row["id"], 0, b"abc")  # sent again: fine
    assert _finish(client, row["id"])["status"] == "done"


def test_a_file_sent_twice_is_written_once(client, asset_root):
    row = _start(client, {"files": [{"path": "a.bin", "size": 3}]})
    _put(client, row["id"], 0, b"abc")
    _put(client, row["id"], 0, b"xyz")
    _finish(client, row["id"])

    assert (asset_root / DOWNLOADS / "a.bin").read_bytes() == b"xyz"


@pytest.mark.parametrize("body", [
    {},
    {"files": []},
    {"files": [{"path": "a", "size": -1}]},
    {"files": [{"path": "a", "size": "1"}]},
    {"files": [{"path": "a", "size": True}]},
    {"files": [{"path": "dir/a", "size": 1}]},  # one file: no folders
    {"files": [{"path": "a", "size": 1}, {"path": "b", "size": 1}]},  # several files: a folder
    {"folder": True, "files": [{"path": "a", "size": 1}]},  # a folder needs a name
    {"folder": True, "name": "P", "files": [{"path": "../a", "size": 1}]},
    {"folder": True, "name": "P", "files": [{"path": "a//b", "size": 1}]},
    {"folder": True, "name": "P", "files": [{"path": "/a", "size": 1}]},
    {"folder": True, "name": "P", "files": [{"path": "a\\b", "size": 1}]},
    {"folder": True, "name": "P", "files": [{"path": "C:a", "size": 1}]},
    {"folder": True, "name": "P", "files": [{"path": ".hidden", "size": 1}]},
    {"folder": True, "name": "P", "files": [{"path": "x.aria2", "size": 1}]},
    {"folder": True, "name": "P", "files": [{"path": "A.png", "size": 1}, {"path": "a.png", "size": 1}]},
    {"folder": True, "name": "P", "files": [{"path": "a", "size": 1}, {"path": "a/b", "size": 1}]},
    {"files": [{"path": "a", "size": 1}], "root": "nope"},
    {"files": [{"path": "a", "size": 1}], "extra": 1},
])
def test_bad_requests_are_refused(client, body):
    assert client.post("/api/uploads", json=body, headers=HEADERS).status_code == 422


def test_a_deep_folder_is_fine(client):
    path = "/".join(f"d{i}" for i in range(20)) + "/a.png"
    _start(client, {"folder": True, "name": "P", "files": [{"path": path, "size": 1}]})


def test_uploading_is_refused_while_the_nas_is_read_only(client, monkeypatch):
    monkeypatch.setattr(uploads, "writable", lambda base: False)

    response = client.post("/api/uploads", json={"files": [{"path": "a", "size": 1}]}, headers=HEADERS)

    assert response.status_code == 409 and "ASSET_MOVER_ACCESS=rw" in response.json()["detail"]


def test_uploading_is_refused_without_room_on_the_nas(client, monkeypatch):
    monkeypatch.setattr(uploads, "free_bytes", lambda base: 10)

    response = client.post("/api/uploads", json={"files": [{"path": "a", "size": 1}]}, headers=HEADERS)

    assert response.status_code == 409 and "空间不够" in response.json()["detail"]


def test_every_upload_route_needs_the_page_header(client):
    row = _start(client, {"files": [{"path": "a", "size": 1}]})

    assert client.post("/api/uploads", json={"files": [{"path": "a", "size": 1}]}).status_code == 403
    assert client.put(f"/api/uploads/{row['id']}/files/0", content=b"1").status_code == 403
    assert client.post(f"/api/uploads/{row['id']}/finish").status_code == 403


def test_unknown_uploads_and_files_are_404(client, conn, settings):
    row = _start(client, {"files": [{"path": "a", "size": 1}]})
    other = downloads.add(conn, "art", "https://example.org/a.zip")

    _put(client, 999, 0, b"1", status=404)
    _put(client, row["id"], 5, b"1", status=404)
    _put(client, other, 0, b"1", status=404)
    _finish(client, other, status=404)


def test_a_cancelled_upload_is_cleared_and_takes_no_more_files(client, asset_root):
    row = _start(client, {"folder": True, "name": "P", "files": [{"path": "a", "size": 1}, {"path": "b", "size": 1}]})
    _put(client, row["id"], 0, b"1")

    response = client.post(f"/api/downloads/{row['id']}/cancel", headers=HEADERS)

    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert not partial_dir(asset_root, row["id"]).exists()
    _put(client, row["id"], 1, b"1", status=409)
    _finish(client, row["id"], status=409)
    retry = client.post(f"/api/downloads/{row['id']}/retry", headers=HEADERS)
    assert retry.status_code == 409 and "重新上传" in retry.json()["detail"]


def test_finishing_waits_for_a_running_move_job(client, asset_root, monkeypatch):
    row = _start(client, {"files": [{"path": "a", "size": 1}]})
    _put(client, row["id"], 0, b"1")
    monkeypatch.setattr(upload_web, "mover_busy", lambda data_dir: True)

    assert "移动" in _finish(client, row["id"], status=409)["detail"]

    monkeypatch.setattr(upload_web, "mover_busy", lambda data_dir: False)
    assert _finish(client, row["id"])["status"] == "done"


def test_the_list_shows_how_far_an_upload_got(client):
    row = _start(client, {"folder": True, "name": "P", "files": [{"path": "a", "size": 2}, {"path": "b", "size": 3}]})
    _put(client, row["id"], 1, b"123")

    [item] = client.get("/api/downloads").json()["items"]

    assert item["status"] == "uploading" and item["progress"] == {"done": 3, "total": 5, "speed": 0}

    uploads.arriving(row["id"], 0, 1)  # half of file 0 written so far
    try:
        [item] = client.get("/api/downloads").json()["items"]
        assert item["progress"]["done"] == 4
    finally:
        uploads.arrived(row["id"], 0)
    assert client.get("/api/downloads").json()["items"][0]["progress"]["done"] == 3


def test_an_abandoned_upload_fails_and_is_cleared(conn, settings, asset_root):
    upload_id = uploads.start(conn, settings, "art", {"folder": True, "name": "P",
                                                     "files": [{"path": "a", "size": 1}, {"path": "b", "size": 1}]})
    target, _ = uploads.part_target(conn, settings, upload_id, 0)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"1")
    uploads.part_done(conn, settings, upload_id, 0)

    uploads.sweep(conn, settings, now=time.time() + 60)
    assert downloads.get(conn, upload_id)["status"] == "uploading"  # still fresh

    uploads.sweep(conn, settings, now=time.time() + uploads.IDLE_SECONDS + 60)
    item = downloads.get(conn, upload_id)
    assert item["status"] == "failed" and "重新上传" in item["error"]
    assert not partial_dir(asset_root, upload_id).exists()


def test_a_file_arriving_keeps_an_upload_alive(conn, settings, asset_root):
    upload_id = uploads.start(conn, settings, "art", {"files": [{"path": "a", "size": 1}]},
                              now=time.time() - uploads.IDLE_SECONDS - 60)
    target, _ = uploads.part_target(conn, settings, upload_id, 0)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"1")
    os.utime(target)

    uploads.sweep(conn, settings)

    assert downloads.get(conn, upload_id)["status"] == "uploading"


def test_the_fetcher_leaves_uploads_alone(conn, settings):
    upload_id = uploads.start(conn, settings, "art", {"files": [{"path": "a", "size": 1}]})

    assert downloads.retry(conn, upload_id) is False
    rows = conn.execute("SELECT id FROM downloads WHERE status IN ('queued', 'active')").fetchall()
    assert rows == []


def test_the_list_drops_abandoned_uploads(client, monkeypatch):
    row = _start(client, {"files": [{"path": "a", "size": 1}]})
    monkeypatch.setattr(uploads, "IDLE_SECONDS", -1)

    [item] = client.get("/api/downloads").json()["items"]

    assert item["id"] == row["id"] and item["status"] == "failed"


def test_a_cancelled_upload_leaves_nothing_after_a_late_failed_file(conn, settings, asset_root):
    upload_id = uploads.start(conn, settings, "art", {"files": [{"path": "a", "size": 1}]})
    target, _ = uploads.part_target(conn, settings, upload_id, 0)
    downloads.cancel(conn, upload_id)
    target.parent.mkdir(parents=True)  # a file that was arriving while the cancel cleared the folder

    uploads.clear_if_ended(conn, settings, upload_id)

    assert not partial_dir(asset_root, upload_id).exists()


def test_an_upload_still_running_is_not_cleared(conn, settings, asset_root):
    upload_id = uploads.start(conn, settings, "art", {"files": [{"path": "a", "size": 1}]})
    target, _ = uploads.part_target(conn, settings, upload_id, 0)
    target.parent.mkdir(parents=True)

    uploads.clear_if_ended(conn, settings, upload_id)

    assert partial_dir(asset_root, upload_id).exists()
