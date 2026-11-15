import io
import tarfile
import zipfile

import py7zr
import pytest

from assetindex import archives
from assetindex.archives import ArchiveError, detect_engine, list_archive


def _zip(path, names):
    with zipfile.ZipFile(path, "w") as z:
        for name in names:
            z.writestr(name, b"data" if not name.endswith("/") else b"")
    return path


def _paths(listing):
    return sorted(e.path for e in listing.entries)


def test_zip_lists_files_and_skips_dirs_and_ignored(tmp_path):
    path = _zip(tmp_path / "a.zip", [
        "Pack/", "Pack/Textures/sword.png", "Pack/node_modules/x.js", "Pack/.git/HEAD", "readme.txt",
    ])

    listing = list_archive(path, "zip")

    assert _paths(listing) == ["Pack/Textures/sword.png", "readme.txt"]
    assert listing.entries[0].size == 4
    assert not listing.truncated


def test_zip_gbk_name_without_utf8_flag_is_decoded(tmp_path):
    # Windows zip tools write GBK names without the UTF-8 flag; Python reads them as cp437.
    path = _zip(tmp_path / "gbk.zip", ["XXXX.png"])
    gbk = "刀剑".encode("gbk")
    path.write_bytes(path.read_bytes().replace(b"XXXX", gbk))

    assert _paths(list_archive(path, "zip")) == ["刀剑.png"]


def test_7z_lists_files(tmp_path):
    path = tmp_path / "a.7z"
    with py7zr.SevenZipFile(path, "w") as z:
        z.writestr(b"x", "5.1/SwordEffects/Blueprints/BP_sword1.uasset")
        z.writestr(b"y", "5.1/SwordEffects/Maps/Demo.umap")

    listing = list_archive(path, "7z")

    assert _paths(listing) == [
        "5.1/SwordEffects/Blueprints/BP_sword1.uasset",
        "5.1/SwordEffects/Maps/Demo.umap",
    ]
    assert (listing.engine, listing.engine_version) == ("unreal", "5.1")


def _tar_add(tar, name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def test_unitypackage_uses_pathname_and_records_preview(tmp_path):
    path = tmp_path / "fx.unitypackage"
    with tarfile.open(path, "w:gz") as tar:
        _tar_add(tar, "aaa/pathname", b"Assets/FX/Slash.prefab\n00")
        _tar_add(tar, "aaa/asset", b"12345")
        _tar_add(tar, "aaa/preview.png", b"png")
        _tar_add(tar, "bbb/pathname", b"Assets/FX")  # folder: no asset member
        _tar_add(tar, "ccc/asset", b"orphan")  # no pathname: skipped

    listing = list_archive(path, "unitypackage")

    assert [(e.path, e.size, e.guid, e.has_preview) for e in listing.entries] == [
        ("Assets/FX/Slash.prefab", 5, "aaa", True),
    ]
    assert listing.engine == "unity"


def test_rar_uses_rarfile(tmp_path, monkeypatch):
    class Info:
        def __init__(self, filename, size, is_dir=False):
            self.filename, self.file_size, self._dir = filename, size, is_dir

        def is_dir(self):
            return self._dir

    class FakeRar:
        def __init__(self, path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def infolist(self):
            return [Info("Pack", 0, True), Info("Pack/a.wav", 7)]

    monkeypatch.setattr(archives.rarfile, "RarFile", FakeRar)

    assert [(e.path, e.size) for e in list_archive(tmp_path / "x.rar", "rar").entries] == [("Pack/a.wav", 7)]


def test_corrupt_archive_raises_archive_error(tmp_path):
    for ext in ("zip", "7z", "unitypackage"):
        bad = tmp_path / f"bad.{ext}"
        bad.write_bytes(b"not an archive")
        with pytest.raises(ArchiveError):
            list_archive(bad, ext)


def test_missing_file_raises_oserror_not_archive_error(tmp_path):
    with pytest.raises(OSError):
        list_archive(tmp_path / "gone.zip", "zip")


def test_entries_are_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(archives, "MAX_ENTRIES", 3)
    path = _zip(tmp_path / "many.zip", [f"f{i}.png" for i in range(5)])

    listing = list_archive(path, "zip")

    assert len(listing.entries) == 3
    assert listing.truncated


@pytest.mark.parametrize(
    ("ext", "paths", "expected"),
    [
        ("zip", ["Content/Sword/BP.uasset"], ("unreal", None)),
        ("zip", ["UE5.3/Content/BP.uasset"], ("unreal", "5.3")),
        ("rar", ["Pack 4.27/Content/M.umap"], ("unreal", None)),  # segment must be just the version
        ("zip", ["Assets/FX.prefab", "Assets/FX.prefab.meta"], ("unity", None)),
        ("zip", ["Pack/FX.unitypackage"], ("unity", None)),
        ("zip", ["a.png"], (None, None)),
    ],
)
def test_detect_engine(ext, paths, expected):
    assert detect_engine(ext, paths) == expected


def test_control_and_bidi_characters_are_dropped(tmp_path):
    path = _zip(tmp_path / "evil.zip", ["ok.png", "gnp\u202eexe.png", "line\nbreak.png"])

    assert _paths(list_archive(path, "zip")) == ["ok.png"]


def test_unity_assets_folder_may_be_nested():
    assert detect_engine("zip", ["Pack/Assets/FX.prefab.meta"]) == ("unity", None)


def test_unitypackage_decompression_is_capped(tmp_path, monkeypatch):
    path = tmp_path / "huge.unitypackage"
    with tarfile.open(path, "w:gz") as tar:
        _tar_add(tar, "aaa/asset", b"\0" * 100_000)
    monkeypatch.setattr(archives, "MAX_UNITYPACKAGE_BYTES", 10_000)

    with pytest.raises(ArchiveError):
        list_archive(path, "unitypackage")


def test_memory_error_is_not_an_archive_error(tmp_path, monkeypatch):
    def boom(path):
        raise MemoryError

    monkeypatch.setitem(archives._LISTERS, "zip", boom)

    with pytest.raises(MemoryError):
        list_archive(tmp_path / "a.zip", "zip")
