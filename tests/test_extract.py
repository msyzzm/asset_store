import zipfile

import pytest

from assetindex import extract
from assetindex.extract import ExtractError, find_bsdtar, read_member

TOOL = find_bsdtar()
needs_bsdtar = pytest.mark.skipif(TOOL is None, reason="bsdtar not installed")

TRICKY = ["a[1].png", "a1.png", "a*.png", "a?.png", "-x.png", "dir/中文 名.png", "x[!a].png"]


@pytest.fixture
def tricky_zip(tmp_path):
    path = tmp_path / "t.zip"
    with zipfile.ZipFile(path, "w") as z:
        for name in TRICKY:
            z.writestr(name, name.encode())
    return path


@needs_bsdtar
@pytest.mark.parametrize("name", TRICKY)
def test_member_names_are_matched_literally(tricky_zip, name):
    # bsdtar treats operands as globs and "-x" as an option; both must be neutralised.
    assert read_member(tricky_zip, name, 1000, TOOL) == name.encode()


@needs_bsdtar
def test_output_beyond_limit_is_rejected(tricky_zip):
    with pytest.raises(ExtractError):
        read_member(tricky_zip, "a1.png", 3, TOOL)


@needs_bsdtar
def test_missing_member_is_an_error(tricky_zip):
    with pytest.raises(ExtractError):
        read_member(tricky_zip, "nope.png", 1000, TOOL)


@needs_bsdtar
def test_find_bsdtar_reports_a_real_bsdtar():
    assert "bsdtar" in extract._version(TOOL)


def test_find_bsdtar_rejects_non_bsdtar(monkeypatch):
    find_bsdtar.cache_clear()
    monkeypatch.setenv("ASSET_BSDTAR", "/opt/gnu/tar")
    monkeypatch.setattr(extract, "_candidates", lambda: ["/opt/gnu/tar"])
    monkeypatch.setattr(extract, "_version", lambda tool: "tar (GNU tar) 1.35")
    try:
        assert find_bsdtar() is None
    finally:
        find_bsdtar.cache_clear()


@needs_bsdtar
def test_output_is_kept_when_bsdtar_warns_about_other_members(tricky_zip, monkeypatch):
    # bsdtar exits 1 if *other* entries have unreadable names, yet streams the wanted one.
    class WarningProc:
        def __init__(self, *args, **kwargs):
            import io
            self.stdout = io.BytesIO(b"\x89PNG data")

        def wait(self):
            return 1

        def poll(self):
            return 1

        def kill(self):
            pass

    monkeypatch.setattr(extract.subprocess, "Popen", WarningProc)

    assert read_member(tricky_zip, "a1.png", 1000, TOOL) == b"\x89PNG data"


@needs_bsdtar
def test_timeout_is_an_error_even_with_partial_output(tmp_path, monkeypatch):
    class SlowProc:
        def __init__(self, *args, **kwargs):
            import io
            self.stdout = io.BytesIO(b"partial")
            self.killed = False

        def wait(self):
            return -9

        def poll(self):
            return -9

        def kill(self):
            self.killed = True

    monkeypatch.setattr(extract.subprocess, "Popen", SlowProc)
    monkeypatch.setattr(extract.threading, "Timer", lambda t, fn: _FireNow(fn))

    with pytest.raises(ExtractError):
        read_member(tmp_path / "x.zip", "a.png", 1000, TOOL)


class _FireNow:
    def __init__(self, fn):
        self.fn = fn

    def start(self):
        self.fn()

    def cancel(self):
        pass
