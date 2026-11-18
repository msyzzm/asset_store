"""The 外部素材 search, against responses recorded from the real sites (tests/fixtures/external)."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from assetindex import downloads, ext_ambientcg, ext_polyhaven, external
from assetindex.scanner import scan_root
from assetindex.web import create_app

FIXTURES = Path(__file__).parent / "fixtures" / "external"
HEADERS = {"X-Requested-With": "fetch"}
HDRI = "kloofendal_48d_partly_cloudy_puresky"
TEXTURE = "brick_wall_001"
MODEL = "wooden_crate_01"


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeSites:
    """Answers get_json() from the fixtures; `down` makes a site fail."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.down: set[str] = set()

    def __call__(self, url: str):
        self.calls.append(url)
        site = "polyhaven" if "polyhaven" in url else "ambientcg"
        if site in self.down:
            raise external.SourceError("连不上网站：TimeoutError")
        if url == f"{ext_polyhaven.API}/assets":
            return _fixture("polyhaven_assets.json")
        if url == f"{ext_polyhaven.API}/info/{HDRI}":
            return _fixture("polyhaven_info_hdri.json")
        if url == f"{ext_polyhaven.API}/files/{HDRI}":
            return _fixture("polyhaven_files_hdri.json")
        for asset, kind in ((TEXTURE, "texture"), (MODEL, "model")):
            if url.endswith(f"/{asset}"):
                return _fixture(f"polyhaven_{'info' if '/info/' in url else 'files'}_{kind}.json")
        if "id=Bricks113A" in url:
            return _fixture("ambientcg_one.json")
        if "id=" in url:
            return {"numberOfResults": 0, "foundAssets": []}
        if url.startswith(ext_ambientcg.BASE):
            return _fixture("ambientcg_search.json")
        raise external.SourceError("HTTP 404")


@pytest.fixture
def sites(monkeypatch):
    fake = FakeSites()
    monkeypatch.setattr(external, "get_json", fake)
    for module, names in ((ext_polyhaven, ("_assets", "_details")), (ext_ambientcg, ("_searches", "_details"))):
        for name in names:
            monkeypatch.setattr(module, name, external.TtlCache(3600))
    return fake


@pytest.fixture
def client(settings, sites):
    return TestClient(create_app(settings))


def test_search_asks_both_sites_and_matches_poly_haven_locally(client, sites):
    body = client.get("/api/external/search", params={"q": "brick wall"}).json()

    ph, acg = body["sources"]["polyhaven"], body["sources"]["ambientcg"]
    ids = [i["id"] for i in ph["items"]]
    assert "brick_wall_001" in ids and HDRI not in ids and "wooden_crate_01" not in ids
    item = ph["items"][ids.index("brick_wall_001")]
    assert (item["kind"], item["page"]) == ("贴图", "https://polyhaven.com/a/brick_wall_001")
    assert item["thumb"].startswith("https://cdn.polyhaven.com/")
    assert acg["total"] == 156 and acg["items"][0]["id"] == "Bricks113A" and acg["items"][0]["kind"] == "材质"
    assert any("q=brick+wall" in c for c in sites.calls)
    assert body["chinese"] is False


def test_poly_haven_list_is_fetched_once_and_cached(client, sites):
    client.get("/api/external/search", params={"q": "brick", "source": "polyhaven"})
    client.get("/api/external/search", params={"q": "sky", "source": "polyhaven"})

    assert sites.calls.count(f"{ext_polyhaven.API}/assets") == 1


def test_one_site_down_still_shows_the_other(client, sites):
    sites.down.add("ambientcg")

    body = client.get("/api/external/search", params={"q": "brick"}).json()

    assert "连不上" in body["sources"]["ambientcg"]["error"]
    assert body["sources"]["polyhaven"]["items"]


def test_a_stale_poly_haven_list_is_used_when_the_site_is_down(sites):
    ext_polyhaven._assets.put("all", _fixture("polyhaven_assets.json"))
    ext_polyhaven._assets.seconds = 0  # everything is stale
    sites.down.add("polyhaven")

    assert ext_polyhaven.search("brick")["items"]


def test_a_chinese_query_is_flagged(client):
    assert client.get("/api/external/search", params={"q": "砖墙"}).json()["chinese"] is True


def test_items_in_the_library_or_the_queue_are_marked(client, conn, settings):
    folder = settings.roots["art"] / "_下载"
    folder.mkdir()
    (folder / "Bricks113A_1K-JPG.zip").write_bytes(b"zip")
    scan_root(conn, "art", settings.roots["art"])
    downloads.add(conn, "art", "https://example.org/x.zip", source="polyhaven", source_id="brick_wall_001")

    body = client.get("/api/external/search", params={"q": "brick"}).json()

    acg = {i["id"]: i for i in body["sources"]["ambientcg"]["items"]}
    ph = {i["id"]: i for i in body["sources"]["polyhaven"]["items"]}
    assert acg["Bricks113A"]["in_library"] == "art:/_下载/Bricks113A_1K-JPG.zip"
    assert acg["Bricks105"]["in_library"] is None
    assert ph["brick_wall_001"]["downloaded"] is True and acg["Bricks113A"]["downloaded"] is False


def test_ambientcg_detail_lists_its_zips(client):
    body = client.get("/api/external/ambientcg/Bricks113A").json()

    assert body["license"] == "CC0" and body["previews"][0].endswith(".png")
    keys = [o["key"] for o in body["options"]]
    assert "1K-JPG" in keys and all(o["ready"] for o in body["options"])
    assert next(o for o in body["options"] if o["key"] == "1K-JPG")["size"] == 7839867


def test_poly_haven_hdri_detail_offers_single_files(client):
    body = client.get(f"/api/external/polyhaven/{HDRI}").json()

    assert body["kind"] == "HDRI" and "Greg Zaal" in body["authors"]
    labels = {o["key"]: o for o in body["options"]}
    assert labels["hdri/2k/hdr"]["ready"] and labels["hdri/1k/exr"]["size"] > 0


def test_poly_haven_texture_options_are_whole_map_sets(client):
    body = client.get(f"/api/external/polyhaven/{TEXTURE}").json()

    assert body["kind"] == "贴图"
    assert [o["key"] for o in body["options"]] == ["maps/1k/jpg", "maps/1k/png"] and all(o["ready"] for o in body["options"])


def test_poly_haven_model_options_are_each_format_with_its_files(client):
    keys = [o["key"] for o in client.get(f"/api/external/polyhaven/{MODEL}").json()["options"]]

    assert keys == ["fbx/1k", "gltf/1k", "blend/1k"]


def test_a_texture_set_downloads_as_one_folder_of_maps(client, conn, writable):
    row = client.post(f"/api/external/polyhaven/{TEXTURE}/download", json={"option": "maps/1k/jpg"}, headers=HEADERS).json()

    parts = downloads.parts(conn, row["id"])
    outs = [p["out"] for p in parts]
    assert row["url"] == f"https://polyhaven.com/a/{TEXTURE}" and row["title"] == "Brick Wall 001 1K JPG 全套贴图"
    assert "brick_wall_001_diffuse_1k.jpg" in outs and all("/" not in o and o.endswith(".jpg") for o in outs)
    assert all(len(p["md5"]) == 32 for p in parts) and row["size"] == sum(p["size"] for p in parts)


def test_a_model_downloads_with_the_files_it_includes_at_their_paths(client, conn, writable):
    row = client.post(f"/api/external/polyhaven/{MODEL}/download", json={"option": "gltf/1k"}, headers=HEADERS).json()

    outs = [p["out"] for p in downloads.parts(conn, row["id"])]
    assert outs[0] == "wooden_crate_01_1k.gltf" and "wooden_crate_01.bin" in outs
    assert any(o.startswith("textures/") for o in outs)


def test_unknown_sites_and_assets(client):
    assert client.get("/api/external/nope/x").status_code == 404
    assert client.get("/api/external/ambientcg/Missing99").status_code == 404
    assert client.get("/api/external/ambientcg/..%2Fetc").status_code in (404, 422)
    assert client.get("/api/external/ambientcg/a%20b").status_code == 422


@pytest.fixture
def writable(settings):
    downloads.write_mount(settings.data_dir, {"art": True}, has_aria2=True)


def test_downloading_an_option_queues_the_sites_own_link(client, conn, writable):
    response = client.post("/api/external/ambientcg/Bricks113A/download", json={"option": "1K-JPG"}, headers=HEADERS)

    assert response.status_code == 201
    row = response.json()
    assert row["url"] == "https://ambientcg.com/get?file=Bricks113A_1K-JPG.zip"
    assert (row["source"], row["source_id"], row["size"]) == ("ambientcg", "Bricks113A", 7839867)
    assert row["title"].endswith("1K-JPG")


def test_a_poly_haven_hdri_download_carries_its_md5(client, writable):
    row = client.post(f"/api/external/polyhaven/{HDRI}/download", json={"option": "hdri/1k/hdr"}, headers=HEADERS).json()

    assert row["url"].startswith("https://dl.polyhaven.org/") and len(row["md5"]) == 32


@pytest.mark.parametrize("body", [{}, {"option": ""}, {"option": "9K-JPG"}, {"option": "maps/1k/jpg"}, {"option": "x", "url": "y"}])
def test_options_that_cannot_be_downloaded_are_refused(client, writable, body):
    # maps/1k/jpg asked of an HDRI: an option of another kind of asset
    source, asset = ("polyhaven", HDRI) if body.get("option", "").startswith("maps") else ("ambientcg", "Bricks113A")

    assert client.post(f"/api/external/{source}/{asset}/download", json=body, headers=HEADERS).status_code == 422


def test_downloading_needs_the_page_header_and_a_writable_fetcher(client, settings):
    url = "/api/external/ambientcg/Bricks113A/download"
    assert client.post(url, json={"option": "1K-JPG"}).status_code == 403
    assert client.post(url, json={"option": "1K-JPG"}, headers=HEADERS).status_code == 409


def test_an_answer_shaped_unlike_the_sites_is_a_502_and_not_cached(client, sites, monkeypatch):
    monkeypatch.setattr(external, "get_json", lambda url: {"foundAssets": [{"assetId": "Bricks113A", "downloadFolders": 7}]}
                        if "id=" in url else ["not", "a", "dict"])

    assert client.get("/api/external/ambientcg/Bricks113A").status_code == 502
    body = client.get("/api/external/search", params={"q": "x", "source": "polyhaven"}).json()
    assert "看不懂" in body["sources"]["polyhaven"]["error"]
    assert ext_polyhaven._assets.get("all") == (None, False)


def test_links_from_a_site_must_be_web_links(client, sites, monkeypatch):
    raw = _fixture("ambientcg_one.json")
    raw["foundAssets"][0]["shortLink"] = "javascript:alert(1)"
    raw["foundAssets"][0]["previewImage"]["1024-PNG"] = "data:image/png;base64,AAAA"
    monkeypatch.setattr(external, "get_json", lambda url: raw)

    body = client.get("/api/external/ambientcg/Bricks113A").json()

    assert body["page"] == "https://ambientcg.com/view?id=Bricks113A" and body["previews"] == []


def test_a_site_down_serves_the_stale_answer_without_asking_again_each_time(sites):
    ext_polyhaven._assets.put("all", _fixture("polyhaven_assets.json"))
    ext_polyhaven._assets.seconds = 0.5
    ext_polyhaven._assets._items["all"] = (0.0, ext_polyhaven._assets._items["all"][1])  # long expired
    sites.down.add("polyhaven")

    ext_polyhaven.search("brick")
    ext_polyhaven.search("brick")

    assert sites.calls.count(f"{ext_polyhaven.API}/assets") == 1


def test_ttl_cache_drops_the_oldest_when_full():
    cache = external.TtlCache(60, max_items=2)
    for key in "abc":
        cache.put(key, key)

    assert cache.get("a") == (None, False) and cache.get("c") == ("c", True)
