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


def _hashed_paths(conn):
    rows = conn.execute("SELECT f.rel_path FROM file_hashes h JOIN files f ON f.id = h.file_id")
    return {r[0] for r in rows}


def test_only_possible_twins_are_sampled_and_only_suspects_read_whole(library):
    conn, settings = library

    stats = hash_pending(conn, settings, "lib")

    # Sampled: 300 B a.png x2, b.png x2, shared.png, own.png; 500 B the three zips. Not readme.txt.
    assert stats.sampled == 9 and stats.failed == 0
    # Read whole: same sample as another file AND a pack of the same shape (files, total size).
    # Other Pack (2 files) matches no pack, so its shared.png is not; same-size.zip's sample is unique.
    assert _hashed_paths(conn) == {
        "2D/Knight Pack/sprites/a.png", "2D/Knight Pack/sprites/b.png",
        "2D/Knight Pack (1)/sprites/a.png", "2D/Knight Pack (1)/sprites/b.png",
        "2D/sword.zip", "itch/sword copy.zip",
    }
    again = hash_pending(conn, settings, "lib")
    assert (again.sampled, again.hashed) == (0, 0)  # cached


def test_same_sample_different_content_is_told_apart_by_reading_whole(tmp_path, conn, monkeypatch):
    monkeypatch.setattr(dupes, "SAMPLE_CHUNK", 10)  # samples cover bytes 0-10, 245-255, 490-500
    root = tmp_path / "lib"
    _put(root, "x.zip", b"Q" * 500)
    _put(root, "y.zip", b"Q" * 100 + b"R" + b"Q" * 399)  # differs only where the sample doesn't look
    settings = Settings(roots={"lib": root}, data_dir=tmp_path / "data")
    scan_root(conn, "lib", root)

    stats = hash_pending(conn, settings, "lib")

    assert (stats.sampled, stats.hashed) == (2, 2)
    assert duplicate_units(_units(conn)) == []


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


def test_lone_files_are_never_called_twins_on_their_samples_alone(library, monkeypatch):
    conn, settings = library

    def unreadable(path):
        raise OSError("NAS went away")

    monkeypatch.setattr(dupes, "file_hash", unreadable)
    hash_pending(conn, settings, "lib")

    keepers = {g.keep for g in duplicate_units(_units(conn))}
    assert "2D/sword.zip" not in keepers  # same sample, but never read whole


def test_unreadable_files_are_skipped(library, tmp_path):
    conn, settings = library
    os.remove(tmp_path / "lib" / "2D" / "sword.zip")

    stats = hash_pending(conn, settings, "lib")

    assert stats.failed == 1 and stats.sampled == 8
    assert "itch/sword copy.zip" not in _hashed_paths(conn)  # its twin is gone: nothing to compare


def test_stop_keeps_what_was_sampled(library):
    conn, settings = library
    calls = iter([False, False, True])

    stats = hash_pending(conn, settings, "lib", should_stop=lambda: next(calls))

    assert stats.stopped and stats.sampled == 2
    assert conn.execute("SELECT COUNT(*) FROM file_samples").fetchone()[0] == 2


def test_signature_changes_with_any_file(library):
    conn, _ = library
    files = _units(conn)[("2D/Knight Pack", True)]
    touched = [dupes.UnitFile(f.rel, f.size, f.mtime + 1, None) if f.rel == "readme.txt" else f for f in files]
    assert signature(touched) != signature(files)
