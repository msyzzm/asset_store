"""Pack tags (view, style, themes) asked of the vision model, kept apart from the organizer's
decisions, and shown with each pack in search."""
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from assetindex import packs, tags
from assetindex.archive_index import index_archives
from assetindex.config import Settings
from assetindex.db import connect
from assetindex.embedding import EmbedError
from assetindex.scanner import scan_root
from assetindex.web import create_app

CITY = "02_3D/Environments/SteamPunk City - 蒸汽朋克城市"
GEARS = "02_3D/Props/Gear Props - 齿轮道具.zip"
PAGE = {"X-Requested-With": "fetch"}


class FakeModel:
    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []

    def ask(self, prompt, jpeg, max_tokens=0):
        self.prompts.append(prompt)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "lib"
    (root / CITY / "Content").mkdir(parents=True)
    (root / CITY / "Content" / "City.umap").write_bytes(b"x")
    Image.new("RGB", (8, 8)).save(root / CITY / "preview.png")
    (root / GEARS).parent.mkdir(parents=True)
    with zipfile.ZipFile(root / GEARS, "w") as z:
        z.writestr("Props/gear.fbx", b"gear")
    settings = Settings(roots={"art": root}, data_dir=tmp_path / "data")
    conn = connect(settings.db_path)
    scan_root(conn, "art", root)
    index_archives(conn, "art", root)
    yield conn, settings
    conn.close()


def test_an_answer_becomes_tags():
    got = tags.parse_tags('好的：{"view": "不限", "style": "写实", "themes": "蒸汽朋克、维多利亚、城市"}')

    assert got == tags.Tags(view="不限", style="写实", themes=("蒸汽朋克", "维多利亚", "城市"))
    assert tags.parse_tags('{"view": "鸟瞰", "style": "油画", "themes": ""}') == tags.Tags("不限", "其他", ())
    with pytest.raises(ValueError):
        tags.parse_tags("没有 JSON")


def test_a_job_tags_the_packs_asked_for_and_search_shows_them(lib):
    conn, settings = lib
    model = FakeModel([
        '{"view": "不限", "style": "写实", "themes": "蒸汽朋克、城市"}',
        '{"view": "俯视", "style": "卡通", "themes": "齿轮"}',
    ])

    done = tags.run_tags(conn, settings, "art", [CITY, GEARS], model, "m")

    assert done.done == 2 and done.failed == 0 and done.error is None
    assert "SteamPunk City" in model.prompts[0] and "视角" in model.prompts[0]
    city = packs.search_packs(conn, "蒸汽朋克").packs[0]
    assert city["tags"] == {"view": "不限", "style": "写实", "themes": ["蒸汽朋克", "城市"]}
    gears = packs.search_packs(conn, "gear").packs[0]
    assert gears["tags"]["view"] == "俯视"


def test_a_bad_answer_fails_that_pack_and_an_api_error_stops_the_job(lib):
    conn, settings = lib

    one_bad = tags.run_tags(conn, settings, "art", [CITY, GEARS], FakeModel(["胡说", '{"view": "俯视", "style": "像素", "themes": ""}']), "m")
    assert one_bad.done == 1 and one_bad.failed == 1

    hiccup = FakeModel([EmbedError("超时", 503), '{"view": "俯视", "style": "像素", "themes": ""}'])
    retried = tags.run_tags(conn, settings, "art", [CITY], hiccup, "m", sleep=lambda s: None)
    assert retried.done == 1 and retried.error is None  # one API error is retried, not the end

    down = tags.run_tags(conn, settings, "art", [CITY, GEARS], FakeModel([EmbedError("连不上", 503)] * 4), "m",
                         patience=tags.Patience(retries=1, retry_wait=0, max_failures=2), sleep=lambda s: None)
    assert down.done == 0 and down.failed == 2 and "连不上" in down.error and "连不上" in down.last_error


def test_stopping_stops_and_packs_gone_are_failures(lib):
    conn, settings = lib

    stopped = tags.run_tags(conn, settings, "art", [CITY], FakeModel([]), "m", should_stop=lambda: True)
    gone = tags.run_tags(conn, settings, "art", ["02_3D/Props/nothing.zip"], FakeModel([]), "m")

    assert stopped.stopped and stopped.done == 0
    assert gone.failed == 1 and gone.done == 0


def test_a_folder_holding_packs_is_no_pack(lib):
    conn, settings = lib
    with conn:
        conn.execute("INSERT INTO organize_units (root, path, is_dir, files, size, signature) VALUES (?, ?, 1, 1, 1, 's')",
                     ("art", CITY))

    done = tags.run_tags(conn, settings, "art", ["02_3D/Environments"], FakeModel([]), "m")

    assert done.failed == 1 and "没有这个包" in done.last_error


def test_a_running_status_left_by_a_killed_captioner_says_so(lib):
    _, settings = lib
    tags.write_status(settings.data_dir, tags.TagStatus(running=True, total=20, done=7))

    status = tags.read_status(settings.data_dir)

    assert status["running"] is False and "中途断了" in status["error"] and status["done"] == 7


def test_the_captioner_refuses_a_bad_request_or_a_missing_model(lib):
    from assetindex import cli

    _, settings = lib
    cli._run_tags(settings, {"root": "nowhere", "paths": [CITY]})
    assert "格式" in tags.read_status(settings.data_dir)["error"]

    cli._run_tags(settings, {"root": "art", "paths": [CITY]})  # no vision model set up here
    assert "看图模型" in tags.read_status(settings.data_dir)["error"]

    tags.request_job(settings.data_dir, "art", [CITY])
    assert cli._tags_once(settings) and not tags.is_requested(settings.data_dir)


def test_the_page_asks_for_tags_and_sees_the_status(lib):
    _, settings = lib
    client = TestClient(create_app(settings))

    assert client.post("/api/organize/tags", json={"root": "art", "paths": [CITY]}, headers=PAGE).status_code == 202
    assert tags.take_job(settings.data_dir) == {"root": "art", "paths": [CITY]}
    assert client.post("/api/organize/tags", json={"root": "x", "paths": [CITY]}, headers=PAGE).status_code == 422
    assert client.post("/api/organize/tags", json={"root": "art", "paths": []}, headers=PAGE).status_code == 422
    assert client.get("/api/organize/tags").json()["running"] is False
