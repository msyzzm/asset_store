import pytest

from assetindex.scanner import scan_root
from assetindex.search import MAX_TERMS, search


@pytest.fixture
def indexed(conn, asset_root):
    scan_root(conn, "art", asset_root)
    return conn


def _names(result):
    return sorted(item["name"] for item in result.items)


def test_two_char_chinese_term_matches(indexed):
    assert _names(search(indexed, "刀剑")) == ["刀剑特效.7z"]


def test_search_matches_directory_part_of_path(indexed):
    assert _names(search(indexed, "特效")) == ["刀剑特效.7z"]


def test_long_term_is_case_insensitive(indexed):
    assert _names(search(indexed, "BATTLE")) == ["Battle_Theme.mp3"]


def test_multiple_terms_are_anded(indexed):
    assert _names(search(indexed, "sword knight")) == ["knight_sword.fbx"]


def test_kind_filter(indexed):
    assert _names(search(indexed, "sword", kind="image")) == ["sword_icon.png"]


def test_empty_query_lists_everything_with_total_and_paging(indexed):
    result = search(indexed, "", limit=2, offset=0)
    assert result.total == 5
    assert len(result.items) == 2


def test_terms_beyond_cap_are_ignored(indexed):
    query = " ".join(["sword"] * MAX_TERMS + ["no-such-term"])
    assert search(indexed, query).total == 2


def test_paging_is_stable_when_same_path_exists_in_two_roots(indexed, asset_root):
    scan_root(indexed, "mirror", asset_root)
    first = search(indexed, "Battle", limit=1, offset=0).items[0]["id"]
    second = search(indexed, "Battle", limit=1, offset=1).items[0]["id"]
    assert first != second


def test_percent_sign_is_not_a_wildcard(indexed):
    assert search(indexed, "%").total == 5
    assert search(indexed, "zz%zz").total == 0


def test_short_terms_do_not_depend_on_the_trigram_index(indexed):
    """SQLite 3.40 (the server's) answers a LIKE of under three characters on a trigram
    index with no rows at all. Short terms must read the tables themselves."""
    file_id = indexed.execute("SELECT id FROM files WHERE name = 'knight_sword.fbx'").fetchone()[0]
    indexed.execute(
        "INSERT INTO captions (file_id, model, mtime, text, created) VALUES (?, 'm', 0, '金属齿轮组件', 0)", (file_id,)
    )
    for table in ("files_fts", "captions_fts", "entries_fts"):
        indexed.execute(f"DROP TABLE {table}")  # local SQLite scans instead; prove short terms never ask it

    assert _names(search(indexed, "刀剑")) == ["刀剑特效.7z"]
    assert _names(search(indexed, "齿轮")) == ["knight_sword.fbx"]
    assert _names(search(indexed, "齿轮 kn")) == ["knight_sword.fbx"]


def test_newest_first_lists_files_in_the_order_they_were_indexed(indexed, asset_root):
    (asset_root / "aaa_new.png").write_bytes(b"x")  # sorts first by path, indexed last
    scan_root(indexed, "art", asset_root)

    newest = search(indexed, "", sort="new")

    assert newest.items[0]["name"] == "aaa_new.png"
    ids = [item["id"] for item in newest.items]
    assert ids == sorted(ids, reverse=True)
