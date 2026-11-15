from pathlib import Path

import pytest

from assetindex.config import parse_roots


def test_parse_roots_handles_windows_and_posix_paths():
    roots = parse_roots(r"local=E:\ArtAssets;nas=/assets")
    assert roots == {"local": Path(r"E:\ArtAssets"), "nas": Path("/assets")}


def test_parse_roots_rejects_entry_without_name():
    with pytest.raises(ValueError):
        parse_roots("/assets")


def test_parse_roots_rejects_empty_spec():
    with pytest.raises(ValueError):
        parse_roots("  ")


def test_thumb_cache_cap_from_env(monkeypatch):
    from assetindex.config import load_settings

    monkeypatch.setenv("ASSET_ROOTS", "art=/assets")
    monkeypatch.setenv("ASSET_THUMB_CACHE_MB", "10")

    assert load_settings().thumb_cache_bytes == 10 * 1024**2


def test_thumb_cache_cap_must_be_a_number(monkeypatch):
    from assetindex.config import load_settings

    monkeypatch.setenv("ASSET_ROOTS", "art=/assets")
    monkeypatch.setenv("ASSET_THUMB_CACHE_MB", "2GB")

    with pytest.raises(ValueError):
        load_settings()
