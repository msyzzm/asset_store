import zipfile

import pytest

from assetindex import move_job, verify
from assetindex.config import Settings
from assetindex.db import connect


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    for name in ("good.zip", "bad.zip"):
        with zipfile.ZipFile(root / name, "w", zipfile.ZIP_STORED) as z:
            z.writestr("a.txt", b"hello world " * 100)
    data = bytearray((root / "bad.zip").read_bytes())
    data[60] ^= 0x01  # one flipped bit in the stored file
    (root / "bad.zip").write_bytes(bytes(data))
    settings = Settings(roots={"lib": root}, data_dir=tmp_path / "data")
    conn = connect(settings.db_path)
    yield conn, settings
    conn.close()


@pytest.fixture(autouse=True)
def no_7zz(monkeypatch):
    monkeypatch.setattr(verify, "sevenzip", lambda: None)  # the bsdtar tests; 7zz ones set it


def test_7zz_comes_first_and_tells_damage_from_what_it_cannot_test(env, monkeypatch):
    import subprocess

    _, settings = env
    good = settings.roots["lib"] / "good.zip"
    calls = []
    monkeypatch.setattr(verify, "sevenzip", lambda: "7zz")

    def said(text, code):
        def run(cmd, **kwargs):
            calls.append(cmd[0])
            return subprocess.CompletedProcess(cmd, code, text.encode(), b"")
        monkeypatch.setattr(subprocess, "run", run)

    said("Everything is Ok", 0)
    assert verify.test_archive(good) is None and calls == ["7zz"]
    said("ERROR: CRC Failed : Maps/MAP_Demo.umap\nSub items Errors: 1", 2)
    assert "CRC Failed" in verify.test_archive(good)
    said("ERROR: Wrong password : a.txt", 2)
    with pytest.raises(verify.Untested, match="密码"):
        verify.test_archive(good)
    said("ERROR: Unsupported Method : a.txt", 2)
    with pytest.raises(verify.Untested):
        verify.test_archive(good)


def fake_test(path):
    return "CRC 不对" if path.name.startswith("bad") else None


def test_a_job_reports_each_archive_whole_or_damaged(env):
    conn, settings = env
    request = {"action": "verify", "root": "lib", "paths": ["good.zip", "bad.zip", "gone.zip"]}

    done = move_job.run(conn, settings, request, test_archive=fake_test)

    assert done.action == "verify" and done.total == 3 and done.moved == 1 and done.error is None
    assert {p["path"]: p["reason"] for p in done.problems} == {
        "good.zip": verify.WHOLE, "bad.zip": "损坏：CRC 不对", "gone.zip": "找不到这个文件"}


def test_a_test_that_cannot_run_never_calls_a_copy_damaged(env, monkeypatch):
    conn, settings = env
    (settings.roots["lib"] / "empty.zip").write_bytes(b"")
    monkeypatch.setattr(verify, "bsdtar", lambda: None)

    done = move_job.run(conn, settings, {"action": "verify", "root": "lib", "paths": ["good.zip"]})
    assert done.moved == 0 and done.problems[0]["reason"] == "无法校验：没有 7zz 或 bsdtar"

    monkeypatch.setattr(verify, "bsdtar", lambda: "bsdtar")
    with pytest.raises(verify.Untested, match="空文件"):
        verify.test_archive(settings.roots["lib"] / "empty.zip")


def test_what_bsdtar_cannot_decode_is_not_tested_and_name_warnings_are_ignored(env, monkeypatch):
    import subprocess

    _, settings = env
    good = settings.roots["lib"] / "good.zip"
    monkeypatch.setattr(verify, "bsdtar", lambda: "bsdtar")

    def said(text, code=1):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, code, b"", text.encode()))

    said("x: Can't decompress an entry marked as a solid entry\nPathname contains incomplete multibyte or wide character")
    with pytest.raises(verify.Untested, match="读不了"):
        verify.test_archive(good)
    said("bsdtar: Pathname contains incomplete multibyte or wide character", code=0)
    assert verify.test_archive(good) is None
    said("a.txt: ZIP bad CRC")
    assert "bad CRC" in verify.test_archive(good)


def test_parse_wants_paths_only_for_verifying(env):
    _, settings = env
    assert move_job.parse({"action": "verify", "root": "lib", "paths": ["a.zip"]}, settings) == ("verify", "lib", None)
    for bad in ({"action": "verify", "root": "lib"}, {"action": "verify", "root": "lib", "paths": []},
                {"action": "verify", "root": "lib", "paths": ["../x.zip"]}, {"action": "verify", "root": "lib", "paths": [1]},
                {"action": "verify", "root": "lib", "paths": ["a"] * (move_job.MAX_VERIFY + 1)},
                {"action": "apply", "root": "lib", "paths": ["a.zip"]}):
        with pytest.raises(ValueError):
            move_job.parse(bad, settings)


@pytest.mark.skipif(verify.bsdtar() is None, reason="no bsdtar here")
def test_bsdtar_finds_a_flipped_bit(env):
    _, settings = env
    root = settings.roots["lib"]

    assert verify.test_archive(root / "good.zip") is None
    assert verify.test_archive(root / "bad.zip")
