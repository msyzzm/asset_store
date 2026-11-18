"""Freesound (sound effects; licenses vary: CC0, CC-BY, CC-BY-NC). Its API needs the
user's API key even to search (set on the 外部素材 page). The original files need an
OAuth login, so the high-quality preview (MP3 ~128 kbps or OGG ~192 kbps, public links)
is what downloads; the original is a hand download from its page."""

from urllib.parse import urlencode

from assetindex import downloads, ext_keys, external
from assetindex.fetch_job import safe_name

NAME = "freesound"
LABEL = "Freesound"
CREDIT = "https://freesound.org"
LICENSE = "授权各异"
MATCHES_LIBRARY = False  # its ids are numbers
API = "https://freesound.org/apiv2"
FIELDS = "id,name,tags,description,username,license,previews,images,duration,filesize,type,url"
PREVIEWS = {"ogg": ("preview-hq-ogg", "试听版 OGG（约 192kbps）"), "mp3": ("preview-hq-mp3", "试听版 MP3（约 128kbps）")}
LICENSES = (  # a part of its license link: how to show it
    ("publicdomain/zero", "CC0"),
    ("licenses/by-nc", "CC-BY-NC（署名，不可商用）"),
    ("licenses/sampling+", "Sampling+"),
    ("licenses/by", "CC-BY（要署名）"),
)

_searches = external.TtlCache(600)
_details = external.TtlCache(3600)


def _key() -> dict:
    key = ext_keys.get(NAME)
    if not key:
        raise external.NeedsKey("Freesound 要 API key 才能搜：在「网站账号」里填上")
    return {"Authorization": f"Token {key}"}


def _license(link) -> str:
    link = link if isinstance(link, str) else ""
    return next((label for part, label in LICENSES if part in link.lower()), link)


def _seconds(value) -> str:
    return f"{value:.1f} 秒" if isinstance(value, (int, float)) else ""


def _item(sound: dict) -> dict:
    sound_id = str(sound["id"])
    images = sound.get("images") or {}
    return {
        "source": NAME,
        "id": sound_id,
        "title": (sound.get("name") or sound_id).strip(),
        "kind": "音效",
        "thumb": external.web_url(images.get("waveform_m")),
        "page": external.web_url(sound.get("url")) or f"https://freesound.org/s/{sound_id}/",
        "tags": [t for t in sound.get("tags") or [] if isinstance(t, str)][: external.MAX_TAGS],
    }


def _sounds(raw) -> list[dict]:
    results = external.expect(external.expect(raw, dict, "搜索结果").get("results"), list, "搜索结果")
    return [s for s in results if isinstance(s, dict) and type(s.get("id")) is int]


def search(q: str, offset: int = 0) -> dict:
    page = offset // external.PAGE_SIZE + 1
    params = {"query": q, "page": page, "page_size": external.PAGE_SIZE, "fields": FIELDS}
    headers = _key()

    def load():
        try:
            raw = external.get_json(f"{API}/search/?{urlencode(params)}", headers=headers)
        except external.SourceError as err:
            if "HTTP 404" in str(err):  # a page past the last
                return {"items": [], "total": 0}
            raise
        return {"items": [_item(s) for s in _sounds(raw)], "total": int(raw.get("count") or 0),
                "next": page * external.PAGE_SIZE}

    return _searches.fetch((q, page), load)


def _sound(sound_id: str) -> dict:
    if not sound_id.isdigit():
        raise KeyError(sound_id)
    headers = _key()

    def load():
        try:
            raw = external.get_json(f"{API}/sounds/{sound_id}/?{urlencode({'fields': FIELDS})}", headers=headers)
        except external.SourceError as err:
            if "HTTP 404" in str(err):
                raise KeyError(sound_id) from None
            raise
        sound = external.expect(raw, dict, "声音信息")
        if type(sound.get("id")) is not int:
            raise external.SourceError("网站返回的声音信息看不懂（接口可能改了）")
        return sound

    return _details.fetch(sound_id, load)


def _preview(sound: dict, key: str) -> str | None:
    return external.web_url((sound.get("previews") or {}).get(PREVIEWS[key][0])) if key in PREVIEWS else None


def detail(sound_id: str) -> dict:
    sound = _sound(sound_id)
    original = " ".join(x for x in (str(sound.get("type") or "").upper(), _seconds(sound.get("duration"))) if x)
    return {
        **_item(sound),
        "previews": [p for p in (external.web_url((sound.get("images") or {}).get("waveform_l")),) if p],
        "description": (sound.get("description") or "").strip()[:2000],
        "authors": sound.get("username") or "",
        "license": _license(sound.get("license")),
        "credit": CREDIT,
        "note": f"原始文件（{original}）要登录 Freesound 在原页面手动下载；这里下的是试听版。",
        "options": [{"key": k, "label": label, "size": None, "ready": True}
                    for k, (_, label) in PREVIEWS.items() if _preview(sound, k)],
    }


def option(sound_id: str, key: str) -> dict:
    """{parts, label}: the preview as a file named after the sound; KeyError if none."""
    sound = _sound(sound_id)
    url = _preview(sound, key)
    if not url:
        raise KeyError(key)
    name = (sound.get("name") or sound_id).strip()
    stem = name.rpartition(".")[0] if name.lower().endswith((".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg", ".m4a")) else name
    out = safe_name(f"{stem}.{key}", int(sound_id))
    try:
        downloads.check_out(out)
    except ValueError:
        out = f"sound-{sound_id}.{key}"
    return {"parts": [{"url": url, "out": out, "size": None, "md5": None}], "label": key.upper()}
