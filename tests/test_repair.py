"""Repairing a pack from copies each damaged in a different member."""
import os
import subprocess
import zipfile

import pytest

from assetindex import move_job, repair, scan_request, unpack
from assetindex.config import Settings
from assetindex.db import connect

MEMBERS = {"Steam/Textures/a.uasset": b"A" * 4000, "Steam/Textures/b.uasset": b"B" * 3000, "Steam/readme.txt": b"hi"}


def fake_move(src, dst, is_dir):
    os.replace(src, dst)


def make_zip(path, bad=None):
    """A stored zip of MEMBERS; `bad` gets one byte flipped in its data (its CRC no longer fits)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as z:
        for name, data in MEMBERS.items():
            z.writestr(name, data)
    if bad:
        raw = bytearray(path.read_bytes())
        at = raw.find(MEMBERS[bad]) + 10
        raw[at] ^= 0xFF
        path.write_bytes(bytes(raw))
    return path


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "lib"
    make_zip(root / "E" / "Steam - 场景.zip", bad="Steam/Textures/a.uasset")
    make_zip(root / "E" / "Steam - 城市场景.zip", bad="Steam/Textures/b.uasset")
    (root / "E" / "Steam - 场景.jpg").write_bytes(b"jpg")
    (root / "E" / "Steam - 城市场景.jpg").write_bytes(b"jpg2")
    return root


PATHS = ["E/Steam - 场景.zip", "E/Steam - 城市场景.zip"]
COMPANIONS = {"E/Steam - 场景.zip": ["E/Steam - 场景.jpg"], "E/Steam - 城市场景.zip": ["E/Steam - 城市场景.jpg"]}


def test_each_member_comes_from_a_copy_that_reads_it_whole(lib):
    new = repair.repair_one(lib, PATHS, COMPANIONS, move=fake_move)

    assert new == "E/Steam - 场景"
    folder = lib / new
    for name, data in MEMBERS.items():
        assert (folder / name.removeprefix("Steam/")).read_bytes() == data  # the one top folder is the pack
    assert (folder / "Steam - 场景.jpg").exists()
    assert sorted(os.listdir(lib / "_待删除")) == sorted(["Steam - 城市场景.jpg", "Steam - 城市场景.zip", "Steam - 场景.zip"])
    assert sorted(os.listdir(lib / "E")) == ["Steam - 场景"]


def test_a_member_damaged_in_every_copy_changes_nothing(lib):
    make_zip(lib / "E" / "Steam - 城市场景.zip", bad="Steam/Textures/a.uasset")

    with pytest.raises(repair.RepairError, match="a.uasset"):
        repair.repair_one(lib, PATHS, COMPANIONS, move=fake_move)

    assert sorted(os.listdir(lib / "E")) == ["Steam - 场景.jpg", "Steam - 场景.zip", "Steam - 城市场景.jpg", "Steam - 城市场景.zip"]


def test_copies_that_list_different_files_are_refused(lib):
    with zipfile.ZipFile(lib / "E" / "Steam - 城市场景.zip", "a") as z:
        z.writestr("Steam/extra.txt", b"x")

    with pytest.raises(repair.RepairError, match="不一样"):
        repair.repair_one(lib, PATHS, COMPANIONS, move=fake_move)


def test_unsafe_member_names_are_refused(tmp_path):
    root = tmp_path / "lib"
    (root / "E").mkdir(parents=True)
    for name in ("a.zip", "b.zip"):
        with zipfile.ZipFile(root / "E" / name, "w") as z:
            z.writestr("../evil.txt", b"x")

    with pytest.raises(repair.RepairError, match="路径"):
        repair.repair_one(root, ["E/a.zip", "E/b.zip"], {}, move=fake_move)
    assert not (root / "evil.txt").exists() and not (root / "E/evil.txt").exists()


def test_a_member_zipfile_cannot_decode_is_read_with_7zz(lib, monkeypatch):
    def unsupported(zf, info, out):
        raise NotImplementedError("deflate64")

    def by_7zz(src, info, out):
        out.write(MEMBERS[info.filename])
        return True

    monkeypatch.setattr(repair, "_with_zipfile", unsupported)
    monkeypatch.setattr(repair, "_with_7zz", by_7zz)

    new = repair.repair_one(lib, PATHS, COMPANIONS, move=fake_move)

    assert (lib / new / "Textures/a.uasset").read_bytes() == MEMBERS["Steam/Textures/a.uasset"]


def test_the_job_repairs_the_paths_and_asks_for_a_scan(lib, tmp_path):
    settings = Settings(roots={"lib": lib}, data_dir=tmp_path / "data")
    conn = connect(settings.db_path)
    request = {"action": "repair", "root": "lib", "paths": PATHS}

    assert move_job.parse(request, settings) == ("repair", "lib", None)
    done = move_job.run(conn, settings, request, move=fake_move)

    assert done.moved == 1 and done.error is None and done.total == 3 and done.done == 3
    assert (lib / "E/Steam - 场景/Textures/b.uasset").exists() and scan_request.is_requested(settings.data_dir)
    for bad in ({"action": "repair", "root": "lib"}, {"action": "repair", "root": "lib", "paths": PATHS[:1]}):
        with pytest.raises(ValueError):
            move_job.parse(bad, settings)
    conn.close()


def test_a_copy_whose_directory_cannot_be_read_is_named(lib):
    (lib / "E" / "Steam - 城市场景.zip").write_bytes(b"not a zip")

    with pytest.raises(repair.RepairError, match="城市场景.zip"):
        repair.repair_one(lib, PATHS, COMPANIONS, move=fake_move)


def test_a_failure_after_the_folder_is_in_place_is_half_done_and_still_scanned(lib, tmp_path):
    def no_files(src, dst, is_dir):
        if not is_dir:
            raise OSError("busy")
        fake_move(src, dst, is_dir)

    settings = Settings(roots={"lib": lib}, data_dir=tmp_path / "data")
    conn = connect(settings.db_path)

    done = move_job.run(conn, settings, {"action": "repair", "root": "lib", "paths": PATHS}, move=no_files)

    assert done.moved == 1 and "已经修好" in done.error and (lib / "E/Steam - 场景/readme.txt").exists()
    assert scan_request.is_requested(settings.data_dir)
    conn.close()


def test_a_failed_job_says_why(lib, tmp_path):
    make_zip(lib / "E" / "Steam - 城市场景.zip", bad="Steam/Textures/a.uasset")
    settings = Settings(roots={"lib": lib}, data_dir=tmp_path / "data")
    conn = connect(settings.db_path)

    done = move_job.run(conn, settings, {"action": "repair", "root": "lib", "paths": PATHS}, move=fake_move)

    assert done.moved == 0 and "每一份都坏了" in done.error
    conn.close()


# Locked RAR: 7zz writes every member, a bad one too, and names the bad ones.
PASSWORD = "虚幻4资源站"
RAR_MEMBERS = {"Kyoto/a.uasset": b"a" * 10, "Kyoto/b.uasset": b"bb"}
RAR_LISTING = "\n----------\n" + "\n\n".join(
    f"Path = {name}\nFolder = -\nSize = {len(data)}" for name, data in RAR_MEMBERS.items()) + "\n"


class FakeRar7zz:
    """`bad`: archive name -> the member it holds damaged."""

    def __init__(self, bad):
        self.bad = bad

    def __call__(self, args):
        src = next(a for a in args if a.endswith(".rar"))
        name = src.replace("\\", "/").rpartition("/")[2]
        if args[0] == "l":
            return subprocess.CompletedProcess(args, 0, RAR_LISTING.encode(), b"")
        out = next(a[2:] for a in args if a.startswith("-o"))
        wanted = args[args.index(src) + 1 :] or list(RAR_MEMBERS)
        broken = [m for m in wanted if m == self.bad.get(name)]
        for member in wanted:
            target = os.path.join(out, member)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(b"?" * len(RAR_MEMBERS[member]) if member in broken else RAR_MEMBERS[member])
        if not broken:
            return subprocess.CompletedProcess(args, 0, b"Everything is Ok", b"")
        said = "".join(f"ERROR: CRC Failed : {m}\n" for m in broken) + f"Sub items Errors: {len(broken)}\n"
        return subprocess.CompletedProcess(args, 2, said.encode(), b"")


@pytest.fixture
def rars(tmp_path, monkeypatch):
    root = tmp_path / "lib"
    for name in ("Kyoto - 京都.rar", "Kyoto - 日式.rar"):
        (root / "E").mkdir(parents=True, exist_ok=True)
        (root / "E" / name).write_bytes(b"rar")
    monkeypatch.setattr(unpack, "run_7zz", FakeRar7zz({"Kyoto - 京都.rar": "Kyoto/a.uasset", "Kyoto - 日式.rar": "Kyoto/b.uasset"}))
    return root


RARS = ["E/Kyoto - 京都.rar", "E/Kyoto - 日式.rar"]


def test_a_locked_rar_is_rebuilt_from_its_copies(rars):
    new = repair.repair_one(rars, RARS, {}, move=fake_move, password=PASSWORD)

    assert new == "E/Kyoto - 京都"
    assert (rars / new / "a.uasset").read_bytes() == b"a" * 10 and (rars / new / "b.uasset").read_bytes() == b"bb"
    assert sorted(os.listdir(rars / "E")) == ["Kyoto - 京都"]
    assert sorted(os.listdir(rars / "_待删除")) == sorted(["Kyoto - 京都.rar", "Kyoto - 日式.rar"])


def test_a_rar_member_bad_in_every_copy_changes_nothing(rars, monkeypatch):
    monkeypatch.setattr(unpack, "run_7zz", FakeRar7zz({"Kyoto - 京都.rar": "Kyoto/a.uasset", "Kyoto - 日式.rar": "Kyoto/a.uasset"}))

    with pytest.raises(repair.RepairError, match="每一份都坏了"):
        repair.repair_one(rars, RARS, {}, move=fake_move, password=PASSWORD)

    assert sorted(os.listdir(rars / "E")) == sorted(["Kyoto - 京都.rar", "Kyoto - 日式.rar"])


def test_errors_7zz_does_not_name_are_not_guessed_at(rars, monkeypatch):
    def vague(args):
        if args[0] == "l":
            return subprocess.CompletedProcess(args, 0, RAR_LISTING.encode(), b"")
        return subprocess.CompletedProcess(args, 2, b"ERROR: Headers Error\nSub items Errors: 3", b"")

    monkeypatch.setattr(unpack, "run_7zz", vague)

    with pytest.raises(repair.RepairError, match="解不开"):
        repair.repair_one(rars, RARS, {}, move=fake_move, password=PASSWORD)
    assert sorted(os.listdir(rars / "E")) == sorted(["Kyoto - 京都.rar", "Kyoto - 日式.rar"])


def test_7zz_failing_midway_leaves_nothing_behind(rars, monkeypatch):
    fake = FakeRar7zz({"Kyoto - 京都.rar": "Kyoto/a.uasset", "Kyoto - 日式.rar": "Kyoto/b.uasset"})

    def dies_on_the_second_copy(args):
        if args[0] == "x" and any(a.endswith("日式.rar") for a in args):
            raise unpack.UnpackError("7zz 跑了太久，没解完")
        return fake(args)

    monkeypatch.setattr(unpack, "run_7zz", dies_on_the_second_copy)

    with pytest.raises(repair.RepairError, match="太久"):
        repair.repair_one(rars, RARS, {}, move=fake_move, password=PASSWORD)
    assert sorted(os.listdir(rars / "E")) == sorted(["Kyoto - 京都.rar", "Kyoto - 日式.rar"])


def test_every_member_failing_means_a_wrong_password(rars, monkeypatch):
    def all_bad(args):
        if args[0] == "l":
            return subprocess.CompletedProcess(args, 0, RAR_LISTING.encode(), b"")
        said = "".join(f"ERROR: CRC Failed in encrypted file. Wrong password? : {m}\n" for m in RAR_MEMBERS)
        return subprocess.CompletedProcess(args, 2, (said + "Sub items Errors: 2\n").encode(), b"")

    monkeypatch.setattr(unpack, "run_7zz", all_bad)

    with pytest.raises(repair.RepairError, match="密码"):
        repair.repair_one(rars, RARS, {}, move=fake_move, password="nope")
    with pytest.raises(repair.RepairError, match="密码"):
        repair.repair_one(rars, RARS, {}, move=fake_move)


def test_a_rar_repair_job_takes_the_password(rars, tmp_path):
    settings = Settings(roots={"lib": rars}, data_dir=tmp_path / "data")
    conn = connect(settings.db_path)
    request = {"action": "repair", "root": "lib", "paths": RARS, "password": PASSWORD}

    assert move_job.parse(request, settings) == ("repair", "lib", None)
    done = move_job.run(conn, settings, request, move=fake_move)

    assert done.moved == 1 and done.error is None and (rars / "E/Kyoto - 京都/b.uasset").read_bytes() == b"bb"
    conn.close()
