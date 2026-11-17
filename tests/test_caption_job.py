import json
from dataclasses import replace

import pytest
from PIL import Image

from assetindex import caption_job
from assetindex.caption import CaptionConfig
from assetindex.caption_job import (
    Scope,
    list_folders,
    parse_scope,
    read_status,
    request_job,
    request_stop,
    run_job,
    scope_counts,
    take_job,
)
from assetindex.embed_stage import card_text, embed_pending
from assetindex.embedding import EmbedConfig
from assetindex.scanner import scan_root
from assetindex.search import search
from tests.fake_embed import FakeEmbedServer


@pytest.fixture
def server():
    with FakeEmbedServer() as fake:
        yield fake


@pytest.fixture
def scanned(conn, asset_root):
    for name, color in (("ice.png", (0, 0, 255)), ("frost.png", (0, 80, 255))):
        path = asset_root / "2D" / "Ice" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (16, 16), color).save(path)
    scan_root(conn, "art", asset_root)
    return conn


def _config(server) -> CaptionConfig:
    return CaptionConfig(base_url=server.base_url, model="qwen")


def _captions(conn) -> dict:
    rows = conn.execute("SELECT f.rel_path, c.text, c.error FROM captions c JOIN files f ON f.id = c.file_id")
    return {r["rel_path"]: (r["text"], r["error"]) for r in rows}


def test_scope_is_validated_and_normalised():
    roots = {"art": None}
    assert parse_scope({"root": "art", "prefix": "\\2D\\Icons/ "}, roots) == Scope("art", "2D/Icons", False)
    assert parse_scope({"root": "art", "redo": True}, roots) == Scope("art", "", True)
    # Pasted as the cards show it ("art:/2D/x.zip"): the root part is dropped.
    assert parse_scope({"root": "art", "prefix": "art:/2D/x.zip"}, roots) == Scope("art", "2D/x.zip", False)
    for bad in ({"root": "nope"}, {"root": "art", "prefix": "a\x00b"}, {"root": "art", "redo": "yes"}, [], {"root": "art", "x": 1}):
        with pytest.raises(ValueError):
            parse_scope(bad, roots)


def test_counts_cover_only_cards_with_a_picture_in_the_folder(scanned, settings):
    assert scope_counts(scanned, Scope("art")) == {"cards": 3, "done": 0, "todo": 3}
    assert scope_counts(scanned, Scope("art", "2D/Ice")) == {"cards": 2, "done": 0, "todo": 2}
    assert scope_counts(scanned, Scope("art", "2D/Ic")) == {"cards": 0, "done": 0, "todo": 0}  # folders, not prefixes
    assert scope_counts(scanned, Scope("art", "2D/Ice/ice.png"))["cards"] == 1  # a single file works too


def test_job_describes_pictures_and_feeds_both_searches(scanned, settings, server):
    embed = EmbedConfig(enabled=True, base_url=server.base_url, model="m1")
    embed_pending(scanned, settings, embed)  # text vectors exist before the captions do
    statuses = []

    final = run_job(scanned, settings, Scope("art", "2D"), _config(server), report=statuses.append)

    assert final.done == 3 and final.failed == 0 and not final.running and final.error is None
    assert statuses[0].running and statuses[0].total == 3
    caps = _captions(scanned)
    assert caps["2D/Icons/sword_icon.png"] == ("一团红色的火焰特效\n火焰、红色、特效", None)
    assert caps["2D/Ice/ice.png"][0].startswith("一块蓝色的冰晶")
    # Keyword search finds pictures by what they show, not only by name.
    assert [i["rel_path"] for i in search(scanned, "火焰").items] == ["2D/Icons/sword_icon.png"]
    assert search(scanned, "火焰").items[0]["caption"].startswith("一团红色")
    # The text vector now includes the caption, so the old one was dropped for re-embedding.
    row = scanned.execute("SELECT * FROM files WHERE rel_path = '2D/Icons/sword_icon.png'").fetchone()
    assert "火焰" in card_text(scanned, row)
    assert embed_pending(scanned, settings, embed).texts == 3


def test_second_run_skips_done_cards_unless_redo(scanned, settings, server):
    run_job(scanned, settings, Scope("art"), _config(server))
    calls = len(server.requests)

    assert run_job(scanned, settings, Scope("art"), _config(server)).total == 0
    assert len(server.requests) == calls
    assert scope_counts(scanned, Scope("art")) == {"cards": 3, "done": 3, "todo": 0}
    assert run_job(scanned, settings, Scope("art", redo=True), _config(server)).done == 3


def test_stop_request_ends_the_job_between_pictures(scanned, settings, server):
    asked = iter([False, True])

    final = run_job(scanned, settings, Scope("art"), _config(server), should_stop=lambda: next(asked))

    assert final.stopped and final.done == 1 and len(_captions(scanned)) == 1


def test_server_down_stops_without_recording_anything(scanned, settings, server):
    server.fail_status = 503

    final = run_job(scanned, settings, Scope("art"), _config(server))

    assert final.error and "503" in final.error and final.done == 0
    assert _captions(scanned) == {} and len(server.requests) == 1


def test_rejected_pictures_are_recorded_and_retried_next_run(scanned, settings, server):
    server.fail_status = 400

    final = run_job(scanned, settings, Scope("art"), replace(_config(server), max_failures=2))

    assert final.failed == 2 and "连续" in final.error  # the settings are probably wrong: stop
    assert all(err for _, err in _captions(scanned).values())
    server.fail_status = None
    assert run_job(scanned, settings, Scope("art"), _config(server)).done == 3


def test_unreadable_picture_is_skipped(scanned, settings, server, asset_root):
    (asset_root / "2D" / "Ice" / "frost.png").write_bytes(b"not a png")

    final = run_job(scanned, settings, Scope("art", "2D/Ice"), _config(server))

    assert final.done == 1 and final.failed == 1


def test_deleted_file_takes_its_caption_out_of_search(scanned, settings, server, asset_root):
    run_job(scanned, settings, Scope("art"), _config(server))
    (asset_root / "2D" / "Icons" / "sword_icon.png").unlink()
    scan_root(scanned, "art", asset_root)

    assert search(scanned, "火焰").total == 0
    assert "2D/Icons/sword_icon.png" not in _captions(scanned)


def test_folders_lists_children_of_a_folder(scanned):
    assert list_folders(scanned, "art", "") == ["2D", "3D", "Audio", "特效"]  # tool/ held only skipped files
    assert list_folders(scanned, "art", "2D") == ["2D/Ice", "2D/Icons"]
    assert list_folders(scanned, "art", "2D/Ice") == []


def test_request_stop_and_status_files(tmp_path):
    request_job(tmp_path, Scope("art", "2D", True))
    (tmp_path / caption_job.STOP_NAME).touch()  # left over from an earlier job

    assert take_job(tmp_path) == {"root": "art", "prefix": "2D", "redo": True}
    assert take_job(tmp_path) is None
    assert not caption_job.stop_requested(tmp_path)  # a new job starts with a clean slate

    request_job(tmp_path, Scope("art"))
    request_stop(tmp_path)  # stopping also drops a job that hasn't started yet
    assert caption_job.stop_requested(tmp_path) and take_job(tmp_path) is None

    (tmp_path / caption_job.STATUS_NAME).write_text(json.dumps({"running": "x"}), encoding="utf-8")
    assert read_status(tmp_path).running is False


def test_failed_redo_keeps_the_existing_description(scanned, settings, server):
    run_job(scanned, settings, Scope("art"), _config(server))
    server.fail_status = 400

    run_job(scanned, settings, Scope("art", redo=True), _config(server))

    assert search(scanned, "火焰").total == 1  # still findable by its old description
    assert scope_counts(scanned, Scope("art"))["done"] == 3


def test_vector_computed_before_a_new_caption_is_not_kept(scanned, settings, server, monkeypatch):
    # The scanner embeds a card's old text while the captioner saves its description.
    from assetindex import embed_stage
    from assetindex.embedding import EmbedClient

    embed = EmbedConfig(enabled=True, base_url=server.base_url, model="m1")
    real = EmbedClient.embed_texts

    def caption_meanwhile(self, texts):
        vectors = real(self, texts)
        if not _captions(scanned):
            run_job(scanned, settings, Scope("art", "2D/Icons"), _config(server))
        return vectors

    monkeypatch.setattr(EmbedClient, "embed_texts", caption_meanwhile)
    embed_stage.embed_pending(scanned, settings, embed)
    monkeypatch.setattr(EmbedClient, "embed_texts", real)

    # The stale vector was dropped, so the next round embeds the text with the caption.
    assert embed_stage.embed_pending(scanned, settings, embed).texts == 1
