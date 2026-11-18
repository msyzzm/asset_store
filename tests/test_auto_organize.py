"""After a scan, new packs in _下载 get an analysis requested (proposals only)."""

import pytest

from assetindex import cli, organize
from assetindex.caption import CaptionConfig, save_config
from assetindex.config import Settings
from assetindex.db import connect
from assetindex.scanner import scan_root


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "lib"
    for rel in ("_下载/Forest Pack/tree.png", "_下载/sword.zip", "2D/Old Pack/a.png"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(rel.encode())
    settings = Settings(roots={"lib": root}, data_dir=tmp_path / "data")
    conn = connect(settings.db_path)
    scan_root(conn, "lib", root)
    yield conn, settings
    conn.close()


def test_new_packs_in_downloads_get_an_analysis(env):
    conn, settings = env
    assert cli._auto_organize(conn, settings, ["lib"]) is None  # no model set up
    save_config(settings.data_dir, CaptionConfig(base_url="http://127.0.0.1:9/v1", model="m"))

    assert cli._auto_organize(conn, settings, ["lib"]) == "lib"
    assert organize.take_job(settings.data_dir) == {"root": "lib", "prefix": "_下载", "redo": False}


def test_only_packs_never_asked_about(env):
    conn, settings = env
    save_config(settings.data_dir, CaptionConfig(base_url="http://127.0.0.1:9/v1", model="m"))
    organize.refresh_units(conn, "lib")
    with conn:
        conn.execute("UPDATE organize_units SET status = 'proposed', target = '01_2D/x' WHERE path = '_下载/Forest Pack'")
        conn.execute("UPDATE organize_units SET error = '模型答不出' WHERE path = '_下载/sword.zip'")

    assert cli._auto_organize(conn, settings, ["lib"]) is None  # one proposed, one failed: nothing new
    assert not organize.is_requested(settings.data_dir)


def test_an_analysis_already_waiting_is_left_alone(env):
    conn, settings = env
    save_config(settings.data_dir, CaptionConfig(base_url="http://127.0.0.1:9/v1", model="m"))
    organize.request_job(settings.data_dir, organize.Scope(root="lib", prefix="2D"))

    assert cli._auto_organize(conn, settings, ["lib"]) is None
    assert organize.take_job(settings.data_dir)["prefix"] == "2D"
