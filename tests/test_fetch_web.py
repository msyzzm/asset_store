import pytest
from fastapi.testclient import TestClient

from assetindex import downloads
from assetindex.web import create_app

HEADERS = {"X-Requested-With": "fetch"}


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


@pytest.fixture
def writable(settings):
    downloads.write_mount(settings.data_dir, {"art": True}, has_aria2=True)


def test_a_link_is_queued_for_the_first_root(client, writable):
    response = client.post("/api/downloads", json={"url": "https://example.org/a.zip", "title": "A"}, headers=HEADERS)

    assert response.status_code == 201
    body = response.json()
    assert (body["root"], body["status"], body["title"]) == ("art", "queued", "A")
    listed = client.get("/api/downloads").json()
    assert [i["id"] for i in listed["items"]] == [body["id"]]
    assert listed["counts"] == {"queued": 1}
    assert listed["mount"] == {"roots": {"art": True}, "aria2": True}


def test_queueing_needs_the_page_header(client, writable):
    assert client.post("/api/downloads", json={"url": "https://example.org/a.zip"}).status_code == 403


@pytest.mark.parametrize(("mount", "words"), [
    (None, "还没运行"),
    ({"art": False}, "只读"),
])
def test_queueing_is_refused_while_the_fetcher_cannot_write(client, settings, mount, words):
    if mount is not None:
        downloads.write_mount(settings.data_dir, mount, has_aria2=True)

    response = client.post("/api/downloads", json={"url": "https://example.org/a.zip"}, headers=HEADERS)

    assert response.status_code == 409 and words in response.json()["detail"]


def test_queueing_is_refused_without_aria2(client, settings):
    downloads.write_mount(settings.data_dir, {"art": True}, has_aria2=False)

    response = client.post("/api/downloads", json={"url": "https://example.org/a.zip"}, headers=HEADERS)

    assert response.status_code == 409 and "aria2" in response.json()["detail"]


@pytest.mark.parametrize("body", [
    {"url": "ftp://example.org/a.zip"},
    {"url": "https://example.org/a.zip", "root": "nope"},
    {"url": "https://example.org/a.zip", "extra": 1},
    {"url": "https://example.org/a.zip", "md5": "short"},
])
def test_bad_requests_are_refused(client, writable, body):
    assert client.post("/api/downloads", json=body, headers=HEADERS).status_code == 422


def test_progress_shows_on_active_downloads_only(client, settings, conn, writable):
    active = downloads.add(conn, "art", "https://example.org/a.zip")
    queued = downloads.add(conn, "art", "https://example.org/b.zip")
    conn.execute("UPDATE downloads SET status = 'active' WHERE id = ?", (active,))
    conn.commit()
    downloads.write_progress(settings.data_dir, {active: {"done": 1, "total": 4, "speed": 2}, queued: {"done": 9}})

    items = {i["id"]: i for i in client.get("/api/downloads").json()["items"]}

    assert items[active]["progress"] == {"done": 1, "total": 4, "speed": 2}
    assert items[queued]["progress"] is None


def test_cancel_and_retry(client, conn, writable):
    download_id = downloads.add(conn, "art", "https://example.org/a.zip")

    assert client.post(f"/api/downloads/{download_id}/cancel", headers=HEADERS).json()["status"] == "cancelled"
    assert client.post(f"/api/downloads/{download_id}/cancel", headers=HEADERS).status_code == 409
    assert client.post(f"/api/downloads/{download_id}/retry", headers=HEADERS).json()["status"] == "queued"
    assert client.post(f"/api/downloads/{download_id}/retry", headers=HEADERS).status_code == 409
    assert client.post("/api/downloads/999/cancel", headers=HEADERS).status_code == 404
    assert client.post("/api/downloads/999/retry", headers=HEADERS).status_code == 404


def test_a_finished_download_organized_away_says_so(client, settings, conn, asset_root):
    from assetindex import scan_request
    from assetindex.scanner import scan_root

    scan_root(conn, "art", asset_root)
    here = downloads.add(conn, "art", "https://example.org/a.zip")
    away = downloads.add(conn, "art", "https://example.org/b.zip")
    folder = downloads.add(conn, "art", "https://example.org/c.zip")
    for row_id, path in ((here, "Audio/Battle_Theme.mp3"), (away, "_下载/b.zip"), (folder, "3D/Knight")):
        conn.execute("UPDATE downloads SET status = 'done', path = ?, finished = 100 WHERE id = ?", (path, row_id))
    conn.commit()

    def gone():
        return {i["id"]: i["gone"] for i in client.get("/api/downloads").json()["items"]}

    assert gone() == {here: False, away: False, folder: False}  # no scan since: not known yet
    scan_request.write_status(settings.data_dir, scan_request.ScanStatus(started=200, finished=300, ok=True))
    assert gone() == {here: False, away: True, folder: False}
    scan_request.write_status(settings.data_dir, scan_request.ScanStatus(started=200, finished=300, ok=False))
    assert gone()[away] is False  # a scan cut short may have missed it
