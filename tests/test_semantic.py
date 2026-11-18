import pytest
from PIL import Image

from assetindex.embed_stage import embed_pending
from assetindex.embedding import EmbedClient, EmbedConfig
from assetindex.scanner import scan_root
from assetindex.semantic import VectorIndex, ranked_ids, semantic_search
from tests.fake_embed import FakeEmbedServer


@pytest.fixture
def server():
    with FakeEmbedServer() as fake:
        yield fake


@pytest.fixture
def config(server):
    return EmbedConfig(enabled=True, base_url=server.base_url, model="m1", image_format="input_object")


@pytest.fixture
def indexed(conn, settings, asset_root, config):
    scan_root(conn, "art", asset_root)
    embed_pending(conn, settings, config)
    return conn


def _search(conn, config, query, **kw):
    return semantic_search(conn, VectorIndex(), EmbedClient(config), config.model, query, **kw)


def _paths(result):
    return [item["rel_path"] for item in result.items]


def test_finds_by_meaning_across_languages(indexed, config):
    result = _search(indexed, config, "剑")  # two of these are only named "sword"

    assert set(_paths(result)[:3]) == {
        "2D/Icons/sword_icon.png", "3D/Knight/knight_sword.fbx", "特效/刀剑特效.7z",
    }


def test_finds_images_by_what_they_show(conn, settings, asset_root, config):
    # sword_icon.png is the only red picture; nothing about fire in any name.
    for name in ("sky", "sea", "leaf", "ice", "stone"):
        Image.new("RGB", (8, 8), (40, 90, 200)).save(asset_root / f"{name}.png")
    scan_root(conn, "art", asset_root)
    embed_pending(conn, settings, config)

    assert _paths(_search(conn, config, "火焰"))[0] == "2D/Icons/sword_icon.png"


def test_ranked_ids_can_hand_over_more_cards_than_a_page(indexed, config):
    vector = EmbedClient(config).embed_texts(["剑"])[0]

    few = ranked_ids(indexed, VectorIndex(), config.model, vector, limit=2)
    many = ranked_ids(indexed, VectorIndex(), config.model, vector, limit=1000)

    assert len(few) == 2 and len(many) == 5 and many[:2] == few


def test_kind_filter_and_paging(indexed, config):
    result = _search(indexed, config, "剑", kind="model")
    assert _paths(result) == ["3D/Knight/knight_sword.fbx"]

    first = _search(indexed, config, "剑", limit=2)
    second = _search(indexed, config, "剑", limit=2, offset=2)
    assert first.total == second.total == 5
    assert not set(_paths(first)) & set(_paths(second))


def test_items_have_the_same_shape_as_text_search(indexed, config):
    item = _search(indexed, config, "music").items[0]
    assert item["rel_path"] == "Audio/Battle_Theme.mp3"
    assert {"id", "name", "ext", "kind", "size", "frames", "hits", "entry_count"} <= set(item)


def test_index_reloads_after_new_vectors(conn, settings, asset_root, config):
    from assetindex.semantic import NoVectors

    scan_root(conn, "art", asset_root)
    index = VectorIndex()
    client = EmbedClient(config)
    with pytest.raises(NoVectors):
        semantic_search(conn, index, client, "m1", "剑")

    embed_pending(conn, settings, config)

    assert semantic_search(conn, index, client, "m1", "剑").total == 5


def test_reloads_are_throttled_while_vectors_keep_arriving(indexed, settings, asset_root, config):
    now = [1000.0]
    index = VectorIndex(clock=lambda: now[0])
    client = EmbedClient(config)
    before = semantic_search(indexed, index, client, "m1", "剑").total

    (asset_root / "blade.txt").write_text("x")
    scan_root(indexed, "art", asset_root)
    embed_pending(indexed, settings, config)
    assert semantic_search(indexed, index, client, "m1", "剑").total == before  # within 30 s

    now[0] += 31
    assert semantic_search(indexed, index, client, "m1", "剑").total == before + 1


def test_vectors_of_another_model_are_ignored(indexed, server):
    from assetindex.semantic import NoVectors

    other = EmbedConfig(enabled=True, base_url=server.base_url, model="m2")
    with pytest.raises(NoVectors):
        _search(indexed, other, "剑")



def test_no_vectors_yet_is_reported_not_an_empty_result(conn, settings, asset_root, config):
    from assetindex.semantic import NoVectors

    scan_root(conn, "art", asset_root)
    with pytest.raises(NoVectors):
        semantic_search(conn, VectorIndex(), EmbedClient(config), "m1", "剑")


def test_frames_hidden_after_embedding_never_show_up(conn, settings, asset_root, config):
    index = VectorIndex()
    Image.new("RGB", (8, 8), (200, 0, 0)).save(asset_root / "flame_01.png")
    scan_root(conn, "art", asset_root)
    embed_pending(conn, settings, config)
    client = EmbedClient(config)
    assert "flame_01.png" in _paths(semantic_search(conn, index, client, "m1", "火焰"))

    # More frames arrive: flame_01 becomes the head, others hidden; nothing re-embedded yet.
    for n in range(2, 7):
        Image.new("RGB", (8, 8), (200, 0, 0)).save(asset_root / f"flame_{n:02d}.png")
    scan_root(conn, "art", asset_root)
    with conn:
        conn.execute("UPDATE files SET seq_frames = 0 WHERE name = 'flame_01.png'")  # now hidden

    assert "flame_01.png" not in _paths(semantic_search(conn, index, client, "m1", "火焰"))
