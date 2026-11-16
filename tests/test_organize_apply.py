import errno
import json
import os

import pytest

import sqlite3

from assetindex import organize_apply
from assetindex.organize import refresh_units
from assetindex.organize_apply import apply_plan, open_transactions, recover, undo, unsettled
from assetindex.organize_move import build_plan
from assetindex.scanner import scan_root


def linux_rename(src, dst, is_dir=False):
    """os.rename as on Linux: silently replaces an existing file (Windows refuses)."""
    os.replace(src, dst)


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

    return conn, root, put, tmp_path / "data" / "moves" / "run.jsonl"


def _accept(conn, path, target):
    with conn:
        conn.execute("UPDATE organize_units SET status = 'accepted', target = ? WHERE root = 'lib' AND path = ?", (target, path))


def _ids(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT rel_path, id FROM files")}


HAND_SORTED = ("UE/道具/Caves.part1.rar", "UE/道具/Caves.part2.rar", "UE/道具/Caves.png", "UE/道具/Sword.rar", "UE/道具/Shield.rar")


def test_apply_moves_a_pack_whole_keeps_ids_and_undo_puts_it_back(lib):
    conn, root, put, journal = lib
    put(*HAND_SORTED)
    with conn:
        conn.execute("INSERT INTO captions (file_id, model, mtime, text, created) SELECT id, 'm', mtime, '洞穴', 0 FROM files WHERE name = 'Caves.png'")
    before = _ids(conn)
    _accept(conn, "UE/道具/Caves.part1.rar", "02_3D/Environments/Luos 洞穴.rar")

    result = apply_plan(conn, root, "lib", build_plan(conn, "lib"), journal, move=linux_rename)

    assert result.moved == 1 and result.problems == ()
    new = "02_3D/Environments/Luos 洞穴"
    assert sorted(p.name for p in (root / "02_3D/Environments").iterdir()) == [
        "Luos 洞穴.part1.rar", "Luos 洞穴.part2.rar", "Luos 洞穴.png"]
    assert not (root / "UE/道具/Caves.png").exists()
    after = _ids(conn)
    assert after[f"{new}.png"] == before["UE/道具/Caves.png"]  # same file id: caption and preview stay
    assert conn.execute("SELECT text FROM captions WHERE file_id = ?", (after[f"{new}.png"],)).fetchone()[0] == "洞穴"
    row = conn.execute("SELECT status, target FROM organize_units WHERE path = ?", (f"{new}.part1.rar",)).fetchone()
    assert tuple(row) == ("accepted", f"{new}.part1.rar")
    assert build_plan(conn, "lib").moves == ()  # done: nothing left to move
    assert [json.loads(line)["op"] for line in journal.read_text(encoding="utf-8").splitlines()][:2] == ["start", "begin"]

    back = undo(conn, root, "lib", journal, move=linux_rename)

    assert back.moved == 1 and back.problems == ()
    assert (root / "UE/道具/Caves.png").read_bytes() == "UE/道具/Caves.png".encode()
    assert _ids(conn) == before
    row = conn.execute("SELECT status, target FROM organize_units WHERE path = 'UE/道具/Caves.part1.rar'").fetchone()
    assert tuple(row) == ("proposed", "02_3D/Environments/Luos 洞穴.rar")  # confirm again to move again
    assert undo(conn, root, "lib", journal, move=linux_rename).moved == 0  # nothing twice


def test_a_target_that_appeared_after_the_plan_is_never_overwritten(lib):
    conn, root, put, journal = lib
    put(*HAND_SORTED)
    _accept(conn, "UE/道具/Sword.rar", "02_3D/Props/Sword.rar")
    plan = build_plan(conn, "lib")
    (root / "02_3D/Props").mkdir(parents=True)
    (root / "02_3D/Props/SWORD.rar").write_bytes(b"someone else's")  # other case, not indexed yet

    result = apply_plan(conn, root, "lib", plan, journal, move=linux_rename)

    assert result.moved == 0 and "已经有了" in result.problems[0].reason
    assert (root / "02_3D/Props/SWORD.rar").read_bytes() == b"someone else's"
    assert (root / "UE/道具/Sword.rar").exists()


def test_a_pack_that_fails_halfway_is_turned_back_and_the_run_goes_on(lib):
    conn, root, put, journal = lib
    put(*HAND_SORTED)
    _accept(conn, "UE/道具/Caves.part1.rar", "02_3D/Environments/Caves.rar")
    _accept(conn, "UE/道具/Sword.rar", "02_3D/Props/Sword.rar")
    calls = []

    def flaky(src, dst, is_dir):
        calls.append(src)
        if src.endswith("Caves.part2.rar") and len(calls) == 2:
            raise OSError(errno.EIO, "I/O error", src)
        linux_rename(src, dst)

    result = apply_plan(conn, root, "lib", build_plan(conn, "lib"), journal, move=flaky)

    assert result.moved == 1 and [p.path for p in result.problems] == ["UE/道具/Caves.part1.rar"]
    assert (root / "UE/道具/Caves.part1.rar").exists() and (root / "UE/道具/Caves.part2.rar").exists()
    assert (root / "02_3D/Props/Sword.rar").exists()
    assert "UE/道具/Caves.part1.rar" in _ids(conn)


def test_a_read_only_or_cross_device_rename_stops_the_run(lib):
    conn, root, put, journal = lib
    put(*HAND_SORTED)
    _accept(conn, "UE/道具/Shield.rar", "02_3D/Props/Shield.rar")
    _accept(conn, "UE/道具/Sword.rar", "02_3D/Props/Sword.rar")

    def read_only(src, dst, is_dir):
        raise OSError(errno.EROFS, "Read-only file system", src)

    result = apply_plan(conn, root, "lib", build_plan(conn, "lib"), journal, move=read_only)

    assert result.moved == 0 and "已停止" in result.error
    assert (root / "UE/道具/Shield.rar").exists() and (root / "UE/道具/Sword.rar").exists()


def test_a_folder_pack_moves_with_everything_under_it(lib):
    conn, root, put, journal = lib
    put("Game Artbook/a/1.png", "Game Artbook/b/2.png")
    _accept(conn, "Game Artbook", "05_Reference/Artbook")

    result = apply_plan(conn, root, "lib", build_plan(conn, "lib"), journal, move=linux_rename)

    assert result.moved == 1
    assert {"05_Reference/Artbook/a/1.png", "05_Reference/Artbook/b/2.png"} <= set(_ids(conn))
    assert (root / "05_Reference/Artbook/b/2.png").exists()
    refresh_units(conn, "lib")  # the next analysis sees the same pack, still decided
    row = conn.execute("SELECT status FROM organize_units WHERE path = '05_Reference/Artbook'").fetchone()
    assert row[0] == "accepted"


def test_undo_leaves_a_pack_whose_old_place_is_taken(lib):
    conn, root, put, journal = lib
    put(*HAND_SORTED)
    _accept(conn, "UE/道具/Sword.rar", "02_3D/Props/Sword.rar")
    apply_plan(conn, root, "lib", build_plan(conn, "lib"), journal, move=linux_rename)
    (root / "UE/道具/Sword.rar").write_bytes(b"new download")

    back = undo(conn, root, "lib", journal, move=linux_rename)

    assert back.moved == 0 and len(back.problems) == 1
    assert (root / "UE/道具/Sword.rar").read_bytes() == b"new download"
    assert (root / "02_3D/Props/Sword.rar").exists()


def _ops(journal):
    return [json.loads(line)["op"] for line in journal.read_text(encoding="utf-8").splitlines()]


def test_an_index_that_refuses_puts_the_files_back_and_stops(lib, monkeypatch):
    conn, root, put, journal = lib
    put(*HAND_SORTED)
    _accept(conn, "UE/道具/Caves.part1.rar", "02_3D/Environments/Caves.rar")
    plan = build_plan(conn, "lib")
    before = _ids(conn)

    def locked(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(organize_apply, "rekey", locked)
    result = apply_plan(conn, root, "lib", plan, journal, move=linux_rename)

    assert result.moved == 0 and "索引没能更新" in result.error
    assert (root / "UE/道具/Caves.part2.rar").exists() and not (root / "02_3D/Environments/Caves.part1.rar").exists()
    assert _ids(conn) == before and "abort" in _ops(journal) and unsettled(journal.parent.parent) == []


def test_a_crash_after_the_renames_is_finished_by_recover(lib, monkeypatch):
    conn, root, put, journal = lib
    put(*HAND_SORTED)
    _accept(conn, "UE/道具/Sword.rar", "02_3D/Props/Sword.rar")
    plan = build_plan(conn, "lib")

    def power_cut(*args):
        raise KeyboardInterrupt  # not caught: as if the process died here

    monkeypatch.setattr(organize_apply, "rekey", power_cut)
    with pytest.raises(KeyboardInterrupt):
        apply_plan(conn, root, "lib", plan, journal, move=linux_rename)
    monkeypatch.undo()
    assert unsettled(journal.parent.parent) == [journal]  # the scanner must wait

    assert recover(conn, root, "lib", journal, move=None) == []  # all renames done: the index catches up
    assert "02_3D/Props/Sword.rar" in _ids(conn) and open_transactions(journal) == []


def test_a_crash_halfway_through_a_pack_is_turned_back_by_recover(lib):
    conn, root, put, journal = lib
    put(*HAND_SORTED)
    _accept(conn, "UE/道具/Caves.part1.rar", "02_3D/Environments/Caves.rar")
    plan = build_plan(conn, "lib")
    calls = []

    def dies(src, dst, is_dir):
        calls.append(src)
        if len(calls) == 2:
            raise KeyboardInterrupt
        linux_rename(src, dst)

    with pytest.raises(KeyboardInterrupt):
        apply_plan(conn, root, "lib", plan, journal, move=dies)

    left = recover(conn, root, "lib", journal, move=None)  # the scanner cannot move files
    assert len(left) == 1 and "中断" in left[0].reason
    assert recover(conn, root, "lib", journal, move=linux_rename) == []
    assert (root / "UE/道具/Caves.part1.rar").exists() and unsettled(journal.parent.parent) == []


def test_a_pack_that_cannot_be_turned_back_stops_the_run_and_stays_open(lib):
    conn, root, put, journal = lib
    put(*HAND_SORTED)
    _accept(conn, "UE/道具/Caves.part1.rar", "02_3D/Environments/Caves.rar")
    _accept(conn, "UE/道具/Sword.rar", "02_3D/Props/Sword.rar")
    calls = []

    def breaks(src, dst, is_dir):
        calls.append(src)
        if len(calls) >= 2:  # the second rename fails, and so does turning the first back
            raise OSError(errno.EIO, "I/O error", src)
        linux_rename(src, dst)

    result = apply_plan(conn, root, "lib", build_plan(conn, "lib"), journal, move=breaks)

    assert result.moved == 0 and "没能还原" in result.error
    assert (root / "UE/道具/Sword.rar").exists()  # the run stopped before the next pack
    assert "stuck" in _ops(journal) and unsettled(journal.parent.parent) == [journal]


def test_a_rename_reported_failed_that_did_happen_counts_as_done(lib):
    conn, root, put, journal = lib
    put(*HAND_SORTED)
    _accept(conn, "UE/道具/Sword.rar", "02_3D/Props/Sword.rar")

    def nfs_retry(src, dst, is_dir):
        linux_rename(src, dst)
        raise OSError(errno.EIO, "I/O error", src)  # the server did it, the client says no

    result = apply_plan(conn, root, "lib", build_plan(conn, "lib"), journal, move=nfs_retry)

    assert result.moved == 1 and "02_3D/Props/Sword.rar" in _ids(conn)


def test_packs_left_in_a_folder_stay_packs_of_their_own(lib):
    conn, root, put, journal = lib
    put(*HAND_SORTED)
    _accept(conn, "UE/道具/Caves.part1.rar", "02_3D/Environments/Caves.rar")
    apply_plan(conn, root, "lib", build_plan(conn, "lib"), journal, move=linux_rename)

    refresh_units(conn, "lib")  # two archives left: fewer than a "folder of packs" needs

    paths = {r[0] for r in conn.execute("SELECT path FROM organize_units")}
    assert {"UE/道具/Sword.rar", "UE/道具/Shield.rar"} <= paths and "UE/道具" not in paths
