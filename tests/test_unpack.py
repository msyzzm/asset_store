import os

import py7zr
import pytest
from fastapi.testclient import TestClient

from assetindex import move_job, scan_request, unpack
from assetindex.archive_index import index_archives
from assetindex.config import Settings
from assetindex.db import connect
from assetindex.organize import refresh_units
from assetindex.scanner import scan_root
from assetindex.web import create_app

PASSWORD = "虚幻4资源站"
PAGE = {"X-Requested-With": "fetch"}


def locked_7z(path, files, password=PASSWORD):
    """A 7z whose names are encrypted too, as the ones on the NAS."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with py7zr.SevenZipFile(path, "w", password=password, header_encryption=password is not None) as z:
        for name, data in files.items():
            z.writestr(data, name)
    return path


def fake_move(src, dst, is_dir):
    os.replace(src, dst)


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "lib"
    locked_7z(root / "C" / "Tiger.7z", {"Tiger/Content/a.uasset": b"a" * 10, "Tiger/Content/b.uasset": b"bb"})
    (root / "C" / "Tiger.png").write_bytes(b"png")
    return root


def test_a_pack_with_one_top_folder_becomes_that_folder_with_its_preview(lib):
    new = unpack.unpack_one(lib, "C/Tiger.7z", PASSWORD, ["C/Tiger.png"], move=fake_move)

    assert new == "C/Tiger"
    assert (lib / "C/Tiger/Content/a.uasset").read_bytes() == b"a" * 10
    assert (lib / "C/Tiger/Tiger.png").exists() and not (lib / "C/Tiger.png").exists()
    assert (lib / "_待删除/Tiger.7z").exists() and not (lib / "C/Tiger.7z").exists()
    assert sorted(os.listdir(lib / "C")) == ["Tiger"]  # nothing half done left


def test_a_pack_of_loose_files_becomes_a_folder_named_after_it(tmp_path):
    root = tmp_path / "lib"
    locked_7z(root / "C" / "Loose.7z", {"a.txt": b"1", "b.txt": b"22"})

    unpack.unpack_one(root, "C/Loose.7z", PASSWORD, [], move=fake_move)

    assert sorted(os.listdir(root / "C/Loose")) == ["a.txt", "b.txt"]


def test_a_wrong_password_changes_nothing(lib):
    with pytest.raises(unpack.UnpackError, match="密码不对"):
        unpack.unpack_one(lib, "C/Tiger.7z", "wrong", ["C/Tiger.png"], move=fake_move)

    assert sorted(os.listdir(lib / "C")) == ["Tiger.7z", "Tiger.png"] and not (lib / "_待删除").exists()


def test_a_folder_already_there_is_never_written_into(lib):
    (lib / "C" / "tiger").mkdir()  # the share may ignore case

    with pytest.raises(unpack.UnpackError, match="已经有了"):
        unpack.unpack_one(lib, "C/Tiger.7z", PASSWORD, [], move=fake_move)

    assert os.listdir(lib / "C" / "tiger") == [] and (lib / "C/Tiger.7z").exists()


def test_what_a_crash_left_half_unpacked_is_cleared_first(lib):
    (lib / "C" / ".Tiger.unpacking" / "junk").mkdir(parents=True)

    unpack.unpack_one(lib, "C/Tiger.7z", PASSWORD, [], move=fake_move)

    assert not (lib / "C" / ".Tiger.unpacking").exists() and (lib / "C/Tiger/Content").is_dir()


def test_a_name_taken_in_the_set_aside_folder_is_numbered(lib):
    (lib / "_待删除").mkdir()
    (lib / "_待删除" / "Tiger.7z").write_bytes(b"older")

    unpack.unpack_one(lib, "C/Tiger.7z", PASSWORD, [], move=fake_move)

    assert (lib / "_待删除/Tiger 2.7z").exists() and (lib / "_待删除/Tiger.7z").read_bytes() == b"older"


def test_files_that_do_not_add_up_are_not_kept(lib, monkeypatch):
    real = unpack.extract_7z

    def short(src, out, password):
        expected = real(src, out, password)
        next(p for p in out.rglob("*") if p.is_file()).unlink()  # one went missing on the way
        return expected

    with pytest.raises(unpack.UnpackError, match="对不上"):
        unpack.unpack_one(lib, "C/Tiger.7z", PASSWORD, [], move=fake_move, extract=short)

    assert sorted(os.listdir(lib / "C")) == ["Tiger.7z", "Tiger.png"]


@pytest.fixture
def env(tmp_path, lib):
    locked_7z(lib / "C" / "Copy.7z", {"x.txt": b"x"})  # its zip copy is kept: already set aside
    locked_7z(lib / "_待删除" / "Old.7z", {"y.txt": b"y"})
    locked_7z(lib / "C" / "Open.7z", {"z.txt": b"z"}, password=None)
    locked_7z(lib / "Pack" / "inner.7z", {"w.txt": b"w"})
    settings = Settings(roots={"lib": lib}, data_dir=tmp_path / "data")
    conn = connect(settings.db_path)
    scan_root(conn, "lib", lib)
    index_archives(conn, "lib", lib)
    refresh_units(conn, "lib")
    with conn:
        conn.execute("UPDATE organize_units SET status = 'accepted', target = '_待删除/Copy.7z' WHERE path = 'C/Copy.7z'")
    yield conn, settings
    conn.close()


def test_only_locked_archives_not_set_aside_are_listed(env):
    conn, _ = env

    assert [a.path for a in unpack.locked_archives(conn, "lib")] == ["C/Tiger.7z", "Pack/inner.7z"]


def test_the_job_unpacks_asks_for_a_scan_and_stops_when_space_runs_out(env):
    conn, settings = env
    request = {"action": "unpack", "root": "lib", "password": PASSWORD}

    full = move_job.run(conn, settings, request, move=fake_move, free_bytes=lambda path: 0)
    assert full.moved == 0 and "空间" in full.error and (settings.roots["lib"] / "C/Tiger.7z").exists()

    done = move_job.run(conn, settings, request, move=fake_move)
    assert done.moved == 2 and done.total == 2 and done.error is None and done.action == "unpack"
    assert (settings.roots["lib"] / "C/Tiger/Tiger.png").exists() and (settings.roots["lib"] / "Pack/inner/w.txt").exists()
    assert scan_request.is_requested(settings.data_dir)


def test_a_wrong_password_is_a_problem_not_a_crash(env):
    conn, settings = env

    done = move_job.run(conn, settings, {"action": "unpack", "root": "lib", "password": "nope"}, move=fake_move)

    assert done.moved == 0 and done.problems[0]["path"] == "C/Tiger.7z" and "密码不对" in done.problems[0]["reason"]


def test_parse_wants_a_password_only_for_unpacking(env):
    _, settings = env
    assert move_job.parse({"action": "unpack", "root": "lib", "password": "p"}, settings) == ("unpack", "lib", None)
    for bad in ({"action": "unpack", "root": "lib"}, {"action": "unpack", "root": "lib", "password": ""},
                {"action": "unpack", "root": "lib", "password": 1}, {"action": "apply", "root": "lib", "password": "p"}):
        with pytest.raises(ValueError):
            move_job.parse(bad, settings)


def test_the_page_counts_the_locked_packs(env):
    _, settings = env
    client = TestClient(create_app(settings))

    locked = client.get("/api/organize/locked", params={"root": "lib"}).json()

    assert locked["count"] == 2 and locked["size"] > 0 and locked["sample"] == ["C/Tiger.7z", "Pack/inner.7z"]
    assert client.get("/api/organize/locked", params={"root": "x"}).status_code == 422


def test_a_run_cut_short_after_the_rename_is_finished_next_time(lib):
    def crash_on_files(src, dst, is_dir):
        if not is_dir:
            raise OSError("power gone")
        fake_move(src, dst, is_dir)

    with pytest.raises(OSError):
        unpack.unpack_one(lib, "C/Tiger.7z", PASSWORD, ["C/Tiger.png"], move=crash_on_files)
    assert (lib / "C/Tiger").is_dir() and (lib / "C/Tiger.7z").exists()

    assert unpack.unpack_one(lib, "C/Tiger.7z", PASSWORD, ["C/Tiger.png"], move=fake_move) == "C/Tiger"
    assert (lib / "C/Tiger/Tiger.png").exists() and (lib / "_待删除/Tiger.7z").exists()


def test_an_archive_already_gone_says_so(lib):
    (lib / "C/Tiger.7z").unlink()

    with pytest.raises(unpack.UnpackError, match="不在了"):
        unpack.unpack_one(lib, "C/Tiger.7z", PASSWORD, [], move=fake_move)


def test_half_unpacked_folders_are_never_indexed(lib, tmp_path):
    (lib / "C" / ".Tiger.unpacking" / "Tiger").mkdir(parents=True)
    (lib / "C" / ".Tiger.unpacking" / "Tiger" / "a.uasset").write_bytes(b"a")
    conn = connect(tmp_path / "x.db")

    scan_root(conn, "lib", lib)

    paths = [r[0] for r in conn.execute("SELECT rel_path FROM files")]
    assert paths and not any("unpacking" in p for p in paths)


def test_a_job_can_unpack_just_a_few_and_the_page_skips_archives_gone(env):
    conn, settings = env
    request = {"action": "unpack", "root": "lib", "password": PASSWORD, "limit": 1}

    done = move_job.run(conn, settings, request, move=fake_move)

    assert done.moved == 1 and done.total == 1
    assert [a.path for a in unpack.locked_archives(conn, "lib", settings.roots["lib"])] == ["Pack/inner.7z"]
    for bad in (0, -1, "2", True):
        with pytest.raises(ValueError):
            move_job.parse({**request, "limit": bad}, settings)
    with pytest.raises(ValueError):
        move_job.parse({"action": "apply", "root": "lib", "limit": 1}, settings)
