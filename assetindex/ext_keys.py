"""The keys of sites that need one (a Sketchfab API token, a Freesound API key), set on
the 外部素材 page and kept owner-only in the data dir (external.keys). The web server
reads them to search; the fetcher reads the Sketchfab token to ask for download links.
The page only ever learns whether a key is set."""

import json
from pathlib import Path

from assetindex.embedding import KEY_CHARS, MAX_KEY_LENGTH, save_private_json

FILE_NAME = "external.keys"
SITES = ("sketchfab", "freesound")

_data_dir: Path | None = None


def use(data_dir: Path) -> None:
    """Where the keys are kept (set once by the web server and the fetcher)."""
    global _data_dir
    _data_dir = data_dir


def _load() -> dict[str, str]:
    if _data_dir is None:
        return {}
    try:
        raw = json.loads((_data_dir / FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in raw.items() if k in SITES and isinstance(v, str)} if isinstance(raw, dict) else {}


def get(site: str) -> str:
    return _load().get(site, "")


def status() -> dict[str, bool]:
    keys = _load()
    return {site: bool(keys.get(site)) for site in SITES}


def save(update: dict) -> dict[str, bool]:
    """Set the keys given ("" clears one); ValueError if one is unusable."""
    if not isinstance(update, dict) or set(update) - set(SITES):
        raise ValueError(f"只能设置这些网站：{', '.join(SITES)}")
    keys = _load()
    for site, key in update.items():
        if not isinstance(key, str):
            raise ValueError("key 要是文字")
        key = key.strip()
        if len(key) > MAX_KEY_LENGTH or not all(KEY_CHARS[0] <= c <= KEY_CHARS[1] for c in key):
            raise ValueError("key 只能包含英文字母、数字和符号，不能有空格或换行")
        keys[site] = key
    if _data_dir is None:
        raise ValueError("服务器没设置数据目录")
    save_private_json(_data_dir, FILE_NAME, {k: v for k, v in keys.items() if v})
    return status()
