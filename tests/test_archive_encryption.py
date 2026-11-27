"""Locked content: archives whose names list but whose files need a password are counted
when listed, and archives listed before that are counted once without being relisted."""
import zipfile

import py7zr
import pytest
from fastapi.testclient import TestClient

from assetindex import archives
from assetindex.archive_index import index_archives
from assetindex.archives import encrypted_count, list_archive
from assetindex.config import Settings
from assetindex.db import connect
from assetindex.scanner import scan_root
from assetindex.web import create_app


def _zip(path, names, locked=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name in names:
            z.writestr(name, b"data")
    if locked:  # zipfile cannot write encrypted entries: set their "encrypted" flag bit
        data = bytearray(path.read_bytes())
        for signature, flag_at in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            at = 0
            while (at := data.find(signature, at)) != -1:
                name_len = int.from_bytes(data[at + (26 if signature == b"PK\x03\x04" else 28):][:2], "little")
                name = bytes(data[at + (30 if signature == b"PK\x03\x04" else 46):][:name_len]).decode()
                if name in locked:
                    data[at + flag_at] |= 1
                at += 4
        path.write_bytes(bytes(data))
    return path


def _7z(path, names, password=None, header=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with py7zr.SevenZipFile(path, "w", password=password, header_encryption=header) as z:
        for name in names:
            z.writestr(b"data", name)
    return path


def test_a_zip_counts_its_locked_files(tmp_path):
    path = _zip(tmp_path / "a.zip", ["Content/a.duf", "Content/b.dsf", "Preview.jpg"], locked={"Content/a.duf", "Content/b.dsf"})

    assert list_archive(path, "zip").encrypted == 2
    assert encrypted_count(path, "zip") == 2
    assert encrypted_count(_zip(tmp_path / "plain.zip", ["a.png"]), "zip") == 0


def test_a_7z_with_locked_content_counts_every_file(tmp_path):
    locked = _7z(tmp_path / "locked.7z", ["a.duf", "b.jpg"], password="pw")

    assert list_archive(locked, "7z").encrypted == 2
    assert encrypted_count(locked, "7z") == 2
    assert encrypted_count(_7z(tmp_path / "plain.7z", ["a.duf"]), "7z") == 0


class _RarInfo:
    def __init__(self, name, locked):
        self.filename, self.file_size, self._locked = name, 4, locked

    def is_dir(self):
        return False

    def needs_password(self):
        return self._locked


class _Rar:
    def __init__(self, path):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def infolist(self):
        return [_RarInfo("Content/a.duf", True), _RarInfo("Preview.jpg", False)]


def test_a_rar_counts_its_locked_files(tmp_path, monkeypatch):
    monkeypatch.setattr(archives.rarfile, "RarFile", _Rar)
    path = tmp_path / "a.rar"
    path.write_bytes(b"Rar!\x1a\x07\x01\x00rest")

    assert list_archive(path, "rar").encrypted == 1
    assert encrypted_count(path, "rar") == 1


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "lib"
    _zip(root / "Locked.zip", ["Content/a.duf", "Preview.jpg"], locked={"Content/a.duf"})
    _zip(root / "Plain.zip", ["Content/b.duf"])
    _7z(root / "Hidden.7z", ["a.duf"], password="pw", header=True)  # not even its names list
    settings = Settings(roots={"nas": root}, data_dir=tmp_path / "data")
    conn = connect(settings.db_path)
    scan_root(conn, "nas", root)
    index_archives(conn, "nas", root)
    yield settings, conn, root
    conn.close()


def _encrypted(conn):
    return dict(conn.execute("SELECT f.rel_path, a.encrypted FROM files f JOIN archives a ON a.file_id = f.id"))


def test_listing_stores_the_count(lib):
    _, conn, _ = lib

    assert _encrypted(conn) == {"Locked.zip": 1, "Plain.zip": 0, "Hidden.7z": None}


def test_archives_listed_before_are_counted_without_relisting(lib):
    _, conn, root = lib
    conn.execute("UPDATE archives SET encrypted = NULL, preview_state = 1")  # as listed before 2026-11-26
    conn.commit()

    stats = index_archives(conn, "nas", root)

    assert stats.listed == 0  # nothing relisted, so no cover is redone
    assert _encrypted(conn) == {"Locked.zip": 1, "Plain.zip": 0, "Hidden.7z": None}
    assert conn.execute("SELECT preview_state FROM archives a JOIN files f ON f.id = a.file_id"
                        " WHERE f.rel_path = 'Locked.zip'").fetchone()[0] == 1


def test_the_agent_api_lists_every_locked_archive(lib):
    settings, _, _ = lib
    client = TestClient(create_app(settings))

    body = client.get("/api/agent/encrypted").json()

    assert body["total"] == 2
    assert {(i["path"], i["encrypted"]) for i in body["items"]} == {("nas:/Locked.zip", 1), ("nas:/Hidden.7z", None)}
    hidden = next(i for i in body["items"] if i["path"] == "nas:/Hidden.7z")
    assert hidden["names_locked"] is True
    found = client.get("/api/agent/search", params={"q": "Locked", "mode": "text"}).json()["items"][0]
    assert found["archive"]["encrypted"] == 1
