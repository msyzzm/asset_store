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
