import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from assetindex import fab_covers, packs
from assetindex.config import Settings
from assetindex.db import connect
from assetindex.fab_covers import FabError, match_pending
from assetindex.scanner import scan_root
from assetindex.web import create_app

UID = "0f8fad5b-d9cb-469f-a165-70867728950e"
OTHER_UID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


def _listing(title, uid=UID, formats=("unreal-engine",)):
    return {
        "title": title,
        "uid": uid,
        "assetFormats": [{"assetFormatType": {"code": code}} for code in formats],
        "thumbnails": [{"images": [
            {"url": f"https://media.fab.com/{uid}/small.jpg", "width": 320},
            {"url": f"https://media.fab.com/{uid}/big.jpg", "width": 640},
            {"url": f"https://media.fab.com/{uid}/huge.jpg", "width": 1280},
        ]}],
    }


def _jpeg():
    out = io.BytesIO()
    Image.new("RGB", (64, 36), (200, 40, 40)).save(out, "JPEG")
    return out.getvalue()


class _Fab:
    """Stands in for fab.com: answers by query, counts what was asked."""

    def __init__(self, answers, fail_on=None):
        self.answers, self.fail_on = answers, fail_on
        self.queries, self.fetched = [], []

    def find(self, query):
        self.queries.append(query)
        if query == self.fail_on:
            raise FabError("www.fab.com: HTTP Error 429: Too Many Requests")
        return self.answers.get(query, [])

    def fetch(self, url):
        self.fetched.append(url)
        return _jpeg()


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "index.db")
    yield connection
    connection.close()


def _run(conn, tmp_path, names, fab):
    return match_pending(conn, tmp_path, names, find=fab.find, fetch=fab.fetch, sleep=lambda _: None)


def test_names_are_read_as_fab_titles_are_written():
    assert fab_covers.name_of("02_3D/Characters/SwordAnimsetPro - 剑术动作.zip") == "SwordAnimsetPro"
    assert fab_covers.query_of("SwordAnimsetPro") == "Sword Animset Pro"
    assert fab_covers.query_of("Crystal_cave") == "Crystal cave"
    assert fab_covers.key_of("Sword Animset Pro") == fab_covers.key_of("SwordAnimsetPro")


def test_the_one_unreal_listing_with_the_name_gives_the_cover(conn, tmp_path):
    fab = _Fab({"Sword Animset Pro": [_listing("Other Pack", OTHER_UID), _listing("Sword Animset Pro")]})

    stats = _run(conn, tmp_path, ["SwordAnimsetPro"], fab)

    assert (stats.checked, stats.matched, stats.error) == (1, 1, None)
    assert fab.fetched == [f"https://media.fab.com/{UID}/big.jpg"]  # the smallest one at least 640 wide
    assert (tmp_path / "fab_covers" / f"{UID}.webp").is_file()
    assert fab_covers.cover_url(conn, "02_3D/Anim/SwordAnimsetPro - 剑术.zip") == f"/api/fab-covers/{UID}.webp"
    assert json.loads((tmp_path / "fab.status").read_text())["matched"] == 1


def test_no_cover_when_the_name_is_shared_or_not_unreal(conn, tmp_path):
    fab = _Fab({
        "Tropical Island": [_listing("Tropical Island"), _listing("Tropical Island", OTHER_UID)],
        "Low Poly Barrel": [_listing("Low Poly Barrel", formats=("fbx", "blender"))],
    })

    stats = _run(conn, tmp_path, ["Tropical Island", "LowPolyBarrel"], fab)

    assert (stats.checked, stats.matched) == (2, 0) and not fab.fetched
    rows = {r[0]: r[1] for r in conn.execute("SELECT query, status FROM fab_matches")}
    assert rows == {"Tropical Island": "none", "LowPolyBarrel": "none"}  # well-formed answers: not asked again


def test_a_refused_request_stops_and_records_nothing_for_it(conn, tmp_path):
    fab = _Fab({"Neon City": [_listing("Neon City")]}, fail_on="Bunker")

    stats = _run(conn, tmp_path, ["Neon City", "Bunker", "Subway"], fab)

    assert stats.error and "429" in stats.error
    assert fab.queries == ["Neon City", "Bunker"]  # stopped: Subway waits for the next scan
    assert [r[0] for r in conn.execute("SELECT query FROM fab_matches")] == ["Neon City"]
    again = _run(conn, tmp_path, ["Neon City", "Bunker", "Subway"], _Fab({}))
    assert again.checked == 2  # Neon City is known; the other two are asked now


def test_cover_shows_for_ue_packs_without_a_picture_only(tmp_path):
    root = tmp_path / "lib"
    for pack in ("UE/Neon City - 霓虹城", "UE/Neon City Shots - 截图"):
        (root / pack / "Content").mkdir(parents=True)
        (root / pack / "Content" / "SM_Sign.uasset").write_bytes(b"x")
    Image.new("RGB", (8, 8)).save(root / "UE/Neon City Shots - 截图/shot.png")
    for pack in ("Models/Neon City - 模型", "Models/Sky - 天空"):  # "Models" groups packs: each its own
        (root / pack).mkdir(parents=True)
        (root / pack / "scene.blend").write_bytes(b"x")
    settings = Settings(roots={"art": root}, data_dir=tmp_path / "data")
    conn = connect(settings.db_path)
    scan_root(conn, "art", root)
    assert packs.pictureless_unreal(conn, "art") == ["UE/Neon City - 霓虹城"]

    _run(conn, settings.data_dir, ["Neon City"], _Fab({"Neon City": [_listing("Neon City")]}))
    covers = {p["path"]: p["links"].get("thumb") for p in packs.search_packs(conn, "").packs}
    conn.close()

    assert covers["art:/UE/Neon City - 霓虹城"] == f"/api/fab-covers/{UID}.webp"
    assert covers["art:/UE/Neon City Shots - 截图"].startswith("/api/files/")  # its own picture first
    assert covers["art:/Models/Neon City - 模型"] is None  # not Unreal: never matched by name alone
    with TestClient(create_app(settings)) as client:
        assert client.get(f"/api/fab-covers/{UID}.webp").headers["content-type"] == "image/webp"
        assert client.get("/api/fab-covers/..%2Findex.db").status_code == 404
        assert client.get("/api/fab-covers/status").json()["matched"] == 1


def test_answers_about_one_name_are_recorded_and_the_rest_still_asked(conn, tmp_path):
    """A picture Pillow cannot read, or a 404, says nothing of the other names."""
    broken = _Fab({"Neon City": [_listing("Neon City")], "Subway": [_listing("Subway", OTHER_UID)]})
    broken.fetch = lambda url: b"not an image"

    stats = _run(conn, tmp_path, ["Neon City", "Subway"], broken)

    assert (stats.checked, stats.matched, stats.error) == (2, 0, None)
    assert {r[0] for r in conn.execute("SELECT status FROM fab_matches")} == {"none"}


def test_listings_that_changed_shape_stop_and_record_nothing(conn, tmp_path):
    changed = _listing("Neon City")
    changed["thumbnails"] = "moved elsewhere"

    stats = _run(conn, tmp_path, ["Neon City"], _Fab({"Neon City": [changed]}))

    assert stats.error and stats.checked == 0
    assert conn.execute("SELECT COUNT(*) FROM fab_matches").fetchone()[0] == 0


def test_a_unity_edition_beside_the_unreal_one_does_not_hide_it(conn, tmp_path):
    fab = _Fab({"Neon City": [_listing("Neon City", OTHER_UID, formats=("unity",)), _listing("Neon City")]})

    assert _run(conn, tmp_path, ["Neon City"], fab).matched == 1
    assert fab.fetched == [f"https://media.fab.com/{UID}/big.jpg"]


def test_a_scan_spends_at_most_its_time_budget(conn, tmp_path):
    ticks = iter(range(0, 10_000, 100))  # each look at the clock: 100 s later
    stats = match_pending(conn, tmp_path, ["A", "B", "C"], find=_Fab({}).find, fetch=_Fab({}).fetch,
                          sleep=lambda _: None, clock=lambda: next(ticks))

    assert stats.checked == 1  # the second name would start past the 120 s budget


def test_cover_names_must_be_exactly_a_listing_id():
    assert fab_covers.is_cover_file(f"{UID}.webp")
    assert not fab_covers.is_cover_file(f"{UID}.webp\n") and not fab_covers.is_cover_file("../index.db")
