"""The 外部素材 search, against responses recorded from the real sites (tests/fixtures/external)."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from assetindex import downloads, ext_ambientcg, ext_itch, ext_opengameart, ext_polyhaven, external
from assetindex.scanner import scan_root
from assetindex.web import create_app

FIXTURES = Path(__file__).parent / "fixtures" / "external"
HEADERS = {"X-Requested-With": "fetch"}
HDRI = "kloofendal_48d_partly_cloudy_puresky"
TEXTURE = "brick_wall_001"
MODEL = "wooden_crate_01"


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


OGA_ASSET = "zelda-like-tilesets-and-sprites"
EMPTY_FEED = '<?xml version="1.0" encoding="UTF-8" ?><rss version="2.0"><channel><title>x</title></channel></rss>'


class FakeSites:
    """Answers get_json() and get_text() from the fixtures; `down` makes a site fail."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.down: set[str] = set()

    def _site(self, url: str) -> str:
        site = next((s for s in ("polyhaven", "opengameart", "itch.io") if s in url), "ambientcg")
        if site in self.down:
            raise external.SourceError("连不上网站：TimeoutError")
        return site

    def text(self, url: str) -> str:
        self.calls.append(url)
        if self._site(url) == "itch.io":
            return (FIXTURES / "itch_pixel_art.xml").read_text(encoding="utf-8") if "tag-pixel-art" in url else EMPTY_FEED
        if url == f"{ext_opengameart.BASE}/content/{OGA_ASSET}":
            return (FIXTURES / "oga_detail.html").read_text(encoding="utf-8")
        if "/art-search-advanced?" in url:
            name = "oga_empty.html" if "keys=nothing" in url else "oga_search.html"
            return (FIXTURES / name).read_text(encoding="utf-8")
        raise external.SourceError("网站返回 HTTP 404")

    def __call__(self, url: str):
        self.calls.append(url)
        self._site(url)
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
    monkeypatch.setattr(external, "get_text", fake.text)
    monkeypatch.setattr(ext_opengameart, "_gate", external.Gate(0, 0, "OpenGameArt"))
    for module, names in ((ext_polyhaven, ("_assets", "_details")), (ext_ambientcg, ("_searches", "_details")),
                          (ext_opengameart, ("_searches", "_details")), (ext_itch, ("_feeds", "_seen"))):
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


# ---- OpenGameArt (HTML pages) ----


def test_opengameart_search_reads_its_result_page(client, sites):
    body = client.get("/api/external/search", params={"q": "sword", "source": "opengameart"}).json()

    oga = body["sources"]["opengameart"]
    assert oga["total"] == 768 and len(oga["items"]) == 24 and oga["license"] == "授权各异"
    first = oga["items"][0]
    assert (first["id"], first["title"]) == (OGA_ASSET, "Zelda-like tilesets and sprites")
    assert first["thumb"].startswith("https://opengameart.org/sites/default/files/")
    assert first["in_library"] is None  # its ids are words, not in file names
    assert any("keys=sword" in c and "page=0" in c for c in sites.calls)


def test_opengameart_pages_follow_the_offset_and_nothing_found_is_empty(client, sites):
    client.get("/api/external/search", params={"q": "sword", "source": "opengameart", "offset": 48})
    body = client.get("/api/external/search", params={"q": "nothing", "source": "opengameart"}).json()

    assert any("page=2" in c for c in sites.calls)
    assert (body["sources"]["opengameart"]["items"], body["sources"]["opengameart"]["total"]) == ([], 0)


def test_opengameart_detail_reads_the_asset_page_but_not_its_comments(client):
    body = client.get(f"/api/external/opengameart/{OGA_ASSET}").json()

    assert body["title"] == "Zelda-like tilesets and sprites" and body["kind"] == "2D Art"
    assert (body["license"], body["authors"]) == ("CC0", "ArMM1998")
    assert body["tags"][:2] == ["pixelart", "zelda"] and body["description"].startswith("16x16 tilesets\n")
    assert len(body["previews"]) == 3 and not any("Zeldatest" in p for p in body["previews"])  # a comment's picture
    assert body["options"] == [{"key": "92549", "label": "gfx.zip", "size": 174776, "ready": True}]


def test_an_opengameart_file_downloads_into_a_folder_named_after_the_asset(client, conn, writable):
    row = client.post(f"/api/external/opengameart/{OGA_ASSET}/download", json={"option": "92549"}, headers=HEADERS).json()

    assert row["title"] == "Zelda-like tilesets and sprites gfx" and row["size"] == 174776
    parts = downloads.parts(conn, row["id"])
    assert [(p["url"], p["out"]) for p in parts] == [("https://opengameart.org/sites/default/files/gfx_3.zip", "gfx.zip")]


def _two_files(html: str) -> str:
    one = ('<a href="https://opengameart.org/sites/default/files/gfx_3.zip" type="application/zip; length=174776" '
           'data-fid="92549" target="_blank" download="gfx.zip">')
    two = ('<a href="https://opengameart.org/sites/default/files/gfx_4.zip" type="application/zip; length=10" '
           'data-fid="92550" target="_blank" download="GFX.zip">x</a> ')
    assert one in html
    return html.replace(one, two + one)


def test_several_opengameart_files_can_download_together(client, conn, sites, writable, monkeypatch):
    page = _two_files((FIXTURES / "oga_detail.html").read_text(encoding="utf-8"))
    monkeypatch.setattr(external, "get_text", lambda url: page)

    options = client.get(f"/api/external/opengameart/{OGA_ASSET}").json()["options"]
    row = client.post(f"/api/external/opengameart/{OGA_ASSET}/download", json={"option": "all"}, headers=HEADERS).json()

    assert options[0] == {"key": "all", "label": "全部 2 个文件", "size": 174786, "ready": True}
    assert [p["out"] for p in downloads.parts(conn, row["id"])] == ["GFX.zip", "92549-gfx.zip"]  # names kept apart
    assert row["title"].endswith("全部文件")


def test_an_opengameart_page_unlike_the_sites_is_a_502(client, monkeypatch):
    monkeypatch.setattr(external, "get_text", lambda url: "<html>maintenance</html>")

    assert client.get(f"/api/external/opengameart/{OGA_ASSET}").status_code == 502
    body = client.get("/api/external/search", params={"q": "x", "source": "opengameart"}).json()
    assert "看不懂" in body["sources"]["opengameart"]["error"]


def test_the_gate_spaces_requests_and_refuses_a_long_wait(monkeypatch):
    clock, slept = [100.0], []
    monkeypatch.setattr(external.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(external.time, "sleep", slept.append)
    gate = external.Gate(10, 15, "OpenGameArt")

    gate.wait()
    gate.wait()
    with pytest.raises(external.SourceError, match="10 秒"):
        gate.wait()  # would wait 20 s

    assert slept == [0, 10]


# ---- itch.io (tag feeds, no downloads) ----


def test_itch_lists_a_tags_free_assets_from_its_feed(client, sites):
    body = client.get("/api/external/search", params={"q": "Pixel Art", "source": "itch"}).json()

    itch = body["sources"]["itch"]
    assert (itch["total"], itch["more"], len(itch["items"])) == (None, True, 36)
    first = itch["items"][0]
    assert first["id"] == "ansimuz.gothicvania-patreon-collection"
    assert first["page"] == "https://ansimuz.itch.io/gothicvania-patreon-collection"
    assert first["title"].startswith("Legacy Collection") and first["thumb"].startswith("https://img.itch.zone/")
    assert sites.calls[-1] == "https://itch.io/game-assets/free/tag-pixel-art.xml"


def test_itch_pages_and_unknown_tags(client, sites):
    client.get("/api/external/search", params={"q": "pixel art", "source": "itch", "offset": 36})
    body = client.get("/api/external/search", params={"q": "brick wall", "source": "itch"}).json()

    assert "https://itch.io/game-assets/free/tag-pixel-art.xml?page=2" in sites.calls
    assert (body["sources"]["itch"]["items"], body["sources"]["itch"]["more"]) == ([], False)


def test_itch_needs_an_english_tag(client):
    body = client.get("/api/external/search", params={"q": "像素", "source": "itch"}).json()

    assert "英文标签" in body["sources"]["itch"]["error"]


def test_an_itch_detail_links_to_its_page_and_offers_no_download(client, writable):
    client.get("/api/external/search", params={"q": "pixel art", "source": "itch"})

    body = client.get("/api/external/itch/limezu.moderninteriors").json()
    unseen = client.get("/api/external/itch/someone.some-pack").json()

    assert body["title"].startswith("Modern Interiors") and body["description"] == "16x16 tilesets for your top-down game"
    assert body["options"] == [] and "原页面" in body["note"] and body["previews"]
    assert (unseen["page"], unseen["title"]) == ("https://someone.itch.io/some-pack", "some-pack")
    assert client.post("/api/external/itch/limezu.moderninteriors/download", json={"option": "x"},
                       headers=HEADERS).status_code == 422


def test_opengameart_pages_by_its_own_rows_even_when_one_is_skipped(sites):
    page = (FIXTURES / "oga_search.html").read_text(encoding="utf-8")
    page = page.replace('href="/content/a-blocky-dungeon"', 'href="/content/odd%20name"', 1)
    ext_opengameart._searches.put(("sword", 0), ext_opengameart._parse_search(page, 0))

    found = ext_opengameart.search("sword")

    assert len(found["items"]) == 23 and found["next"] == 24


def test_opengameart_totals_with_thousands_and_rows_it_cannot_read():
    page = (FIXTURES / "oga_search.html").read_text(encoding="utf-8")

    assert ext_opengameart._parse_search(page.replace("of 768", "of 36,273"), 0)["total"] == 36273
    with pytest.raises(external.SourceError):
        ext_opengameart._parse_search(page.replace("art-preview-title", "renamed"), 0)  # 768 said, none read
    assert ext_opengameart._parse_search(page.replace("art-preview-title", "renamed"), 768)["items"] == []  # past the end


def test_itch_tells_the_offset_of_its_next_page(client):
    itch = client.get("/api/external/search", params={"q": "pixel art", "source": "itch", "offset": 36}).json()["sources"]["itch"]

    assert itch["next"] == 72
