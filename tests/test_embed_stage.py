import os

import numpy as np
import pytest
from PIL import Image

from assetindex.embed_stage import card_text, embed_pending
from assetindex.embedding import EmbedConfig
from assetindex.scanner import scan_root
from tests.fake_embed import FakeEmbedServer


@pytest.fixture
def server():
    with FakeEmbedServer() as fake:
        yield fake


@pytest.fixture
def scanned(conn, asset_root):
    scan_root(conn, "art", asset_root)
    return conn


def _config(server, **kw) -> EmbedConfig:
    return EmbedConfig(enabled=True, base_url=server.base_url, model="m1", **kw)


def _rows(conn, source=None):
    sql = "SELECT f.rel_path, e.source, e.model, e.vector, e.error FROM embeddings e JOIN files f ON f.id = e.file_id"
    rows = conn.execute(sql + (" WHERE e.source = ?" if source else ""), (source,) if source else ()).fetchall()
    return {(r["rel_path"], r["source"]): r for r in rows}


def test_stop_ends_embedding_between_batches(scanned, settings, server):
    from assetindex.scan_request import ScanStopped

    flags = iter([False] + [True] * 100)  # one batch, then stop

    with pytest.raises(ScanStopped):
        embed_pending(scanned, settings, _config(server, batch_size=1), should_stop=lambda: next(flags))

    assert len(_rows(scanned)) == 1  # the finished batch is kept


def test_embedding_reports_progress(scanned, settings, server):
    seen = []

    embed_pending(scanned, settings, _config(server, batch_size=2),
                  progress=lambda phase, done, total: seen.append((phase, done, total)))

    assert seen[-1] == ("embed_text", 5, 5) and seen[0] == ("embed_text", 0, 5)


def test_every_card_gets_a_text_vector_once(scanned, settings, server):
    stats = embed_pending(scanned, settings, _config(server))

    rows = _rows(scanned, "text")
    assert stats.texts == len(rows) == 5 and stats.error is None
    vec = np.frombuffer(rows[("Audio/Battle_Theme.mp3", "text")]["vector"], dtype=np.float32)
    assert abs(np.linalg.norm(vec) - 1) < 1e-5  # stored unit length
    assert vec[3] > 0.9  # "battle"/"theme" -> music concept

    again = embed_pending(scanned, settings, _config(server))
    assert again.texts == 0 and len(server.requests) == 1


def test_images_are_embedded_when_a_format_is_set(scanned, settings, server):
    stats = embed_pending(scanned, settings, _config(server, image_format="input_object"))

    images = _rows(scanned, "image")
    assert stats.images == 1 and list(images) == [("2D/Icons/sword_icon.png", "image")]
    vec = np.frombuffer(images[("2D/Icons/sword_icon.png", "image")]["vector"], dtype=np.float32)
    assert vec[1] > 0.9  # the icon is red


def test_changed_file_or_model_is_embedded_again(scanned, settings, server, asset_root):
    embed_pending(scanned, settings, _config(server))
    target = asset_root / "readme.txt"
    os.utime(target, (1_000_000, 1_000_000))
    scan_root(scanned, "art", asset_root)

    assert embed_pending(scanned, settings, _config(server)).texts == 1

    other = EmbedConfig(enabled=True, base_url=server.base_url, model="m2")
    assert embed_pending(scanned, settings, other).texts == 5
    assert {r["model"] for r in _rows(scanned).values()} == {"m2"}


def test_server_down_stops_the_stage_without_failing(scanned, settings, server):
    server.fail_status = 503

    stats = embed_pending(scanned, settings, _config(server))

    assert stats.error and "503" in stats.error
    assert _rows(scanned) == {}
    assert len(server.requests) == 1  # gave up instead of hammering a dead server


def test_rejected_inputs_are_recorded_and_not_retried(scanned, settings, server):
    server.reject_images = True

    stats = embed_pending(scanned, settings, _config(server, image_format="input_object"))

    assert stats.rejected == 1 and stats.error is None
    row = _rows(scanned, "image")[("2D/Icons/sword_icon.png", "image")]
    assert row["vector"] is None and "images not supported" in row["error"]
    calls = len(server.requests)
    embed_pending(scanned, settings, _config(server, image_format="input_object"))
    assert len(server.requests) == calls

    stats = embed_pending(scanned, settings, _config(server, image_format="input_object"), retry_errors=True)
    assert stats.rejected == 1  # tried again on request


def test_inactive_settings_do_nothing(scanned, settings, server):
    stats = embed_pending(scanned, settings, EmbedConfig(base_url=server.base_url, model="m"))
    assert stats.texts == 0 and server.requests == []


def test_deleted_file_drops_its_vectors(scanned, settings, server, asset_root):
    embed_pending(scanned, settings, _config(server))
    (asset_root / "readme.txt").unlink()
    scan_root(scanned, "art", asset_root)
    assert ("readme.txt", "text") not in _rows(scanned)


def test_only_the_head_of_an_image_sequence_is_embedded(conn, settings, server, asset_root):
    for n in range(1, 7):
        Image.new("RGB", (8, 8), (0, 0, 255)).save(asset_root / f"walk_{n:02d}.png")
    scan_root(conn, "art", asset_root)

    embed_pending(conn, settings, _config(server, image_format="input_object"))

    walk = [k for k in _rows(conn) if k[0].startswith("walk_")]
    assert len(walk) == 2  # one card: text + image


def test_archive_text_includes_names_inside(scanned):
    file_id = scanned.execute("SELECT id FROM files WHERE ext = '7z'").fetchone()[0]
    with scanned:
        scanned.executemany(
            "INSERT INTO archive_entries (file_id, path, ext, size) VALUES (?, ?, ?, 1)",
            [(file_id, "Effects/Fire/blast_01.png", "png"), (file_id, "Effects/Fire/blast_02.png", "png")],
        )
    row = scanned.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()

    text = card_text(scanned, row)

    assert "刀剑特效" in text and "Fire" in text and "blast" in text
    assert text.count("Fire") == 1  # names are deduplicated



def test_one_bad_card_does_not_block_the_rest(scanned, settings, server, monkeypatch):
    from assetindex import embed_stage

    real = embed_stage.card_text

    def flaky(conn, row):
        if row["rel_path"] == "readme.txt":
            raise OverflowError("odd file")
        return real(conn, row)

    monkeypatch.setattr(embed_stage, "card_text", flaky)

    stats = embed_pending(scanned, settings, _config(server))

    assert stats.texts == 4 and stats.rejected == 1 and stats.error is None
    assert "odd file" in _rows(scanned, "text")[("readme.txt", "text")]["error"]


def test_wrong_settings_do_not_mark_everything_rejected(conn, settings, server, asset_root):
    # Every image refused = the image format is wrong, not every picture: stop, record nothing.
    for name in ("a", "b", "c"):
        Image.new("RGB", (8, 8), (0, 0, 255)).save(asset_root / f"{name}.png")
    scan_root(conn, "art", asset_root)
    server.reject_images = True

    stats = embed_pending(conn, settings, _config(server, image_format="input_object"))

    assert stats.error and "images not supported" in stats.error
    assert _rows(conn, "image") == {}


def test_only_the_scanned_root_is_embedded(conn, settings, server, asset_root, tmp_path):
    from dataclasses import replace

    other = tmp_path / "other"
    other.mkdir()
    (other / "fire.txt").write_text("x")
    scan_root(conn, "art", asset_root)
    scan_root(conn, "other", other)
    both = replace(settings, roots={"art": asset_root, "other": other})

    embed_pending(conn, replace(both, roots={"other": other}), _config(server))

    assert [k[0] for k in _rows(conn)] == ["fire.txt"]
