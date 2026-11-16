from assetindex.units import Unit, compute_units, unit_of

FILES = [
    ("loose.zip", 10),
    ("2D/hero.png", 1),
    ("2D/Knight Pack/sprites/idle.png", 2),
    ("2D/Knight Pack/readme.txt", 3),
    ("2D/Mega 合集/Swords/a.png", 4),
    ("2D/Mega 合集/Swords/b.png", 5),
    ("2D/Mega 合集/bonus.zip", 6),
    ("itch/角色系列/hero.rar", 7),
    ("Game Artbook/chars/a.png", 8),
    ("Game Artbook/bg/b.png", 9),
]


def _units(splits=()):
    return {u.path: u for u in compute_units(FILES, splits)}


def test_default_rule_keeps_packs_together():
    units = _units()

    assert set(units) == {
        "loose.zip",
        "2D/hero.png",
        "2D/Knight Pack",
        "2D/Mega 合集/Swords",
        "2D/Mega 合集/bonus.zip",
        "itch/角色系列/hero.rar",
        "Game Artbook",
    }
    assert units["2D/Knight Pack"] == Unit("2D/Knight Pack", True, 2, 5)
    assert units["Game Artbook"].files == 2  # not a category name: the whole folder is one pack
    assert not units["2D/hero.png"].is_dir


def test_every_file_belongs_to_exactly_one_unit():
    units = compute_units(FILES)
    assert sum(u.files for u in units) == len(FILES)
    dirs = [u.path for u in units if u.is_dir]
    assert not any(a != b and b.startswith(a + "/") for a in dirs for b in dirs)  # no nesting


def test_user_split_opens_a_unit():
    units = _units(splits=["Game Artbook"])
    assert {"Game Artbook/chars", "Game Artbook/bg"} <= set(units) and "Game Artbook" not in units


def test_collection_hint_is_set_on_folders_that_look_like_several_packs():
    # Inside a container a collection is opened anyway; elsewhere it is one unit, flagged.
    units = compute_units([("2D/Assorted Bundle/x/a.png", 1), ("Big Collection 2/x.png", 1)])
    hints = {u.path: u.collection_hint for u in units}
    assert hints == {"2D/Assorted Bundle/x": False, "Big Collection 2": True}


def test_unit_of_a_file_directly_in_the_root():
    assert unit_of("x.png", frozenset()) == ("x.png", False)
