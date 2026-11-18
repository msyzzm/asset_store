import io
import os
import tarfile
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from assetindex.archive_index import index_archives
from assetindex.archive_previews import build_previews, choose_candidates
from assetindex.config import Settings
from assetindex.db import connect
from assetindex.extract import find_bsdtar
from assetindex.scanner import scan_root
from assetindex.thumbs import archive_preview_dir, sheet_path
from assetindex.web import create_app

TOOL = find_bsdtar()
needs_bsdtar = pytest.mark.skipif(TOOL is None, reason="bsdtar not installed")
GUID_A = "a" * 32
GUID_B = "b" * 32


def _png(color=(255, 0, 0, 255), size=(16, 16)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, "PNG")
    return buf.getvalue()


def _zip(path, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def _tar_add(tar, name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


@pytest.fixture
def preview_root(tmp_path):
    root = tmp_path / "packs"
    _zip(root / "icons.zip", {
        "Icons/sword.png": _png((255, 0, 0, 255)),
        "Icons/shield.png": _png((0, 255, 0, 255)),
        "FX/slash.png": _png((0, 0, 255, 255)),
        "readme.txt": b"hi",
    })
    _zip(root / "sounds.zip", {"a.wav": b"RIFF"})
    _zip(root / "fake.zip", {"bad.png": b"not a png"})
    unity = root / "fx.unitypackage"
    with tarfile.open(unity, "w:gz") as tar:
        for guid, path in ((GUID_A, "Assets/FX/Slash.prefab"), (GUID_B, "Assets/FX/Spark.mat")):
            _tar_add(tar, f"{guid}/pathname", path.encode())
            _tar_add(tar, f"{guid}/asset", b"x")
            _tar_add(tar, f"{guid}/preview.png", _png())
        _tar_add(tar, "../evil/pathname", b"Assets/Evil.prefab")  # not a GUID: no preview file
        _tar_add(tar, "../evil/asset", b"x")
        _tar_add(tar, "../evil/preview.png", _png())
    return root


def _ids(conn):
    return {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM files")}


def _state(conn, name):
    return conn.execute(
        "SELECT a.preview_state FROM archives a JOIN files f ON f.id = a.file_id WHERE f.name = ?", (name,)
    ).fetchone()[0]


def _build(conn, root, thumb_dir, tool=TOOL):
    scan_root(conn, "art", root)
    index_archives(conn, "art", root)
    return build_previews(conn, "art", root, thumb_dir, tool)


def test_stop_comes_before_an_archive_so_previews_stay_pending(conn, preview_root, tmp_path):
    from assetindex.scan_request import ScanStopped

    scan_root(conn, "art", preview_root)
    index_archives(conn, "art", preview_root)

    with pytest.raises(ScanStopped):
        build_previews(conn, "art", preview_root, tmp_path / "thumbs", TOOL, should_stop=lambda: True)

    assert _state(conn, "icons.zip") is None
    assert conn.execute("SELECT COUNT(*) FROM scan_attempts").fetchone()[0] == 0


def test_previews_report_progress(conn, preview_root, tmp_path):
    seen = []
    scan_root(conn, "art", preview_root)
    index_archives(conn, "art", preview_root)

    build_previews(conn, "art", preview_root, tmp_path / "thumbs", TOOL,
                   progress=lambda phase, done, total: seen.append((phase, done, total)))

    assert seen and seen[-1][0] == "previews" and seen[-1][1] == seen[-1][2]


def test_choose_candidates_spreads_folders_and_skips_junk():
    entries = [
        (1, "A/a1.png", 10),
        *[(10 + i, f"A/walk_0{i}.png", 10) for i in range(1, 7)],  # one sequence = one candidate
        (20, "B/b.png", 10),
        (21, "__MACOSX/A/._a1.png", 10),
        (22, "C/._hidden.png", 10),
        (23, "C/huge.png", 10**9),
        (24, "C/notes.txt", 10),
    ]

    assert [path for _, path in choose_candidates(entries, limit=5)] == ["A/a1.png", "B/b.png", "A/walk_01.png"]


@needs_bsdtar
def test_sheet_built_once_for_archive_with_images(conn, preview_root, tmp_path):
    thumbs = tmp_path / "thumbs"
    stats = _build(conn, preview_root, thumbs)

    ids = _ids(conn)
    mtime = conn.execute("SELECT mtime FROM files WHERE id = ?", (ids["icons.zip"],)).fetchone()[0]
    assert sheet_path(thumbs, ids["icons.zip"], mtime).exists()
    assert _state(conn, "icons.zip") == 1
    assert _state(conn, "sounds.zip") == 0
    assert _state(conn, "fake.zip") == 0  # images that don't decode
    assert stats.built == 2  # icons.zip + unitypackage

    again = build_previews(conn, "art", preview_root, thumbs, TOOL)
    assert again.built == again.empty == 0


def test_without_bsdtar_zip_previews_stay_pending(conn, preview_root, tmp_path):
    stats = _build(conn, preview_root, tmp_path / "thumbs", tool=None)

    assert _state(conn, "icons.zip") is None  # retried once bsdtar is installed
    assert _state(conn, "fx.unitypackage") == 1  # needs no external tool
    assert stats.skipped == 3


def test_unitypackage_previews_only_for_real_guids(conn, preview_root, tmp_path):
    thumbs = tmp_path / "thumbs"
    _build(conn, preview_root, thumbs, tool=None)

    file_id = _ids(conn)["fx.unitypackage"]
    files = sorted(p.name for p in archive_preview_dir(thumbs, file_id).iterdir())
    assert len([f for f in files if f.startswith("u_")]) == 2
    assert any(f.startswith("sheet_") for f in files)
    assert not (tmp_path / "evil").exists()


@needs_bsdtar
def test_relisted_archive_gets_fresh_previews(conn, preview_root, tmp_path):
    thumbs = tmp_path / "thumbs"
    _build(conn, preview_root, thumbs)
    icons = _zip(preview_root / "icons.zip", {"Only/one.png": _png()})
    os.utime(icons, (1_000_000_000, 1_000_000_000))

    _build(conn, preview_root, thumbs)

    file_id = _ids(conn)["icons.zip"]
    assert _state(conn, "icons.zip") == 1
    assert [p.name for p in archive_preview_dir(thumbs, file_id).iterdir()] == ["sheet_1000000000.webp"]


@needs_bsdtar
def test_web_serves_sheet_and_entry_thumbnails(preview_root, tmp_path):
    settings = Settings(roots={"art": preview_root}, data_dir=tmp_path / "data")
    with TestClient(create_app(settings)) as client:
        conn = connect(settings.db_path)
        _build(conn, preview_root, settings.thumb_dir)
        ids = _ids(conn)
        conn.close()

        sheet = client.get(f"/api/files/{ids['icons.zip']}/thumb")
        assert sheet.status_code == 200 and sheet.headers["content-type"] == "image/webp"
        item = client.get("/api/search", params={"q": "icons"}).json()["items"][0]
        assert item["sheet"] == 1

        entries = client.get(f"/api/files/{ids['icons.zip']}/entries").json()["entries"]
        by_path = {e["path"]: e for e in entries}
        png = by_path["Icons/sword.png"]
        thumb = client.get(f"/api/files/{ids['icons.zip']}/entries/{png['id']}/thumb")
        assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/webp"
        txt = by_path["readme.txt"]
        assert client.get(f"/api/files/{ids['icons.zip']}/entries/{txt['id']}/thumb").status_code == 415
        # An entry id must belong to the archive in the URL.
        assert client.get(f"/api/files/{ids['sounds.zip']}/entries/{png['id']}/thumb").status_code == 404

        unity = client.get(f"/api/files/{ids['fx.unitypackage']}/entries").json()["entries"]
        prefab = next(e for e in unity if e["path"] == "Assets/FX/Slash.prefab")
        assert client.get(f"/api/files/{ids['fx.unitypackage']}/entries/{prefab['id']}/thumb").status_code == 200

        assert client.get(f"/api/files/{ids['sounds.zip']}/thumb").status_code == 415


def test_tiny_sprites_are_scaled_up_crisply_in_sheet(tmp_path):
    from assetindex.thumbs import compose_sheet

    target = tmp_path / "sheet.webp"
    compose_sheet([Image.new("RGBA", (8, 8), (255, 0, 0, 255))], target)

    with Image.open(target) as sheet:
        box = sheet.getchannel("A").getbbox()
    assert box[2] - box[0] >= 300  # 8px sprite fills the 320px cell, not a dot in the middle


def test_io_error_on_one_archive_does_not_abort_and_stays_pending(conn, preview_root, tmp_path, monkeypatch):
    from assetindex import archive_previews

    def broken(path, guids, max_bytes_each):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(archive_previews, "read_unity_previews", broken)
    stats = _build(conn, preview_root, tmp_path / "thumbs", tool=None)

    assert _state(conn, "fx.unitypackage") is None
    assert stats.skipped == 4  # 3 zips without bsdtar + the unreadable unitypackage


def test_timeouts_leave_preview_pending(conn, preview_root, tmp_path, monkeypatch):
    from assetindex import archive_previews
    from assetindex.extract import ExtractTimeout

    def slow(*args, **kwargs):
        raise ExtractTimeout("too slow")

    monkeypatch.setattr(archive_previews, "read_member", slow)
    _build(conn, preview_root, tmp_path / "thumbs", tool="bsdtar")

    assert _state(conn, "icons.zip") is None  # transient: retried next scan
    assert _state(conn, "sounds.zip") == 0  # nothing to try at all


def test_an_archive_timing_out_scan_after_scan_is_given_up(conn, preview_root, tmp_path, monkeypatch):
    """A big solid 7z times out every time: it must not cost every scan minutes for ever."""
    from assetindex import archive_previews
    from assetindex.extract import ExtractTimeout

    def slow(*args, **kwargs):
        raise ExtractTimeout("too slow")

    monkeypatch.setattr(archive_previews, "read_member", slow)
    for _ in range(archive_previews.MAX_POSTPONED - 1):
        _build(conn, preview_root, tmp_path / "thumbs", tool="bsdtar")
        assert _state(conn, "icons.zip") is None
    _build(conn, preview_root, tmp_path / "thumbs", tool="bsdtar")

    assert _state(conn, "icons.zip") == 0  # nothing to show, not tried again


def test_archives_set_aside_get_no_previews(conn, tmp_path):
    root = tmp_path / "packs"
    for folder in ("_待删除", "_Duplicates", "02_3D"):
        _zip(root / folder / "icons.zip", {"Icons/sword.png": _png()})

    stats = _build(conn, root, tmp_path / "thumbs", tool="bsdtar")

    states = dict(conn.execute(
        "SELECT f.rel_path, a.preview_state FROM archives a JOIN files f ON f.id = a.file_id").fetchall())
    assert states["_待删除/icons.zip"] is None and states["_Duplicates/icons.zip"] is None
    assert stats.built + stats.empty + stats.skipped == 1  # only the one in 02_3D was looked at


@needs_bsdtar
def test_failed_entry_extraction_is_not_retried_immediately(preview_root, tmp_path, monkeypatch):
    from assetindex import web
    from assetindex.extract import ExtractError

    settings = Settings(roots={"art": preview_root}, data_dir=tmp_path / "data")
    calls = []

    def failing(*args, **kwargs):
        calls.append(args)
        raise ExtractError("corrupt")

    with TestClient(create_app(settings)) as client:
        conn = connect(settings.db_path)
        _build(conn, preview_root, settings.thumb_dir)
        file_id = _ids(conn)["icons.zip"]
        entry_id = conn.execute(
            "SELECT id FROM archive_entries WHERE path = 'Icons/sword.png'"
        ).fetchone()[0]
        conn.close()
        monkeypatch.setattr(web, "read_member", failing)

        for _ in range(3):
            assert client.get(f"/api/files/{file_id}/entries/{entry_id}/thumb").status_code == 415
        assert len(calls) == 1


@needs_bsdtar
def test_uasset_thumbnails_feed_sheets_entries_and_loose_files(tmp_path):
    from tests.test_uasset import make_uasset

    root = tmp_path / "ue"
    _zip(root / "pack.zip", {
        "5.1/FX/Blueprints/BP_sword.uasset": make_uasset("JPEG"),
        "5.1/FX/Particles/P_spark.uasset": make_uasset("JPEG")[:4] + b"\0" * 64,  # no thumbnail
    })
    loose = root / "Content" / "BP_loose.uasset"
    loose.parent.mkdir(parents=True)
    loose.write_bytes(make_uasset("PNG"))
    settings = Settings(roots={"art": root}, data_dir=tmp_path / "data")

    with TestClient(create_app(settings)) as client:
        conn = connect(settings.db_path)
        _build(conn, root, settings.thumb_dir)
        ids = _ids(conn)
        entries = {r["path"]: r["id"] for r in conn.execute("SELECT id, path FROM archive_entries")}
        conn.close()

        assert _state_of(settings, "pack.zip") == 1
        pack = ids["pack.zip"]
        ok = client.get(f"/api/files/{pack}/entries/{entries['5.1/FX/Blueprints/BP_sword.uasset']}/thumb")
        assert ok.status_code == 200 and ok.headers["content-type"] == "image/webp"
        none = client.get(f"/api/files/{pack}/entries/{entries['5.1/FX/Particles/P_spark.uasset']}/thumb")
        assert none.status_code == 415
        assert client.get(f"/api/files/{ids['BP_loose.uasset']}/thumb").status_code == 200


def _state_of(settings, name):
    conn = connect(settings.db_path)
    try:
        return _state(conn, name)
    finally:
        conn.close()


def test_real_images_are_tried_before_uassets():
    entries = [(1, "A/BP.uasset", 10), (2, "B/M.uasset", 10), (3, "C/icon.png", 10)]

    assert [p for _, p in choose_candidates(entries, limit=2)] == ["C/icon.png", "A/BP.uasset"]


@needs_bsdtar
def test_single_image_fills_the_whole_cover(conn, tmp_path):
    root = tmp_path / "packs"
    _zip(root / "one.zip", {"cover.png": _png(size=(400, 400))})
    thumbs = tmp_path / "thumbs"

    _build(conn, root, thumbs)

    file_id = _ids(conn)["one.zip"]
    mtime = conn.execute("SELECT mtime FROM files WHERE id = ?", (file_id,)).fetchone()[0]
    with Image.open(sheet_path(thumbs, file_id, mtime)) as sheet:
        box = sheet.getchannel("A").getbbox()
    assert box[2] - box[0] >= 300  # not a half-size picture in the middle


@needs_bsdtar
def test_covers_from_an_older_layout_are_rebuilt_once(conn, preview_root, tmp_path):
    thumbs = tmp_path / "thumbs"
    _build(conn, preview_root, thumbs)
    with conn:
        conn.execute("UPDATE archives SET sheet_version = NULL")  # as left by an older release

    rebuilt = build_previews(conn, "art", preview_root, thumbs, TOOL)

    assert rebuilt.built == 1  # icons.zip; unitypackage previews are already full size
    assert _state(conn, "icons.zip") == 1 and _state(conn, "sounds.zip") == 0
    again = build_previews(conn, "art", preview_root, thumbs, TOOL)
    assert again.built == again.empty == 0


@needs_bsdtar
@pytest.mark.parametrize("failure", ["broken", "slow"])
def test_failed_rebuild_keeps_the_old_cover(conn, preview_root, tmp_path, monkeypatch, failure):
    from assetindex import archive_previews
    from assetindex.extract import ExtractError, ExtractTimeout

    thumbs = tmp_path / "thumbs"
    _build(conn, preview_root, thumbs)
    with conn:
        conn.execute("UPDATE archives SET sheet_version = NULL")
    file_id = _ids(conn)["icons.zip"]
    mtime = conn.execute("SELECT mtime FROM files WHERE id = ?", (file_id,)).fetchone()[0]

    def fail(*args, **kwargs):
        raise (ExtractError("flaky NAS") if failure == "broken" else ExtractTimeout("too slow"))

    monkeypatch.setattr(archive_previews, "read_member", fail)
    build_previews(conn, "art", preview_root, thumbs, TOOL)

    assert sheet_path(thumbs, file_id, mtime).exists()
    assert _state(conn, "icons.zip") == 1
