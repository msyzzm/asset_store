import sqlite3

import pytest
from fastapi.testclient import TestClient

from assetindex.config import Settings
from assetindex.db import connect
from assetindex.scanner import scan_root
from assetindex.search import search
from assetindex.sequences import SEQUENCE_MIN_FRAMES, sequence_of
from assetindex.web import create_app

FX_FRAMES = (1, 2, 3, 4, 5, 10)


@pytest.fixture
def seq_root(tmp_path):
    root = tmp_path / "seq"
    fx = root / "fx"
    fx.mkdir(parents=True)
    for i in FX_FRAMES:
        (fx / f"explode_{i}.png").write_bytes(b"p")
    for i in (1, 2, 3):  # below the threshold: variants, not an animation
        (fx / f"tree{i}.png").write_bytes(b"p")
    (fx / "explode_7.tga").write_bytes(b"t")  # other ext: separate group
    return root


def _names(result):
    return sorted((item["name"], item["frames"]) for item in result.items)


@pytest.mark.parametrize(
    ("rel_path", "kind", "expected"),
    [
        ("fx/explode_01.png", "image", ("fx/explode_#png", 1)),
        ("fx/Explode_12.PNG", "image", ("fx/Explode_#png", 12)),
        ("0003.jpg", "image", ("#jpg", 3)),
        ("fx/explode.png", "image", None),
        ("music/track01.mp3", "audio", None),
        ("fx/stamp_20231114093000.png", "image", None),  # >9 digits: an id, not a frame
    ],
)
def test_sequence_of(rel_path, kind, expected):
    assert sequence_of(rel_path, kind) == expected


def test_threshold_is_five():
    assert SEQUENCE_MIN_FRAMES == 5


def test_frames_collapse_into_one_head_item(conn, seq_root):
    scan_root(conn, "art", seq_root)

    result = search(conn, "")

    assert result.total == 5  # 1 sequence head + 3 trees + 1 tga
    assert _names(result) == [
        ("explode_1.png", 6),
        ("explode_7.tga", None),
        ("tree1.png", None),
        ("tree2.png", None),
        ("tree3.png", None),
    ]


def test_query_matches_head_only(conn, seq_root):
    scan_root(conn, "art", seq_root)

    assert _names(search(conn, "explode", kind="image")) == [
        ("explode_1.png", 6),
        ("explode_7.tga", None),
    ]


def test_query_matching_a_hidden_frame_returns_its_head(conn, seq_root):
    scan_root(conn, "art", seq_root)

    assert _names(search(conn, "explode_3")) == [("explode_1.png", 6)]
    assert search(conn, "explode_3").total == 1


def test_group_dissolves_when_frames_drop_below_threshold(conn, seq_root):
    scan_root(conn, "art", seq_root)
    for i in (4, 5):
        (seq_root / "fx" / f"explode_{i}.png").unlink()

    scan_root(conn, "art", seq_root)

    names = _names(search(conn, "explode_"))
    assert ("explode_1.png", None) in names
    assert ("explode_10.png", None) in names
    assert len(names) == 5  # 4 png frames + tga, all visible again


def test_head_moves_when_first_frame_removed(conn, seq_root):
    scan_root(conn, "art", seq_root)
    (seq_root / "fx" / "explode_1.png").unlink()
    (seq_root / "fx" / "explode_11.png").write_bytes(b"p")

    scan_root(conn, "art", seq_root)

    assert ("explode_2.png", 6) in _names(search(conn, "explode"))


def test_connect_migrates_old_schema(tmp_path):
    db_path = tmp_path / "old.db"
    old = sqlite3.connect(db_path)
    old.execute(
        "CREATE TABLE files (id INTEGER PRIMARY KEY, root TEXT NOT NULL, rel_path TEXT NOT NULL,"
        " name TEXT NOT NULL, ext TEXT NOT NULL, kind TEXT NOT NULL, size INTEGER NOT NULL,"
        " mtime REAL NOT NULL, scan_id INTEGER NOT NULL, UNIQUE (root, rel_path))"
    )
    old.execute("INSERT INTO files VALUES (1, 'art', 'a1.png', 'a1.png', 'png', 'image', 1, 0, 0)")
    old.commit()
    old.close()

    conn = connect(db_path)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(files)")}
    assert {"seq_key", "seq_index", "seq_frames"} <= cols
    assert search(conn, "").items[0]["frames"] is None


def test_frames_endpoint_returns_numeric_order(seq_root, tmp_path):
    settings = Settings(roots={"art": seq_root}, data_dir=tmp_path / "data")
    with TestClient(create_app(settings)) as client:
        conn = connect(settings.db_path)
        scan_root(conn, "art", seq_root)
        ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM files")}
        conn.close()

        # Any member resolves the whole sequence, in frame order (10 after 5).
        body = client.get(f"/api/files/{ids['explode_3.png']}/frames").json()
        assert [f["name"] for f in body["frames"]] == [f"explode_{i}.png" for i in FX_FRAMES]
        assert set(body["frames"][0]) >= {"id", "name", "ext", "mtime", "size", "rel_path"}

        single = client.get(f"/api/files/{ids['tree1.png']}/frames").json()
        assert [f["name"] for f in single["frames"]] == ["tree1.png"]

        assert client.get("/api/files/999999/frames").status_code == 404
