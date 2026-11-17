import pytest
from fastapi.testclient import TestClient
from PIL import Image

from assetindex import cli, dupes, organize
from assetindex.caption import CaptionConfig, save_config
from assetindex.config import Settings
from assetindex.organize_web import check_target
from assetindex.scanner import scan_root
from assetindex.web import create_app
from tests.fake_embed import FakeEmbedServer

PAGE = {"X-Requested-With": "fetch"}


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
def env(tmp_path, conn, server):
    root = tmp_path / "lib"
    for pack in ("Fire Pack", "Fire Pack (1)"):
        _png(root / "2D" / pack / "flame.png", (255, 0, 0))
    _png(root / "2D" / "Ice Pack" / "a" / "ice.png", (0, 0, 255))
    _png(root / "2D" / "Ice Pack" / "b" / "frost.png", (0, 0, 200))
    settings = Settings(roots={"lib": root}, data_dir=tmp_path / "data")
    scan_root(conn, "lib", root)
    save_config(settings.data_dir, CaptionConfig(base_url=server.base_url, model="qwen"))
    return TestClient(create_app(settings)), settings


def _analyze(client, settings):
    assert client.post("/api/organize", headers=PAGE, json={"root": "lib"}).status_code == 202
    assert cli._organize_once(settings) is True


def _units(client, show="all"):
    return {u["path"]: u for u in client.get("/api/organize/units", params={"root": "lib", "show": show}).json()["items"]}


def test_start_runs_in_the_captioner_and_fills_the_page(env):
    client, settings = env
    assert client.post("/api/organize", json={"root": "lib"}).status_code == 403

    _analyze(client, settings)

    summary = client.get("/api/organize").json()
    assert summary["job"]["running"] is False and summary["job"]["done"] == 2
    assert summary["counts"]["lib"]["duplicates"] == 1 and summary["counts"]["lib"]["proposed"] == 2
    assert summary["report"]["lib"]["dup_groups"] == 1
    assert any(c["path"] == "01_2D/VFX" for c in summary["categories"])
    units = _units(client, "proposed")
    assert units["2D/Fire Pack"]["target"] == "01_2D/VFX/Fake - 火焰特效"
    assert units["2D/Fire Pack"]["thumb"].startswith("/api/files/")
    assert list(_units(client, "duplicates")) == ["2D/Fire Pack (1)"]


def test_decisions_and_edits(env):
    client, settings = env
    _analyze(client, settings)

    done = client.post("/api/organize/units/decide", headers=PAGE,
                       json={"root": "lib", "paths": ["2D/Fire Pack", "2D/Ice Pack"], "action": "accept"})
    assert done.json()["changed"] == 2
    assert set(_units(client, "accepted")) == {"2D/Fire Pack", "2D/Ice Pack"}

    edit = client.post("/api/organize/units/target", headers=PAGE,
                       json={"root": "lib", "path": "2D/Ice Pack", "target": "01_2D/UI/冰系图标"})
    assert edit.status_code == 200 and _units(client)["2D/Ice Pack"]["target"] == "01_2D/UI/冰系图标"
    clash = client.post("/api/organize/units/target", headers=PAGE,
                        json={"root": "lib", "path": "2D/Ice Pack", "target": "01_2D/VFX/Fake - 火焰特效"})
    assert clash.status_code == 409
    bad = client.post("/api/organize/units/target", headers=PAGE,
                      json={"root": "lib", "path": "2D/Ice Pack", "target": "../etc/x"})
    assert bad.status_code == 422

    client.post("/api/organize/units/decide", headers=PAGE, json={"root": "lib", "paths": ["2D/Ice Pack"], "action": "reset"})
    assert _units(client)["2D/Ice Pack"]["status"] == "proposed"


def test_keeping_the_other_copy_swaps_the_proposals(env):
    client, settings = env
    _analyze(client, settings)

    assert client.post("/api/organize/units/keep", headers=PAGE, json={"root": "lib", "path": "2D/Fire Pack (1)"}).status_code == 200

    units = _units(client)
    assert units["2D/Fire Pack (1)"]["target"] == "01_2D/VFX/Fake - 火焰特效" and units["2D/Fire Pack (1)"]["dup_of"] is None
    assert units["2D/Fire Pack"]["dup_of"] == "2D/Fire Pack (1)" and units["2D/Fire Pack"]["target"].startswith("_Duplicates/")
    assert client.post("/api/organize/units/keep", headers=PAGE, json={"root": "lib", "path": "2D/Ice Pack"}).status_code == 409


def test_split_and_unsplit(env):
    client, settings = env
    _analyze(client, settings)

    client.post("/api/organize/split", headers=PAGE, json={"root": "lib", "path": "2D/Ice Pack"})
    units = _units(client)
    assert {"2D/Ice Pack/a", "2D/Ice Pack/b"} <= set(units) and units["2D/Ice Pack/a"]["status"] == "todo"
    assert client.get("/api/organize/splits", params={"root": "lib"}).json() == {"splits": ["2D/Ice Pack"]}

    client.post("/api/organize/unsplit", headers=PAGE, json={"root": "lib", "path": "2D/Ice Pack"})
    assert "2D/Ice Pack" in _units(client)


def test_join_keeps_a_folder_as_one_pack(env):
    client, settings = env
    _analyze(client, settings)
    client.post("/api/organize/split", headers=PAGE, json={"root": "lib", "path": "2D/Ice Pack"})

    assert client.post("/api/organize/join", headers=PAGE, json={"root": "lib", "path": "2D"}).status_code == 409
    assert client.post("/api/organize/join", headers=PAGE, json={"root": "lib", "path": "2D/Nope"}).status_code == 404
    assert client.post("/api/organize/join", headers=PAGE, json={"root": "lib", "path": "2D/Ice Pack"}).status_code == 200

    units = _units(client)
    assert "2D/Ice Pack" in units and "2D/Ice Pack/a" not in units
    assert client.get("/api/organize/splits", params={"root": "lib"}).json() == {"splits": []}
    client.post("/api/organize/split", headers=PAGE, json={"root": "lib", "path": "2D/Ice Pack"})
    assert "2D/Ice Pack/a" in _units(client)  # a split takes the join back


def test_join_refuses_to_drop_decisions(env):
    client, settings = env
    _analyze(client, settings)
    client.post("/api/organize/split", headers=PAGE, json={"root": "lib", "path": "2D/Ice Pack"})
    client.post("/api/organize/units/target", headers=PAGE,
                json={"root": "lib", "path": "2D/Ice Pack/a", "target": "01_2D/UI/冰"})

    assert client.post("/api/organize/join", headers=PAGE, json={"root": "lib", "path": "2D/Ice Pack"}).status_code == 409


def test_a_decided_folder_pack_stays_whole_after_the_decision_is_undone(env):
    from assetindex.db import connect

    client, settings = env
    _analyze(client, settings)

    client.post("/api/organize/units/target", headers=PAGE, json={"root": "lib", "path": "2D/Ice Pack", "target": "01_2D/VFX/冰"})
    client.post("/api/organize/units/decide", headers=PAGE, json={"root": "lib", "paths": ["2D/Ice Pack"], "action": "reset"})

    conn = connect(settings.db_path)
    joins = [r[0] for r in conn.execute("SELECT path FROM unit_joins")]
    conn.close()
    assert joins == ["2D/Ice Pack"]  # undoing the decision does not let the rules split it


def test_a_top_level_folder_that_is_no_category_can_be_joined(env):
    client, settings = env
    root = settings.roots["lib"]
    for name in ("Barrels", "Crates", "Lamps"):
        (root / "OverHit").mkdir(exist_ok=True)
        (root / "OverHit" / f"{name}.zip").write_bytes(name.encode())
    from assetindex.db import connect
    conn = connect(settings.db_path)
    scan_root(conn, "lib", root)
    conn.close()
    _analyze(client, settings)
    assert "OverHit/Barrels.zip" in _units(client)  # three packs inside: opened by the rules

    assert client.post("/api/organize/join", headers=PAGE, json={"root": "lib", "path": "OverHit"}).status_code == 200

    units = _units(client)
    assert "OverHit" in units and "OverHit/Barrels.zip" not in units


def test_start_needs_the_model_and_one_job_at_a_time(env):
    client, settings = env
    (settings.data_dir / "caption.json").unlink()
    assert client.post("/api/organize", headers=PAGE, json={"root": "lib"}).status_code == 409
    save_config(settings.data_dir, CaptionConfig(base_url="http://127.0.0.1:9/v1", model="m"))
    assert client.post("/api/organize", headers=PAGE, json={"root": "nope"}).status_code == 422
    assert client.post("/api/organize", headers=PAGE, json={"root": "lib"}).status_code == 202
    assert client.post("/api/organize", headers=PAGE, json={"root": "lib"}).status_code == 409
    stopped = client.post("/api/organize/stop", headers=PAGE).json()
    assert stopped["requested"] is False and organize.stop_requested(settings.data_dir)


def test_dead_job_is_reported(env):
    client, settings = env
    organize.write_status(settings.data_dir, organize.OrganizeStatus(running=True, phase="classifying"))
    job = client.get("/api/organize").json()["job"]
    assert job["running"] is False and "中途断了" in job["error"]
    assert "organize" in client.get("/api/agent/tasks").json()


def test_renaming_a_file_keeps_its_extension(env, conn):
    client, settings = env
    (settings.roots["lib"] / "2D" / "pack.zip").write_bytes(b"PK")
    scan_root(conn, "lib", settings.roots["lib"])
    _analyze(client, settings)

    done = client.post("/api/organize/units/target", headers=PAGE,
                       json={"root": "lib", "path": "2D/pack.zip", "target": "01_2D/UI/图标包"})

    assert done.json()["target"] == "01_2D/UI/图标包.zip"


@pytest.mark.parametrize("target", ["01_2D/UI", "Nope/x", "01_2D/UI/a:b", "01_2D/UI/ x", "01_2D/UI/.."])
def test_bad_targets(target):
    with pytest.raises(ValueError):
        check_target(target)


def test_kept_copy_survives_the_next_analysis(env):
    client, settings = env
    _analyze(client, settings)
    client.post("/api/organize/units/keep", headers=PAGE, json={"root": "lib", "path": "2D/Fire Pack (1)"})

    _analyze(client, settings)

    units = _units(client)
    assert units["2D/Fire Pack (1)"]["dup_of"] is None and units["2D/Fire Pack (1)"]["target"].startswith("01_2D/VFX/")
    assert units["2D/Fire Pack"]["dup_of"] == "2D/Fire Pack (1)"


def test_keep_refuses_to_overrule_a_decision(env):
    client, settings = env
    _analyze(client, settings)
    client.post("/api/organize/units/decide", headers=PAGE, json={"root": "lib", "paths": ["2D/Fire Pack"], "action": "accept"})

    resp = client.post("/api/organize/units/keep", headers=PAGE, json={"root": "lib", "path": "2D/Fire Pack (1)"})

    assert resp.status_code == 409 and _units(client)["2D/Fire Pack"]["status"] == "accepted"


def test_decided_duplicates_are_counted_with_their_decision(env):
    client, settings = env
    _analyze(client, settings)
    client.post("/api/organize/units/decide", headers=PAGE, json={"root": "lib", "paths": ["2D/Fire Pack (1)"], "action": "accept"})

    counts = client.get("/api/organize").json()["counts"]["lib"]

    assert counts["duplicates"] == 0 and counts["accepted"] == 1
    assert list(_units(client, "accepted")) == ["2D/Fire Pack (1)"]


def test_unsplit_forgets_nested_splits(env):
    client, settings = env
    _analyze(client, settings)
    client.post("/api/organize/split", headers=PAGE, json={"root": "lib", "path": "2D/Ice Pack"})
    client.post("/api/organize/split", headers=PAGE, json={"root": "lib", "path": "2D/Ice Pack/a"})

    client.post("/api/organize/unsplit", headers=PAGE, json={"root": "lib", "path": "2D/Ice Pack"})

    assert client.get("/api/organize/splits", params={"root": "lib"}).json() == {"splits": []}


def test_command_line_analysis(env, monkeypatch):
    client, settings = env
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    assert cli.main(["organize", "--root", "lib", "--prefix", "2D"]) == 0
    assert _units(client, "proposed")


def test_redo_sends_a_pack_back_to_the_model_but_not_a_decided_one(env):
    client, settings = env
    _analyze(client, settings)
    client.post("/api/organize/units/decide", headers=PAGE, json={"root": "lib", "paths": ["2D/Fire Pack"], "action": "accept"})

    done = client.post("/api/organize/units/decide", headers=PAGE,
                       json={"root": "lib", "paths": ["2D/Ice Pack", "2D/Fire Pack", "2D/Fire Pack (1)"], "action": "redo"})

    assert done.json() == {"changed": 1}  # the accepted one and the duplicate stay as they are
    units = _units(client)
    assert units["2D/Ice Pack"]["status"] == "todo" and units["2D/Ice Pack"]["target"] is None
    assert units["2D/Fire Pack"]["status"] == "accepted" and units["2D/Fire Pack (1)"]["dup_of"]
    _analyze(client, settings)
    assert _units(client)["2D/Ice Pack"]["status"] == "proposed"
