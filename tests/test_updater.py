import json

import pytest
from fastapi.testclient import TestClient

from assetindex import updater
from assetindex.web import create_app

PAGE = {"X-Requested-With": "fetch"}


def _status(data_dir, **fields):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / updater.STATUS_NAME).write_text(json.dumps(fields), encoding="utf-8")


def test_no_status_file_means_the_updater_is_not_installed(tmp_path):
    state = updater.update_state(tmp_path)
    assert state["installed"] is False and state["requested"] is None


def test_status_written_by_the_host_is_read_tolerantly(tmp_path):
    _status(tmp_path, state="idle", current="abc1234", latest="def5678", behind=2,
            pending=["def5678 feat: x", "bbb0000 fix: y"], checked=1700000000, extra="ignored")

    state = updater.update_state(tmp_path)

    assert state["installed"] and state["behind"] == 2 and state["current"] == "abc1234"
    assert state["pending"] == ["def5678 feat: x", "bbb0000 fix: y"] and "extra" not in state


@pytest.mark.parametrize("raw", ["{not json", "[]", json.dumps({"state": 5}), json.dumps({"behind": "x", "state": "idle"})])
def test_garbage_status_does_not_break_the_page(tmp_path, raw):
    tmp_path.joinpath(updater.STATUS_NAME).write_text(raw, encoding="utf-8")
    state = updater.update_state(tmp_path)
    assert isinstance(state["installed"], bool) and state["behind"] in (None, 0)


def test_requests_are_one_word_files(tmp_path):
    updater.request(tmp_path, "check")
    assert (tmp_path / updater.REQUEST_NAME).read_text(encoding="utf-8") == "check\n"
    assert updater.update_state(tmp_path)["requested"] == "check"
    with pytest.raises(ValueError):
        updater.request(tmp_path, "rm -rf /")


def test_web_endpoints(settings):
    client = TestClient(create_app(settings))
    assert client.get("/api/update").json()["installed"] is False
    assert client.post("/api/update", json={"action": "update"}).status_code == 403
    assert client.post("/api/update", headers=PAGE, json={"action": "nope"}).status_code == 422

    _status(settings.data_dir, state="running", current="abc1234")
    assert client.post("/api/update", headers=PAGE, json={"action": "update"}).status_code == 409

    _status(settings.data_dir, state="idle", current="abc1234")
    started = client.post("/api/update", headers=PAGE, json={"action": "update"})
    assert started.status_code == 202 and started.json()["requested"] == "update"
    assert client.get("/api/agent/tasks").json()["update"]["requested"] == "update"


def test_stop_button(settings):
    client = TestClient(create_app(settings))
    assert client.post("/api/update/stop", headers=PAGE).status_code == 409  # nothing to stop

    _status(settings.data_dir, state="idle", current="abc1234")
    updater.request(settings.data_dir, "update")
    assert client.post("/api/update/stop").status_code == 403
    assert updater.update_state(settings.data_dir)["stoppable"] is True
    body = client.post("/api/update/stop", headers=PAGE).json()
    assert body["requested"] is None and body["stoppable"] is False  # not started yet: simply dropped

    _status(settings.data_dir, state="running", step="build", current="abc1234")
    body = client.post("/api/update/stop", headers=PAGE).json()
    assert body["stopping"] is True and (settings.data_dir / updater.STOP_NAME).exists()

    # Containers are being recreated: interrupting now could leave the site down.
    (settings.data_dir / updater.STOP_NAME).unlink()
    (settings.data_dir / updater.STOP_NAME).touch()  # pressed just as the restart began
    _status(settings.data_dir, state="running", step="restart", current="abc1234")
    state = updater.update_state(settings.data_dir)
    assert state["stoppable"] is False and state["stopping"] is False
    (settings.data_dir / updater.STOP_NAME).unlink()
    assert client.post("/api/update/stop", headers=PAGE).status_code == 409
    assert not (settings.data_dir / updater.STOP_NAME).exists()


def test_request_nobody_picked_up_stops_blocking(tmp_path, monkeypatch):
    import os
    import time

    updater.request(tmp_path, "update")
    old = time.time() - updater.REQUEST_STALE_SECONDS - 5
    os.utime(tmp_path / updater.REQUEST_NAME, (old, old))

    state = updater.update_state(tmp_path)

    assert state["requested"] is None and state["state"] == "error" and "没响应" in state["error"]


def test_stuck_check_is_reported(tmp_path):
    import time

    _status(tmp_path, state="checking", started=time.time() - updater.STUCK_CHECK_SECONDS - 5)
    assert updater.update_state(tmp_path)["state"] == "error"


def test_built_version_differing_from_the_code_needs_a_rebuild(tmp_path):
    _status(tmp_path, state="error", current="bbb2222", deployed="aaa1111", behind=0)
    state = updater.update_state(tmp_path)
    assert state["deployed"] == "aaa1111" and state["needs_rebuild"] is True


def test_non_utf8_status_is_still_read(tmp_path):
    tmp_path.joinpath(updater.STATUS_NAME).write_bytes(b'{"state":"idle","pending":["abc \xe9t\xe9"]}')
    assert updater.update_state(tmp_path)["state"] == "idle"
