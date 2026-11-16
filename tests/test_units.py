from assetindex.units import Unit, compute_units, layout, pair_companions

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
    assert layout(["x.png"]).unit_of("x.png") == ("x.png", False)


HAND_SORTED = [
    ("UE/道具/Sword Pack.rar", 50), ("UE/道具/Sword Pack.png", 1),
    ("UE/道具/Hong_Kong_Street.rar", 60), ("UE/道具/Hong Kong Street.png", 1),
    ("UE/道具/Megapack 4.26.zip", 70), ("UE/道具/Megapack.jpg", 1),
    ("UE/道具/Caves.part1.rar", 10), ("UE/道具/Caves.part2.rar", 10), ("UE/道具/Caves (1).png", 1),
    ("Mixed/Barrels.zip", 5), ("Mixed/Crates.zip", 5), ("Mixed/Lamps/Lamps.7z", 5), ("Mixed/notes.txt", 1),
]


def test_a_folder_of_several_packs_is_opened_and_previews_stay_with_their_archive():
    units = {u.path: u for u in compute_units(HAND_SORTED)}

    assert units["UE/道具/Sword Pack.rar"] == Unit("UE/道具/Sword Pack.rar", False, 2, 51)
    assert units["UE/道具/Hong_Kong_Street.rar"].files == 2  # separators and case don't matter
    assert units["UE/道具/Megapack 4.26.zip"].files == 2  # the preview's name leaves out the version
    assert units["UE/道具/Caves.part1.rar"] == Unit("UE/道具/Caves.part1.rar", False, 3, 21)  # volumes + preview
    # Not a category name, but it holds three packs (one of them a folder).
    assert {"Mixed/Barrels.zip", "Mixed/Crates.zip", "Mixed/Lamps", "Mixed/notes.txt"} <= set(units)
    assert sum(u.files for u in units.values()) == len(HAND_SORTED)


def test_variants_of_one_pack_stay_together():
    files = [
        ("3D/LakeCabin/LakeCabin_Source_V1.zip", 1), ("3D/LakeCabin/LakeCabin_Unity_V1.zip", 1),
        ("3D/LakeCabin/LakeCabin_Unreal_V1.zip", 1), ("3D/LakeCabin/Read me.rar", 1),
        ("3D/Camera/4.27/CloseupCamera.7z", 1), ("3D/Camera/5.0/CloseupCamera.7z", 1), ("3D/Camera/5.1/CloseupCamera.7z", 1),
        ("3D/Two/a.zip", 1), ("3D/Two/b.zip", 1),
    ]
    assert {u.path for u in compute_units(files)} == {"3D/LakeCabin", "3D/Camera", "3D/Two"}


def test_a_folder_holding_an_opened_folder_is_opened_too():
    files = [("Stuff/Inbox/a.rar", 1), ("Stuff/Inbox/bb.rar", 1), ("Stuff/Inbox/ccc.rar", 1), ("Stuff/Game/x.png", 1)]
    assert {u.path for u in compute_units(files)} == {
        "Stuff/Inbox/a.rar", "Stuff/Inbox/bb.rar", "Stuff/Inbox/ccc.rar", "Stuff/Game",
    }


def test_a_joined_folder_is_one_pack_unless_split():
    joined = {u.path for u in compute_units(HAND_SORTED, joins=["UE/道具"])}
    assert "UE/道具" in joined and not any(p.startswith("UE/道具/") for p in joined)

    split = {u.path for u in compute_units(HAND_SORTED, splits=["UE/道具"], joins=["UE/道具"])}
    assert "UE/道具/Sword Pack.rar" in split


def test_pairing_rules():
    pairs = pair_companions(["A.rar", "A.r00", "A.png", "Big Pack Deluxe.zip", "Big Pack.jpg", "Ab.png", "x.txt"])
    # The .rar leads its volumes; a short name pairs only exactly; unrelated files stay alone.
    assert pairs == {"A.r00": "A.rar", "A.png": "A.rar", "Big Pack.jpg": "Big Pack Deluxe.zip"}


def test_only_previews_and_notes_pair_by_prefix_and_the_closest_name_wins():
    pairs = pair_companions(["Tree.zip", "Tree_Oak.fbx", "Warroom.rar", "Warroom Environment 2.rar", "Warroom Environment.png"])
    assert pairs == {"Warroom Environment.png": "Warroom Environment 2.rar"}  # the .fbx is an asset of its own


def test_joining_a_deep_folder_keeps_its_parent_open():
    files = [("Stuff/Inbox/Deep/a.rar", 1), ("Stuff/Inbox/Deep/bb.rar", 1), ("Stuff/Inbox/Deep/ccc.rar", 1)]
    assert {u.path for u in compute_units(files, joins=["Stuff/Inbox/Deep"])} == {"Stuff/Inbox/Deep"}


def test_a_folder_of_previews_goes_with_the_archives_above_it():
    files = [
        ("Packs/(1)_Icons.zip", 10), ("Packs/(2)_Icons.zip", 10), ("Packs/Swords.zip", 10),
        ("Packs/JPEG/(1)_Icons.jpg", 1), ("Packs/JPEG/(2)_Icons.jpg", 1), ("Packs/JPEG/Swords.jpg", 1),
        ("Packs/Mixed/(1)_Icons.jpg", 1), ("Packs/Mixed/hero.png", 1),  # not all previews: a pack of its own
    ]
    units = {u.path: u for u in compute_units(files)}
    assert units["Packs/(1)_Icons.zip"].files == 2 and units["Packs/Swords.zip"].files == 2
    assert "Packs/JPEG" not in units and units["Packs/Mixed"].files == 2
