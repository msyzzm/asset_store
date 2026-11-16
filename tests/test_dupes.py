import os

import pytest

from assetindex import dupes
from assetindex.config import Settings
from assetindex.dupes import duplicate_units, file_duplicates, hash_pending, signature, unit_files
from assetindex.scanner import scan_root
from assetindex.units import opened_folders


@pytest.fixture(autouse=True)
def small_threshold(monkeypatch):
    monkeypatch.setattr(dupes, "HASH_MIN_BYTES", 100)


def _put(root, rel, data: bytes):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@pytest.fixture
def library(tmp_path, conn):
    root = tmp_path / "lib"
    pack = {"sprites/a.png": b"A" * 300, "sprites/b.png": b"B" * 300, "readme.txt": b"hi"}
    for name in ("Knight Pack", "Knight Pack (1)"):
        for rel, data in pack.items():
            _put(root, f"2D/{name}/{rel}", data)
    _put(root, "2D/sword.zip", b"Z" * 500)
    _put(root, "itch/sword copy.zip", b"Z" * 500)
    _put(root, "2D/same-size.zip", b"Y" * 500)  # same size, different content: not a duplicate
    _put(root, "2D/Other Pack/shared.png", b"A" * 300)  # one file repeated in another pack
    _put(root, "2D/Other Pack/own.png", b"O" * 300)
    settings = Settings(roots={"lib": root}, data_dir=tmp_path / "data")
    scan_root(conn, "lib", root)
    return conn, settings


def _units(conn):
    paths = [r[0] for r in conn.execute("SELECT rel_path FROM files")]
    return unit_files(conn, "lib", opened_folders(paths))


def test_only_possible_twins_are_hashed(library):
    conn, settings = library

    stats = hash_pending(conn, settings, "lib")

    # 300 B: a.png x2, b.png x2, shared.png, own.png; 500 B: the three zips. Not readme.txt.
    assert stats.hashed == 9 and stats.failed == 0
    assert hash_pending(conn, settings, "lib").hashed == 0  # cached


def test_identical_packs_and_archives_are_grouped_with_a_sensible_keeper(library):
    conn, settings = library
    hash_pending(conn, settings, "lib")

    groups = {g.keep: g for g in duplicate_units(_units(conn))}

    assert set(groups) == {"2D/Knight Pack", "2D/sword.zip"}
    assert groups["2D/Knight Pack"].others == ("2D/Knight Pack (1)",)
    assert groups["2D/sword.zip"].others == ("itch/sword copy.zip",) and groups["2D/sword.zip"].exact
    assert not groups["2D/Knight Pack"].exact  # readme.txt is below the threshold: name and size only
    assert groups["2D/Knight Pack"].wasted == 602


def test_files_repeated_across_packs_are_only_reported(library):
    conn, settings = library
    hash_pending(conn, settings, "lib")
    units = _units(conn)
    skip = {p for g in duplicate_units(units) for p in (g.keep, *g.others)}

    report = file_duplicates(units, skip)

    assert report["groups"] == 0  # Other Pack's shared.png matches files of skipped packs only
    report = file_duplicates(units, {"2D/Knight Pack (1)", "itch/sword copy.zip"})
    assert report["groups"] == 1 and report["wasted"] == 300
    assert report["top"][0]["paths"] == ["2D/Knight Pack/sprites/a.png", "2D/Other Pack/shared.png"]


def test_copies_inside_one_pack_are_counted_apart(library, tmp_path):
    conn, settings = library
    _put(tmp_path / "lib", "2D/Other Pack/again/own.png", b"O" * 300)
    scan_root(conn, "lib", tmp_path / "lib")
    hash_pending(conn, settings, "lib")

    report = file_duplicates(_units(conn), set())

    assert report["inner_groups"] == 1 and report["inner_wasted"] == 300


def test_preferred_copy_is_kept_and_copyright_is_not_a_copy_mark(library):
    conn, settings = library
    hash_pending(conn, settings, "lib")

    keepers = {g.keep for g in duplicate_units(_units(conn), preferred={"2D/Knight Pack (1)"})}

    assert "2D/Knight Pack (1)" in keepers
    assert dupes._keep_rank("Copyright Free Pack")[1] is False


def test_unreadable_files_are_skipped(library, tmp_path):
    conn, settings = library
    os.remove(tmp_path / "lib" / "2D" / "sword.zip")

    stats = hash_pending(conn, settings, "lib")

    assert stats.failed == 1 and stats.hashed == 8


def test_stop_keeps_what_was_hashed(library):
    conn, settings = library
    calls = iter([False, False, True])

    stats = hash_pending(conn, settings, "lib", should_stop=lambda: next(calls))

    assert stats.stopped and stats.hashed == 2
    assert conn.execute("SELECT COUNT(*) FROM file_hashes").fetchone()[0] == 2


def test_signature_changes_with_any_file(library):
    conn, _ = library
    files = _units(conn)[("2D/Knight Pack", True)]
    touched = [dupes.UnitFile(f.rel, f.size, f.mtime + 1, None) if f.rel == "readme.txt" else f for f in files]
    assert signature(touched) != signature(files)
