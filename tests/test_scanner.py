import os

from assetindex import scanner
from assetindex.scanner import scan_root


def _rows(conn):
    return {r["rel_path"]: r for r in conn.execute("SELECT * FROM files")}


def test_scan_indexes_files_with_kind_and_skips_ignored_dirs(conn, asset_root):
    stats = scan_root(conn, "art", asset_root)

    rows = _rows(conn)
    assert stats.added == 5
    assert "tool/node_modules/junk.js" not in rows
    assert rows["特效/刀剑特效.7z"]["kind"] == "archive"
    assert rows["Audio/Battle_Theme.mp3"]["kind"] == "audio"
    assert rows["3D/Knight/knight_sword.fbx"]["kind"] == "model"
    assert rows["2D/Icons/sword_icon.png"]["kind"] == "image"
    assert rows["readme.txt"]["kind"] == "other"
    assert rows["Audio/Battle_Theme.mp3"]["size"] == 1000


def test_rescan_detects_changed_and_removed_files_and_keeps_ids(conn, asset_root):
    scan_root(conn, "art", asset_root)
    old_id = _rows(conn)["Audio/Battle_Theme.mp3"]["id"]

    mp3 = asset_root / "Audio" / "Battle_Theme.mp3"
    mp3.write_bytes(b"new")
    os.utime(mp3, (1_000_000_000, 1_000_000_000))
    (asset_root / "readme.txt").unlink()

    stats = scan_root(conn, "art", asset_root)

    rows = _rows(conn)
    assert (stats.added, stats.updated, stats.removed) == (0, 1, 1)
    assert "readme.txt" not in rows
    assert rows["Audio/Battle_Theme.mp3"]["id"] == old_id
    assert rows["Audio/Battle_Theme.mp3"]["size"] == 3


def test_empty_root_does_not_wipe_existing_index(conn, asset_root, tmp_path):
    # An unmounted NFS share shows up as an empty mountpoint directory.
    scan_root(conn, "art", asset_root)
    empty = tmp_path / "unmounted"
    empty.mkdir()

    stats = scan_root(conn, "art", empty)

    assert stats.removed == 0
    assert stats.deletion_skipped
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 5


def test_unreadable_dir_protects_only_its_subtree(conn, asset_root, monkeypatch):
    scan_root(conn, "art", asset_root)
    (asset_root / "readme.txt").unlink()
    real_walk = scanner.os.walk

    def flaky_walk(top, onerror=None):
        onerror(OSError(5, "I/O error", os.path.join(top, "Audio")))
        for dirpath, dirnames, filenames in real_walk(top):
            if "Audio" not in dirpath:
                yield dirpath, dirnames, filenames

    monkeypatch.setattr(scanner.os, "walk", flaky_walk)
    stats = scan_root(conn, "art", asset_root)

    rows = _rows(conn)
    assert not stats.deletion_skipped
    assert stats.protected_prefixes == ("Audio",)
    assert "Audio/Battle_Theme.mp3" in rows  # unreadable, so kept
    assert "readme.txt" not in rows  # really deleted, so removed
    assert stats.removed == 1


def test_unreadable_root_skips_all_deletion(conn, asset_root, monkeypatch):
    scan_root(conn, "art", asset_root)

    def broken_walk(top, onerror=None):
        onerror(OSError(5, "I/O error", str(top)))
        return iter(())

    monkeypatch.setattr(scanner.os, "walk", broken_walk)
    stats = scan_root(conn, "art", asset_root)

    assert stats.deletion_skipped
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 5


def test_undecodable_filename_is_skipped_not_fatal(conn, asset_root, monkeypatch):
    # On Linux, a GBK-encoded name on NFS comes back from os.walk with lone surrogates.
    real_walk = scanner.os.walk

    def walk_with_bad_name(top, onerror=None):
        for dirpath, dirnames, filenames in real_walk(top):
            extra = ["bad\udcb5\udcb6.png"] if dirpath == str(top) else []
            yield dirpath, dirnames, [*filenames, *extra]

    monkeypatch.setattr(scanner.os, "walk", walk_with_bad_name)

    stats = scan_root(conn, "art", asset_root)

    assert stats.added == 5
    assert not stats.deletion_skipped


def test_scan_of_one_root_does_not_touch_other_root(conn, asset_root, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.wav").write_bytes(b"w")
    scan_root(conn, "other", other)

    scan_root(conn, "art", asset_root)

    roots = {r["root"] for r in conn.execute("SELECT root FROM files")}
    assert roots == {"art", "other"}


def test_stopped_walk_keeps_the_files_it_did_not_reach(conn, asset_root):
    import itertools

    import pytest

    from assetindex.scan_request import ScanStopped

    scan_root(conn, "art", asset_root)
    before = set(_rows(conn))
    flags = itertools.chain([False], itertools.repeat(True))  # stop after the first folder

    with pytest.raises(ScanStopped):
        scan_root(conn, "art", asset_root, should_stop=lambda: next(flags))

    assert set(_rows(conn)) == before  # not seen this time is not "deleted"
