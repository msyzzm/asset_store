import json

import pytest
from PIL import Image

from assetindex import dupes, organize
from assetindex.caption import CaptionConfig
from assetindex.caption_job import Scope
from assetindex.config import Settings
from assetindex.organize import read_report, refresh_units, run_analysis
from assetindex.scanner import scan_root
from tests.fake_embed import FakeEmbedServer


@pytest.fixture(autouse=True)
def small_threshold(monkeypatch):
    monkeypatch.setattr(dupes, "HASH_MIN_BYTES", 100)


@pytest.fixture
def server():
    with FakeEmbedServer() as fake:
        yield fake


def _png(path, color):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 40), color).save(path)


@pytest.fixture
def lib(tmp_path, conn):
    root = tmp_path / "lib"
    for pack in ("Fire Pack", "Fire Pack (1)"):
        _png(root / "2D" / pack / "flame.png", (255, 0, 0))
        _png(root / "2D" / pack / "spark.png", (255, 60, 0))
    _png(root / "2D" / "Ice Pack" / "ice.png", (0, 0, 255))
    (root / "Audio").mkdir()
    (root / "Audio" / "boom.wav").write_bytes(b"RIFF" + bytes(200))
    settings = Settings(roots={"lib": root}, data_dir=tmp_path / "data")
    scan_root(conn, "lib", root)
    return conn, settings, root


def _config(server):
    return CaptionConfig(base_url=server.base_url, model="qwen")


def _rows(conn):
    return {r["path"]: r for r in conn.execute("SELECT * FROM organize_units")}


def test_analysis_marks_duplicates_and_proposes_a_place_for_everything_else(lib, server):
    conn, settings, _ = lib
    statuses = []

    final = run_analysis(conn, settings, Scope("lib"), _config(server), report=statuses.append)

    assert not final.running and final.error is None and final.done == 3 and final.failed == 0
    phases = [s.phase for s in statuses]
    assert phases[:2] == ["units", "sampling"] and "hashing" in phases
    rows = _rows(conn)
    assert set(rows) == {"2D/Fire Pack", "2D/Fire Pack (1)", "2D/Ice Pack", "Audio/boom.wav"}
    dup = rows["2D/Fire Pack (1)"]
    assert dup["dup_of"] == "2D/Fire Pack" and dup["target"] == "_Duplicates/Fire Pack (1)" and dup["status"] == "proposed"
    assert rows["2D/Fire Pack"]["target"] == "01_2D/VFX/Fake - 火焰特效"
    assert rows["2D/Ice Pack"]["category"] == "01_2D/Textures"
    assert rows["Audio/boom.wav"]["target"] == "03_Audio/SFX/Fake - 音效.wav"
    report = read_report(settings.data_dir)["lib"]
    assert report["dup_groups"] == 1 and report["dup_copies"] == 1 and report["dup_wasted"] > 0


def test_second_run_only_asks_about_new_packs(lib, server):
    conn, settings, root = lib
    run_analysis(conn, settings, Scope("lib"), _config(server))
    asked = len(server.requests)

    _png(root / "2D" / "New Pack" / "a.png", (0, 0, 255))
    scan_root(conn, "lib", root)
    final = run_analysis(conn, settings, Scope("lib"), _config(server))

    assert final.total == 1 and len(server.requests) == asked + 1


def test_proposals_of_changed_or_gone_packs_are_dropped(lib, server):
    conn, settings, root = lib
    run_analysis(conn, settings, Scope("lib"), _config(server))
    _png(root / "2D" / "Ice Pack" / "more.png", (0, 0, 200))
    (root / "Audio" / "boom.wav").unlink()
    scan_root(conn, "lib", root)

    refresh_units(conn, "lib")

    rows = _rows(conn)
    assert rows["2D/Ice Pack"]["status"] == "todo" and rows["2D/Ice Pack"]["target"] is None
    assert "Audio/boom.wav" not in rows


def test_decisions_survive_a_redo(lib, server):
    conn, settings, _ = lib
    run_analysis(conn, settings, Scope("lib"), _config(server))
    with conn:
        conn.execute("UPDATE organize_units SET status = 'accepted', target = '01_2D/UI/Mine' WHERE path = '2D/Ice Pack'")

    run_analysis(conn, settings, Scope("lib", redo=True), _config(server))

    row = _rows(conn)["2D/Ice Pack"]
    assert row["status"] == "accepted" and row["target"] == "01_2D/UI/Mine"


def test_scope_limits_classification_to_a_folder(lib, server):
    conn, settings, _ = lib
    final = run_analysis(conn, settings, Scope("lib", "Audio"), _config(server))
    rows = _rows(conn)
    assert final.done == 1 and rows["2D/Ice Pack"]["status"] == "todo" and rows["Audio/boom.wav"]["status"] == "proposed"


def test_scope_without_any_pack_says_so_instead_of_finishing_quietly(lib, server):
    conn, settings, _ = lib

    final = run_analysis(conn, settings, Scope("lib", "No Such Folder"), _config(server))

    assert final.total == 0 and "No Such Folder" in final.error


def test_split_turns_a_folder_into_several_packs(lib):
    conn, _, _ = lib
    with conn:
        conn.execute("INSERT INTO unit_splits (root, path) VALUES ('lib', '2D/Ice Pack')")
    refresh_units(conn, "lib")
    assert "2D/Ice Pack/ice.png" in _rows(conn) and "2D/Ice Pack" not in _rows(conn)


def test_api_down_stops_without_proposals(lib, server):
    conn, settings, _ = lib
    server.fail_status = 503

    final = run_analysis(conn, settings, Scope("lib"), _config(server))

    assert "503" in final.error and final.done == 0
    assert all(r["category"] is None for r in _rows(conn).values() if r["dup_of"] is None)


def test_unusable_answers_are_recorded_per_pack(lib, server, monkeypatch):
    conn, settings, _ = lib
    monkeypatch.setattr(organize, "MAX_CONSECUTIVE_FAILURES", 2)
    server.chat_reply = "说不清"

    final = run_analysis(conn, settings, Scope("lib"), _config(server))

    assert final.failed == 2 and "连续" in final.error
    assert sum(1 for r in _rows(conn).values() if r["error"]) == 2


def test_stop_between_packs(lib, server):
    conn, settings, _ = lib
    state = {"classifying": False, "checks": 0}

    def report(status):
        state["classifying"] = status.phase == "classifying"

    def should_stop():
        if state["classifying"]:
            state["checks"] += 1
        return state["checks"] > 1  # one pack, then stop

    final = run_analysis(conn, settings, Scope("lib"), _config(server), should_stop=should_stop, report=report)

    assert final.stopped and final.done == 1


def test_duplicate_marks_go_when_the_kept_copy_does(lib, server):
    import shutil

    conn, settings, root = lib
    run_analysis(conn, settings, Scope("lib"), _config(server))
    with conn:  # even a decided one: it would move the only copy left
        conn.execute("UPDATE organize_units SET status = 'accepted' WHERE path = '2D/Fire Pack (1)'")
    shutil.rmtree(root / "2D" / "Fire Pack")
    scan_root(conn, "lib", root)

    refresh_units(conn, "lib")

    row = _rows(conn)["2D/Fire Pack (1)"]
    assert row["dup_of"] is None and row["status"] == "todo" and row["target"] is None


def test_request_files(tmp_path):
    organize.request_job(tmp_path, Scope("lib", "2D"))
    assert organize.is_requested(tmp_path)
    assert organize.take_job(tmp_path) == {"root": "lib", "prefix": "2D", "redo": False}
    organize.request_stop(tmp_path)
    assert organize.stop_requested(tmp_path)
    organize.write_status(tmp_path, organize.OrganizeStatus(running=True, phase="hashing"))
    assert organize.read_status(tmp_path).phase == "hashing"
    (tmp_path / organize.STATUS_NAME).write_text(json.dumps({"running": 1}), encoding="utf-8")
    assert organize.read_status(tmp_path).running is False
