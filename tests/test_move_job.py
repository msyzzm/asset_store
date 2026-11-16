import os

import pytest
from fastapi.testclient import TestClient

from assetindex import cli, move_job
from assetindex.config import Settings
from assetindex.db import connect
from assetindex.organize import refresh_units
from assetindex.scanner import scan_root
from assetindex.web import create_app

PAGE = {"X-Requested-With": "fetch"}
FILES = ("UE/道具/Sword.rar", "UE/道具/Shield.rar", "UE/道具/Bow.rar", "UE/道具/Sword.png")


def fake_move(src, dst, is_dir):
    os.replace(src, dst)


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "lib"
    for rel in FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(rel.encode())
    settings = Settings(roots={"lib": root}, data_dir=tmp_path / "data")
    conn = connect(settings.db_path)
    scan_root(conn, "lib", root)
    refresh_units(conn, "lib")
    with conn:
        conn.execute("UPDATE organize_units SET status = 'accepted', target = '02_3D/Props/剑.rar' WHERE path = 'UE/道具/Sword.rar'")
    yield conn, settings, root
    conn.close()


def test_parse_accepts_only_known_actions_roots_and_journals(env):
    _, settings, _ = env
    assert move_job.parse({"action": "apply", "root": "lib"}, settings) == ("apply", "lib", None)
    for bad in ({"action": "rm", "root": "lib"}, {"action": "apply", "root": "x"},
                {"action": "undo", "root": "lib", "journal": "../../etc/passwd"},
                {"action": "undo", "root": "lib", "journal": "20260101-000000.jsonl"},  # no such run
                {"action": "apply", "root": "lib", "extra": 1}):
        with pytest.raises(ValueError):
            move_job.parse(bad, settings)


def test_a_job_moves_then_undoes_and_keeps_a_history(env):
    conn, settings, root = env
    times = iter([1_800_000_000 + i for i in range(100)])

    done = move_job.run(conn, settings, {"action": "apply", "root": "lib"}, move=fake_move, now=lambda: next(times))

    assert done.moved == 1 and done.error is None and done.journal.endswith(".jsonl")
    assert (root / "02_3D/Props/剑.rar").exists() and (root / "02_3D/Props/剑.png").exists()
    (run,) = move_job.history(settings.data_dir)
    assert run["moved"] == 1 and run["still_moved"] == 1 and run["open"] == 0

    back = move_job.run(conn, settings, {"action": "undo", "root": "lib", "journal": done.journal}, move=fake_move)

    assert back.moved == 1 and (root / "UE/道具/Sword.rar").exists()
    assert move_job.history(settings.data_dir)[0]["still_moved"] == 0


def test_status_files_round_trip(tmp_path):
    move_job.request(tmp_path, {"action": "apply", "root": "lib"})
    assert move_job.is_requested(tmp_path) and move_job.take(tmp_path) == {"action": "apply", "root": "lib"}
    move_job.write_status(tmp_path, move_job.MoveStatus(running=True, moved=3))
    assert move_job.read_status(tmp_path).moved == 3
    move_job.write_mount(tmp_path, {"lib": False})
    assert move_job.read_mount(tmp_path) == {"lib": False}


def test_web_refuses_to_start_without_a_writable_mover_and_shows_the_plan(env):
    conn, settings, _ = env
    client = TestClient(create_app(settings))

    plan = client.get("/api/organize/moves/plan", params={"root": "lib"}).json()
    assert plan["moves"] == 1 and plan["files"] == 2 and plan["sample"][0]["target"] == "02_3D/Props/剑.rar"

    body = {"action": "apply", "root": "lib"}
    assert client.post("/api/organize/moves", json=body).status_code == 403  # not from the page
    assert "还没运行" in client.post("/api/organize/moves", headers=PAGE, json=body).json()["detail"]
    move_job.write_mount(settings.data_dir, {"lib": False})
    assert "只读" in client.post("/api/organize/moves", headers=PAGE, json=body).json()["detail"]
    move_job.write_mount(settings.data_dir, {"lib": True})
    assert client.post("/api/organize/moves", headers=PAGE, json=body).status_code == 202
    assert client.post("/api/organize/moves", headers=PAGE, json=body).status_code == 409  # one at a time

    state = client.get("/api/organize/moves").json()
    assert state["job"]["requested"] is True and state["mount"] == {"lib": True} and state["unsettled"] == 0
    assert client.post("/api/organize/moves/stop", headers=PAGE).status_code == 200
    assert client.post("/api/organize/moves/stop", headers=PAGE).status_code == 409


def test_the_mover_runs_a_requested_job(env, monkeypatch):
    conn, settings, root = env
    real_run = move_job.run
    monkeypatch.setattr(move_job, "run", lambda *a, **k: real_run(*a, **k, move=fake_move))
    move_job.request(settings.data_dir, {"action": "apply", "root": "lib"})

    assert cli._move_once(settings) is True

    status = move_job.read_status(settings.data_dir)
    assert status.running is False and status.moved == 1 and (root / "02_3D/Props/剑.rar").exists()
    assert cli._move_once(settings) is False  # nothing asked
