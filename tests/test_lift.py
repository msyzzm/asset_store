"""Taking the one archive out of an archive that only wraps it (plus ad links, a picture)."""
import os
import subprocess

import pytest

from assetindex import lift, move_job, unpack
from assetindex.config import Settings
from assetindex.db import connect


def fake_move(src, dst, is_dir):
    os.replace(src, dst)


class Fake7zz:
    """7zz over made-up archives: name -> {member: bytes}."""

    def __init__(self, archives):
        self.archives = archives

    def __call__(self, args):
        src = next(a for a in args if a.lower().endswith((".zip", ".7z", ".rar")))
        files = self.archives[src.replace("\\", "/").rpartition("/")[2]]
        if args[0] == "l":
            blocks = "\n\n".join(f"Path = {p}\nSize = {len(d)}\nAttributes = A" for p, d in files.items())
            return subprocess.CompletedProcess(args, 0, f"\n----------\n{blocks}\n".encode(), b"")
        out = next(a[2:] for a in args if a.startswith("-o"))
        for member, data in files.items():
            os.makedirs(os.path.dirname(os.path.join(out, member)), exist_ok=True)
            with open(os.path.join(out, member), "wb") as f:
                f.write(data)
        return subprocess.CompletedProcess(args, 0, b"Everything is Ok", b"")


OUTER = "C/Fight - 格斗.7z"
INNER = b"PK" + b"z" * 100


@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = tmp_path / "lib"
    (root / "C").mkdir(parents=True)
    (root / OUTER).write_bytes(b"7z outer")
    monkeypatch.setattr(unpack.verify, "sevenzip", lambda: "7zz")
    monkeypatch.setattr(unpack, "run_7zz", Fake7zz({"Fight - 格斗.7z": {
        "Fight/Fight.zip": INNER, "More.url": b"ad", "Unity3d快捷搜索.url": b"ad", "Fight/cover.png": b"png",
    }}))
    return root


def test_the_inner_archive_takes_the_outer_name_and_the_outer_goes_aside(lib):
    new = lift.lift_one(lib, OUTER, None, [], move=fake_move)

    assert new == "C/Fight - 格斗.zip"
    assert (lib / new).read_bytes() == INNER
    assert (lib / "C/Fight - 格斗.png").read_bytes() == b"png"  # its picture is the cover
    assert sorted(os.listdir(lib / "C")) == ["Fight - 格斗.png", "Fight - 格斗.zip"]  # no ad links, no scratch
    assert os.listdir(lib / "_待删除") == ["Fight - 格斗.7z"]


def test_a_cover_already_beside_it_is_kept(lib):
    (lib / "C/Fight - 格斗.jpg").write_bytes(b"jpg")

    lift.lift_one(lib, OUTER, None, ["C/Fight - 格斗.jpg"], move=fake_move)

    assert sorted(os.listdir(lib / "C")) == ["Fight - 格斗.jpg", "Fight - 格斗.zip"]


def test_same_name_and_kind_inside_still_works(lib, monkeypatch):
    (lib / "C/Brush.zip").write_bytes(b"outer zip")
    monkeypatch.setattr(unpack, "run_7zz", Fake7zz({"Brush.zip": {"Brush/Brush.zip": INNER}}))

    assert lift.lift_one(lib, "C/Brush.zip", None, [], move=fake_move) == "C/Brush.zip"
    assert (lib / "C/Brush.zip").read_bytes() == INNER and (lib / "_待删除/Brush.zip").read_bytes() == b"outer zip"


@pytest.mark.parametrize("members", [
    {"a.zip": INNER, "b.zip": INNER},
    {"a.zip": INNER, "model.fbx": b"fbx"},
    {"model.fbx": b"fbx"},
])
def test_anything_more_than_a_wrapper_is_left_alone(lib, monkeypatch, members):
    monkeypatch.setattr(unpack, "run_7zz", Fake7zz({"Fight - 格斗.7z": members}))

    with pytest.raises(lift.LiftError, match="不是只套了一个压缩包"):
        lift.lift_one(lib, OUTER, None, [], move=fake_move)
    assert os.listdir(lib / "C") == ["Fight - 格斗.7z"] and not (lib / "_待删除").exists()


def test_a_name_taken_by_another_file_is_refused(lib):
    (lib / "C/Fight - 格斗.zip").write_bytes(b"other")

    with pytest.raises(lift.LiftError, match="已经有了"):
        lift.lift_one(lib, OUTER, None, [], move=fake_move)
    assert (lib / "C/Fight - 格斗.zip").read_bytes() == b"other" and (lib / OUTER).exists()


def test_a_failed_extraction_changes_nothing(lib, monkeypatch):
    def broken(args):
        if args[0] == "l":
            return Fake7zz({"Fight - 格斗.7z": {"Fight/Fight.zip": INNER}})(args)
        return subprocess.CompletedProcess(args, 2, b"", b"ERROR: Data Error")

    monkeypatch.setattr(unpack, "run_7zz", broken)

    with pytest.raises(lift.LiftError):
        lift.lift_one(lib, OUTER, None, [], move=fake_move)
    assert os.listdir(lib / "C") == ["Fight - 格斗.7z"]


def test_an_upper_case_suffix_is_the_same_name(lib, monkeypatch):
    (lib / "C/Brush.ZIP").write_bytes(b"outer zip")
    monkeypatch.setattr(unpack, "run_7zz", Fake7zz({"Brush.ZIP": {"Brush/Brush.zip": INNER}}))

    assert lift.lift_one(lib, "C/Brush.ZIP", None, [], move=fake_move) == "C/Brush.ZIP"
    assert (lib / "C/Brush.ZIP").read_bytes() == INNER


def failing_on(call):
    """A move that fails on its `call`-th use."""
    calls = []

    def move(src, dst, is_dir):
        calls.append(dst)
        if len(calls) == call:
            raise OSError("NAS gone")
        os.replace(src, dst)
    return move


def test_a_failed_move_of_the_inner_archive_leaves_the_outer_in_place(lib):
    with pytest.raises(lift.LiftError) as err:
        lift.lift_one(lib, OUTER, None, [], move=failing_on(1))

    assert not isinstance(err.value, lift.LiftHalfDone)
    assert os.listdir(lib / "C") == ["Fight - 格斗.7z"]  # nothing changed, no scratch left


def test_a_failed_move_of_the_outer_says_the_inner_is_in(lib):
    with pytest.raises(lift.LiftHalfDone, match="外层没能移进待删除"):
        lift.lift_one(lib, OUTER, None, [], move=failing_on(2))

    assert sorted(os.listdir(lib / "C")) == ["Fight - 格斗.7z", "Fight - 格斗.zip"]


def test_same_name_puts_the_outer_back_if_the_inner_will_not_go_in(lib, monkeypatch):
    (lib / "C/Brush.zip").write_bytes(b"outer zip")
    monkeypatch.setattr(unpack, "run_7zz", Fake7zz({"Brush.zip": {"Brush/Brush.zip": INNER}}))

    with pytest.raises(lift.LiftError, match="外层已还原"):
        lift.lift_one(lib, "C/Brush.zip", None, [], move=failing_on(2))
    assert (lib / "C/Brush.zip").read_bytes() == b"outer zip"


def test_a_cover_that_will_not_move_does_not_undo_the_lift(lib):
    assert lift.lift_one(lib, OUTER, None, [], move=failing_on(3)) == "C/Fight - 格斗.zip"
    assert sorted(os.listdir(lib / "C")) == ["Fight - 格斗.zip"]


def test_the_job_lifts_each_path_and_asks_for_a_scan(lib, tmp_path):
    settings = Settings(roots={"lib": lib}, data_dir=tmp_path / "data")
    conn = connect(settings.db_path)
    request = {"action": "lift", "root": "lib", "paths": [OUTER, "C/Gone.7z"]}

    assert move_job.parse(request, settings) == ("lift", "lib", None)
    done = move_job.run(conn, settings, request, move=fake_move)

    assert done.moved == 1 and done.total == 2 and (lib / "C/Fight - 格斗.zip").exists()
    assert done.problems[0]["path"] == "C/Gone.7z"
    assert (settings.data_dir / "scan.request").exists()
    with pytest.raises(ValueError):
        move_job.parse({"action": "lift", "root": "lib"}, settings)
    conn.close()
