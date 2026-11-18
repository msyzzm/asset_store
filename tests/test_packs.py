"""Search by pack: every hit belongs to a pack (a folder or an archive with its previews),
and a pack is one result however many of its files match."""
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from assetindex import packs
from assetindex.archive_index import index_archives
from assetindex.config import Settings
from assetindex.db import connect
from assetindex.scanner import scan_root
from assetindex.web import create_app

ROBOTS = "02_3D/Characters/Rbots - 机器人角色"


def _write(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _zip(path, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name in members:
            z.writestr(name, b"data")


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "lib"
    city = root / "02_3D/Environments/SteamPunk City - 蒸汽朋克城市"
    for name in ("Content/Maps/City.umap", "Content/Textures/T_Brick.uasset"):
        _write(city / name)
    Image.new("RGB", (8, 8)).save(city / "preview.png")
    for i in range(12):  # an unpacked pack: many files, each a hit
        _write(root / ROBOTS / f"Content/Anim/Rbot_Steam_{i:02d}.uasset")
    _zip(root / "02_3D/Props/Gear Props - 齿轮道具.zip", ["Props/steam_valve.fbx", "Props/gear.fbx"])
    Image.new("RGB", (8, 8)).save(root / "02_3D/Props/Gear Props - 齿轮道具.png")
    _zip(root / "01_2D/Environments/Dungeon - 地牢瓦片.zip", ["topdown/dungeon.png"])
    _zip(root / "_待删除/Steam Old.zip", ["steam.txt"])
    settings = Settings(roots={"art": root}, data_dir=tmp_path / "data")
    conn = connect(settings.db_path)
    scan_root(conn, "art", root)
    index_archives(conn, "art", root)
    yield conn, settings
    conn.close()


def _paths(result):
    return [p["path"] for p in result.packs]


def test_a_pack_is_one_result_and_its_own_name_ranks_first(lib):
    conn, _ = lib

    result = packs.search_packs(conn, "steam")

    assert _paths(result) == [
        "art:/02_3D/Environments/SteamPunk City - 蒸汽朋克城市",  # the name says it
        f"art:/{ROBOTS}",  # 12 files inside say it
        "art:/02_3D/Props/Gear Props - 齿轮道具.zip",  # one file inside
    ]
    assert result.total == 3
    robots = result.packs[1]
    assert robots["match"] == "inside" and robots["hit_count"] == 12
    assert len(robots["hits"]) == packs.MAX_HITS and robots["hits"][0] == "Content/Anim/Rbot_Steam_00.uasset"
    assert result.packs[0]["match"] == "name"
    assert result.packs[2]["hits"] == ["Props/steam_valve.fbx"]  # inside the archive, not its own name
    assert result.packs[2]["hit_count"] == 1 and result.packs[2]["id"] and result.packs[0]["id"] is None


def test_the_page_opens_a_pack_by_its_id_or_its_folder(lib):
    conn, settings = lib
    client = TestClient(create_app(settings))
    gears = packs.search_packs(conn, "齿轮").packs[0]

    card = client.get(f"/api/files/{gears['id']}/card").json()
    inside = client.get("/api/search", params={"prefix": ROBOTS, "limit": 50}).json()

    assert card["rel_path"] == "02_3D/Props/Gear Props - 齿轮道具.zip" and card["entry_count"] == 2
    assert inside["total"] == 12 and all(i["rel_path"].startswith(ROBOTS + "/") for i in inside["items"])
    assert client.get("/api/files/999999/card").status_code == 404


def test_set_aside_packs_are_never_offered(lib):
    conn, _ = lib

    assert all("_待删除" not in p for p in _paths(packs.search_packs(conn, "steam")))


def test_a_pack_tells_what_it_is(lib):
    conn, _ = lib

    city = packs.search_packs(conn, "蒸汽朋克").packs[0]
    gears = packs.search_packs(conn, "齿轮").packs[0]

    assert city["kind"] == "folder" and city["files"] == 3 and city["dim"] == "3D" and city["engine"] == "unreal"
    assert city["category"] == "02_3D/Environments" and city["name"] == "SteamPunk City - 蒸汽朋克城市"
    assert gears["kind"] == "archive" and gears["files"] == 2  # the zip and its preview
    assert gears["links"]["detail"].startswith("/api/agent/files/") and "thumb" in gears["links"]
    assert packs.search_packs(conn, "地牢").packs[0]["dim"] == "2D"


def test_category_filter_and_paging(lib):
    conn, _ = lib

    assert _paths(packs.search_packs(conn, "steam", category="02_3D/Props")) == ["art:/02_3D/Props/Gear Props - 齿轮道具.zip"]
    page = packs.search_packs(conn, "steam", limit=1, offset=1)
    assert page.total == 3 and _paths(page) == [f"art:/{ROBOTS}"]


def test_after_the_named_packs_meaning_orders_the_rest(lib):
    conn, _ = lib
    ids = dict(conn.execute("SELECT name, id FROM files"))
    meaning = [ids["Dungeon - 地牢瓦片.zip"], ids["Gear Props - 齿轮道具.zip"], ids["T_Brick.uasset"]]

    result = packs.search_packs(conn, "steam", meaning=meaning)

    assert _paths(result) == [
        "art:/02_3D/Environments/SteamPunk City - 蒸汽朋克城市",  # its name says it: first, whatever the meaning
        "art:/01_2D/Environments/Dungeon - 地牢瓦片.zip",  # closest in meaning
        "art:/02_3D/Props/Gear Props - 齿轮道具.zip",  # a word inside, and close in meaning
        f"art:/{ROBOTS}",  # words inside only: 12 hits, but nothing in meaning
    ]
    assert [p["match"] for p in result.packs] == ["name", "meaning", "inside", "inside"] and result.total == 4


def test_previews_are_found_as_the_organizer_pairs_them(tmp_path):
    root = tmp_path / "lib"
    _zip(root / "02_3D/Props/hong kong-street.zip", ["street/sign.fbx"])
    Image.new("RGB", (8, 8)).save(root / "02_3D/Props/Hong_Kong Street.png")  # other case and separators
    _zip(root / "02_3D/Props/Megapack 4.26.zip", ["mega/a.fbx"])
    Image.new("RGB", (8, 8)).save(root / "02_3D/Props/Megapack.jpg")  # the preview's name is a prefix
    _zip(root / "02_3D/PropsX/Other sign.zip", ["sign.fbx"])
    conn = connect(tmp_path / "x.db")
    scan_root(conn, "art", root)
    index_archives(conn, "art", root)

    found = {p["name"]: p for p in packs.search_packs(conn, "fbx", category="02_3D/Props").packs}

    assert set(found) == {"hong kong-street.zip", "Megapack 4.26.zip"}  # not 02_3D/PropsX
    assert found["hong kong-street.zip"]["files"] == 2 and "thumb" in found["hong kong-street.zip"]["links"]
    assert found["Megapack 4.26.zip"]["files"] == 2
    assert packs.search_packs(conn, "zip").packs[0]["match"] == "inside"  # ".zip" is not the name
    conn.close()


def test_the_agent_endpoint_lists_packs(lib):
    _, settings = lib
    client = TestClient(create_app(settings))

    body = client.get("/api/agent/packs", params={"q": "steam", "limit": 2}).json()

    assert body["total"] == 3 and body["mode"] == "text" and len(body["packs"]) == 2
    assert body["packs"][0]["name"] == "SteamPunk City - 蒸汽朋克城市"
    assert client.get("/api/agent/packs", params={"q": ""}).status_code == 422
