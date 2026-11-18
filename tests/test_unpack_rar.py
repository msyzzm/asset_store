"""Unpacking password-protected RAR with 7-Zip (7zz). There is no 7zz here: its runs are
faked, the listing parse is checked against 7zz's own -slt layout."""
import os
import subprocess

import pytest

from assetindex import move_job, unpack
from assetindex.config import Settings
from assetindex.db import connect

PASSWORD = "虚幻4资源站"
REAL_7ZZ = unpack.run_7zz
LISTING = """
7-Zip (z) 24.09 (x64)

Listing archive: /lib/C/Tiger.rar

--
Path = /lib/C/Tiger.rar
Type = Rar5
Physical Size = 120

----------
Path = Tiger
Folder = +
Size = 0

Path = Tiger/a.uasset
Folder = -
Size = 10
Encrypted = +

Path = Tiger/b.uasset
Folder = -
Size = 2
Encrypted = +
"""


def fake_move(src, dst, is_dir):
    os.replace(src, dst)


class Fake7zz:
    """Answers `l` with LISTING and `x` by writing the files, or fails like a wrong password."""

    def __init__(self, wrong=False):
        self.wrong, self.calls = wrong, []

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "l":
            return subprocess.CompletedProcess(args, 0, LISTING.encode(), b"")
        if self.wrong:
            return subprocess.CompletedProcess(args, 2, b"ERROR: Wrong password : Tiger/a.uasset", b"")
        out = next(a[2:] for a in args if a.startswith("-o"))
        os.makedirs(os.path.join(out, "Tiger"), exist_ok=True)
        with open(os.path.join(out, "Tiger", "a.uasset"), "wb") as f:
            f.write(b"a" * 10)
        with open(os.path.join(out, "Tiger", "b.uasset"), "wb") as f:
            f.write(b"bb")
        return subprocess.CompletedProcess(args, 0, b"Everything is Ok", b"")


@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = tmp_path / "lib"
    (root / "C").mkdir(parents=True)
    (root / "C" / "Tiger.rar").write_bytes(b"rar" * 40)
    (root / "C" / "Tiger.png").write_bytes(b"png")
    tool = Fake7zz()
    monkeypatch.setattr(unpack, "run_7zz", tool)
    return root, tool


def test_the_listing_is_read_from_7zz_technical_output():
    assert unpack.parse_listing(LISTING) == (2, 12)


def test_a_7z_listing_marks_folders_only_by_their_attributes():
    seven = (
        "Path = /lib/C/Tiger.7z\nType = 7z\n\n----------\n"
        "Path = Tiger\nSize = 0\nAttributes = D_ drwxr-xr-x\n\n"
        "Path = Tiger/Maps\nSize = 0\nAttributes = D\n\n"
        "Path = Tiger/a.uasset\nSize = 10\nAttributes = A_ -rw-r--r--\n\n"
        "Path = Tiger/empty.txt\nSize = 0\nAttributes = A\n"
    )

    assert unpack.parse_listing(seven) == (2, 10)  # two files, an empty one too; two folders


def test_a_rar_becomes_its_folder_and_the_password_goes_to_7zz_only(lib):
    root, tool = lib

    assert unpack.unpack_one(root, "C/Tiger.rar", PASSWORD, ["C/Tiger.png"], move=fake_move) == "C/Tiger"

    assert (root / "C/Tiger/a.uasset").read_bytes() == b"a" * 10
    assert (root / "C/Tiger/Tiger.png").exists() and (root / "_待删除/Tiger.rar").exists()
    assert sorted(os.listdir(root / "C")) == ["Tiger"]
    assert all(f"-p{PASSWORD}" in call for call in tool.calls)


def test_a_wrong_password_keeps_the_rar_and_never_shows_the_password(lib, monkeypatch):
    root, _ = lib
    monkeypatch.setattr(unpack, "run_7zz", Fake7zz(wrong=True))

    with pytest.raises(unpack.UnpackError) as err:
        unpack.unpack_one(root, "C/Tiger.rar", PASSWORD, [], move=fake_move)

    assert "Wrong password" in str(err.value) and PASSWORD not in str(err.value)
    assert sorted(os.listdir(root / "C")) == ["Tiger.png", "Tiger.rar"]  # the hidden folder is gone too


def test_a_rar_in_volumes_is_refused(lib):
    root, tool = lib
    (root / "C" / "Big.part1.rar").write_bytes(b"x")

    with pytest.raises(unpack.UnpackError, match="分卷"):
        unpack.unpack_one(root, "C/Big.part1.rar", PASSWORD, [], move=fake_move)
    assert tool.calls == []


def test_without_7zz_nothing_happens(lib, monkeypatch):
    root, _ = lib
    monkeypatch.setattr(unpack, "run_7zz", REAL_7ZZ)
    monkeypatch.setattr(unpack.verify, "sevenzip", lambda: None)

    with pytest.raises(unpack.UnpackError, match="7zz"):
        unpack.unpack_one(root, "C/Tiger.rar", PASSWORD, [], move=fake_move)
    assert (root / "C/Tiger.rar").exists()


def test_an_old_style_volume_set_is_refused_by_its_listing(lib, monkeypatch):
    root, _ = lib
    listing = LISTING.replace("Type = Rar5", "Type = Rar5\nMultivolume = +\nVolumes = 3")
    monkeypatch.setattr(unpack, "run_7zz", lambda args: subprocess.CompletedProcess(args, 0, listing.encode(), b""))

    with pytest.raises(unpack.UnpackError, match="分卷"):
        unpack.unpack_one(root, "C/Tiger.rar", PASSWORD, [], move=fake_move)


def test_a_listing_with_no_files_is_never_taken_for_an_empty_pack(lib, monkeypatch):
    root, _ = lib
    monkeypatch.setattr(unpack, "run_7zz", lambda args: subprocess.CompletedProcess(args, 0, b"odd output", b""))

    with pytest.raises(unpack.UnpackError, match="没列出"):
        unpack.unpack_one(root, "C/Tiger.rar", PASSWORD, [], move=fake_move)
    assert (root / "C/Tiger.rar").exists()


def test_a_job_unpacks_just_the_paths_asked_for(lib, tmp_path):
    root, _ = lib
    settings = Settings(roots={"lib": root}, data_dir=tmp_path / "data")
    conn = connect(settings.db_path)
    request = {"action": "unpack", "root": "lib", "password": PASSWORD, "paths": ["C/Tiger.rar", "C/Gone.rar"]}

    assert move_job.parse(request, settings) == ("unpack", "lib", None)
    done = move_job.run(conn, settings, request, move=fake_move)

    assert done.moved == 1 and done.total == 2 and (root / "C/Tiger/b.uasset").exists()
    assert done.problems[0]["path"] == "C/Gone.rar" and "不在了" in done.problems[0]["reason"]
    with pytest.raises(ValueError):
        move_job.parse({"action": "apply", "root": "lib", "paths": ["C/Tiger.rar"]}, settings)
    conn.close()


def test_7z_and_zip_go_through_7zz_when_it_is_there(lib, monkeypatch):
    root, tool = lib
    monkeypatch.setattr(unpack.verify, "sevenzip", lambda: "7zz")
    for name in ("Tiger.7z", "Lion.zip", "x.tar"):
        (root / "C" / name).write_bytes(b"pack")

    assert unpack.unpack_one(root, "C/Tiger.7z", PASSWORD, [], move=fake_move) == "C/Tiger"
    assert unpack.unpack_one(root, "C/Lion.zip", PASSWORD, [], move=fake_move) == "C/Lion"
    assert {call[-1].replace("\\", "/").rpartition("/")[2] for call in tool.calls} >= {"Tiger.7z", "Lion.zip"}
    with pytest.raises(unpack.UnpackError, match="只解"):
        unpack.unpack_one(root, "C/x.tar", PASSWORD, [], move=fake_move)
