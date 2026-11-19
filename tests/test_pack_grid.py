import io

from fastapi.testclient import TestClient
from PIL import Image

from assetindex import packs
from assetindex.config import Settings
from assetindex.db import connect
from assetindex.scanner import scan_root
from assetindex.web import create_app
from tests.test_uasset import make_uasset


def _library(tmp_path, with_thumbnails=True):
    root = tmp_path / "lib"
    pub = root / "UE/Pub - 酒馆/Content/Meshes"
    pub.mkdir(parents=True)
    body = make_uasset("PNG") if with_thumbnails else b"\xc1\x83\x2a\x9e" + b"\0" * 300
    for name in ("SM_Chair.uasset", "SM_Table.uasset", "SM_Barrel_01.uasset", "SM_Barrel_02.uasset"):
        (pub / name).write_bytes(body)
    return Settings(roots={"art": root}, data_dir=tmp_path / "data")


def _grid_url(settings):
    conn = connect(settings.db_path)
    scan_root(conn, "art", settings.roots["art"])
    url = packs.search_packs(conn, "").packs[0]["links"]["thumb"]
    conn.close()
    return url


def test_the_grid_is_built_once_from_the_uasset_thumbnails(tmp_path):
    settings = _library(tmp_path)
    url = _grid_url(settings)

    with TestClient(create_app(settings)) as client:
        first = client.get(url)
        again = client.get(url)

    assert url.startswith("/api/packs/grid?") and first.status_code == 200
    assert first.headers["content-type"] == "image/webp" and again.content == first.content
    with Image.open(io.BytesIO(first.content)) as sheet:
        assert sheet.size == (320, 320)
    assert len(list((settings.thumb_dir / "grids").rglob("*.webp"))) == 1  # cached


def test_no_thumbnail_in_any_uasset_is_no_grid(tmp_path):
    settings = _library(tmp_path, with_thumbnails=False)
    url = _grid_url(settings)

    with TestClient(create_app(settings)) as client:
        assert client.get(url).status_code == 415
        assert client.get("/api/packs/grid", params={"root": "nope", "path": "x"}).status_code == 404
