import pytest

from assetindex.caption_job import Scope
from assetindex.organize import refresh_units, to_classify
from assetindex.organize_move import Rename, build_plan
from assetindex.scanner import scan_root


@pytest.fixture
def lib(tmp_path, conn):
    root = tmp_path / "lib"

    def put(*paths):
        for rel in paths:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(rel.encode())
        scan_root(conn, "lib", root)
        refresh_units(conn, "lib")

    return conn, put


def _accept(conn, path, target):
    with conn:
        changed = conn.execute(
            "UPDATE organize_units SET status = 'accepted', target = ? WHERE root = 'lib' AND path = ?", (target, path)
        ).rowcount
    assert changed == 1, path


HAND_SORTED = (
    "UE/道具/Caves.part1.rar", "UE/道具/Caves.part2.rar", "UE/道具/Caves.png",
    "UE/道具/Sword.rar", "UE/道具/Shield.rar",
)


def test_a_file_pack_moves_with_its_volumes_and_preview_renamed_to_pair(lib):
    conn, put = lib
    put(*HAND_SORTED)
    _accept(conn, "UE/道具/Caves.part1.rar", "02_3D/Environments/Luos 洞穴.rar")

    plan = build_plan(conn, "lib")

    assert plan.problems == ()
    (move,) = plan.moves
    assert move.renames == (
        Rename("UE/道具/Caves.part1.rar", "02_3D/Environments/Luos 洞穴.part1.rar", False),
        Rename("UE/道具/Caves.part2.rar", "02_3D/Environments/Luos 洞穴.part2.rar", False),
        Rename("UE/道具/Caves.png", "02_3D/Environments/Luos 洞穴.png", False),
    )


def test_a_folder_pack_is_one_rename_and_stays_one_pack_in_a_sparse_category(lib):
    conn, put = lib
    put("Game Artbook/a.zip", "Game Artbook/bb.zip", "Game Artbook/ccc.zip")
    with conn:  # kept as one pack, as the user would
        conn.execute("INSERT INTO unit_joins (root, path) VALUES ('lib', 'Game Artbook')")
    refresh_units(conn, "lib")
    _accept(conn, "Game Artbook", "05_Reference/Artbook")

    plan = build_plan(conn, "lib")

    assert plan.problems == ()
    assert [m.renames for m in plan.moves] == [(Rename("Game Artbook", "05_Reference/Artbook", True),)]


def test_targets_that_exist_or_are_wanted_twice_are_problems(lib):
    conn, put = lib
    put(*HAND_SORTED, "UE/道具/Bow.rar", "05_Reference/Old.rar")
    _accept(conn, "UE/道具/Sword.rar", "05_Reference/old.rar")  # exists, other case
    _accept(conn, "UE/道具/Shield.rar", "02_3D/Props/Gear.rar")
    _accept(conn, "UE/道具/Bow.rar", "02_3D/Props/Gear.rar")
    _accept(conn, "UE/道具/Caves.part1.rar", "02_3D/Props/Gear 2.rar")

    plan = build_plan(conn, "lib")

    reasons = {p.path: p.reason for p in plan.problems}
    assert "已经存在" in reasons["UE/道具/Sword.rar"]
    assert "也是别的包的目标" in reasons["UE/道具/Shield.rar"] and "也是别的包的目标" in reasons["UE/道具/Bow.rar"]
    assert [m.path for m in plan.moves] == ["UE/道具/Caves.part1.rar"]


def test_volumes_of_different_packs_landing_under_one_name_are_caught(lib):
    conn, put = lib
    put(*HAND_SORTED)
    _accept(conn, "UE/道具/Shield.rar", "02_3D/Props/Gear.rar")
    _accept(conn, "UE/道具/Caves.part1.rar", "02_3D/Props/Gear.rar")  # becomes Gear.part1.rar: same stem

    plan = build_plan(conn, "lib")

    assert len(plan.moves) == 1 and len(plan.problems) == 1


def test_a_move_that_would_merge_two_packs_is_left_out(lib):
    conn, put = lib
    put(*HAND_SORTED, "UE/道具/Oak.png")
    _accept(conn, "UE/道具/Sword.rar", "02_3D/Props/Tree.rar")
    _accept(conn, "UE/道具/Oak.png", "02_3D/Props/Tree.png")  # would become Tree.rar's preview

    plan = build_plan(conn, "lib")

    assert [m.path for m in plan.moves] == ["UE/道具/Sword.rar"]
    assert [p.path for p in plan.problems] == ["UE/道具/Oak.png"]


def test_downloads_are_packs_of_their_own_to_be_sorted_and_moved_out(lib):
    conn, put = lib
    put("_下载/Stone Walls.zip", "_下载/Props.zip", "02_3D/Props/Barrel.zip")

    todo = {r["path"] for r in to_classify(conn, Scope(root="lib"))}
    assert {"_下载/Stone Walls.zip", "_下载/Props.zip"} <= todo  # each one, not "_下载" as one pack
    _accept(conn, "_下载/Stone Walls.zip", "02_3D/Environments/Stone Walls.zip")
    plan = build_plan(conn, "lib")

    assert plan.problems == ()
    assert [(m.path, m.new_path) for m in plan.moves] == [("_下载/Stone Walls.zip", "02_3D/Environments/Stone Walls.zip")]


def test_nothing_accepted_nothing_planned(lib):
    conn, put = lib
    put(*HAND_SORTED)
    assert build_plan(conn, "lib").moves == ()


def test_targets_inside_moving_folders_or_each_other_are_problems(lib):
    conn, put = lib
    put(*HAND_SORTED, "Game Artbook/a/1.png", "Game Artbook/b/2.png")
    _accept(conn, "Game Artbook", "05_Reference/Artbook")
    _accept(conn, "UE/道具/Sword.rar", "Game Artbook/Sword.rar")  # into a folder that is leaving
    _accept(conn, "UE/道具/Shield.rar", "05_Reference/Artbook/Shield.rar")  # into another target

    plan = build_plan(conn, "lib")

    reasons = {p.path: p.reason for p in plan.problems}
    assert "另一个要移动的文件夹" in reasons["UE/道具/Sword.rar"]
    assert "包含关系" in reasons["UE/道具/Shield.rar"]


def test_a_stale_decision_is_not_moved(lib):
    conn, put = lib
    put(*HAND_SORTED)
    with conn:  # a row for a pack the layout no longer has
        conn.execute("INSERT INTO organize_units (root, path, is_dir, files, size, signature, status, target, updated)"
                     " VALUES ('lib', 'UE/道具/Gone.rar', 0, 1, 1, 'x', 'accepted', '02_3D/Props/Gone.rar', 0)")

    plan = build_plan(conn, "lib")

    assert plan.moves == () and "重新分析" in plan.problems[0].reason
