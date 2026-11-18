"""Openverse (images and audio under Creative Commons, gathered from Flickr, Wikimedia,
Jamendo and others), through its public API without an account: 20 requests a minute,
200 a day, 20 results a page. So an empty search asks nothing, answers are cached, and
an item's detail comes from the search that listed it.

It shows as two sources, IMAGES and AUDIO, with the calls the other site modules have.
A download is the original file from the site that hosts it, landing in a folder named
after the item."""

import html
from urllib.parse import urlencode, urlsplit

from assetindex import downloads, external
from assetindex.fetch_job import safe_name

API = "https://api.openverse.org/v1"
PAGE = 20  # the most an anonymous request gets
CREDIT = "https://openverse.org"
FILETYPES = {"mp32": "mp3", "jpeg": "jpg"}  # its file types that are not extensions
_ID_CHARS = frozenset("0123456789abcdef-")


def _license(item: dict) -> str:
    code = str(item.get("license") or "").lower()
    if code in ("cc0", "pdm"):
        return "CC0" if code == "cc0" else "公有领域"
    text = f"CC {code.upper()} {item.get('license_version') or ''}".strip()
    return f"{text}（不可商用）" if "nc" in code.split("-") else text


def _extension(item: dict, url: str) -> str:
    filetype = str(item.get("filetype") or "").lower()
    if filetype.isalnum():
        return FILETYPES.get(filetype, filetype)
    name = urlsplit(url).path.rpartition("/")[2]
    suffix = name.rpartition(".")[2].lower() if "." in name else ""
    return FILETYPES.get(suffix, suffix) if suffix.isalnum() and len(suffix) <= 5 else ""


class Kind:
    """One of Openverse's media types, as a source of the 外部素材 page."""

    LICENSE = "授权各异"
    MATCHES_LIBRARY = False  # its ids are random
    CREDIT = CREDIT

    def __init__(self, media: str, name: str, label: str, kind: str) -> None:
        self.media, self.NAME, self.LABEL, self.kind = media, name, label, kind
        self._searches = external.TtlCache(600)
        self._seen = external.TtlCache(6 * 3600, max_items=5000)  # items listed lately, for their details

    def _item(self, raw: dict) -> dict:
        item_id = raw["id"]
        return {
            "source": self.NAME,
            "id": item_id,
            "title": html.unescape(raw.get("title") or item_id).strip(),  # some come with &amp;
            "kind": self.kind,
            "thumb": external.web_url(raw.get("thumbnail")),
            "page": external.web_url(raw.get("foreign_landing_url")) or f"https://openverse.org/{self.media}/{item_id}",
            "tags": [t["name"] for t in raw.get("tags") or [] if isinstance(t, dict) and isinstance(t.get("name"), str)]
            [: external.MAX_TAGS],
        }

    def _get(self, url: str):
        try:
            return external.get_json(url)
        except external.SiteBusy as err:
            if "429" in str(err):
                raise external.SiteBusy("Openverse 不登录每分钟只能搜 20 次、每天 200 次，用完了：等一会儿再试") from None
            raise

    def search(self, q: str, offset: int = 0) -> dict:
        if not q:  # every request counts against a small daily allowance
            raise external.SourceError("输入关键词才搜（不登录每天只能请求 200 次）")
        page = offset // PAGE + 1
        url = f"{API}/{self.media}/?{urlencode({'q': q, 'page': page, 'page_size': PAGE})}"

        def load():
            try:
                raw = external.expect(self._get(url), dict, "搜索结果")
            except external.SourceError as err:
                if page > 1 and "HTTP 400" in str(err):  # a page past the last it serves
                    return {"total": 0, "raw": []}
                raise
            found = [r for r in external.expect(raw.get("results"), list, "搜索结果")
                     if isinstance(r, dict) and isinstance(r.get("id"), str) and set(r["id"]) <= _ID_CHARS]
            return {"raw": found, "total": int(raw.get("result_count") or 0)}

        answer = self._searches.fetch((q, page), load)
        for raw in answer["raw"]:
            self._seen.put(raw["id"], raw)
        return {"items": [self._item(r) for r in answer["raw"]], "total": answer["total"], "next": page * PAGE}

    def _raw(self, item_id: str) -> dict:
        if not (len(item_id) == 36 and set(item_id) <= _ID_CHARS):
            raise KeyError(item_id)
        raw, fresh = self._seen.get(item_id)
        if raw is None or not fresh:
            try:
                raw = external.expect(self._get(f"{API}/{self.media}/{item_id}/"), dict, "素材信息")
            except external.SourceError as err:
                if "HTTP 404" in str(err):
                    raise KeyError(item_id) from None
                raise
            if raw.get("id") != item_id:
                raise external.SourceError("网站返回的素材信息看不懂（接口可能改了）")
            self._seen.put(item_id, raw)
        return raw

    def detail(self, item_id: str) -> dict:
        raw = self._raw(item_id)
        url = external.web_url(raw.get("url"))
        ext = _extension(raw, url or "")
        facts = []
        if isinstance(raw.get("width"), int) and isinstance(raw.get("height"), int):
            facts.append(f"{raw['width']}×{raw['height']}")
        if isinstance(raw.get("duration"), int):
            facts.append(f"{raw['duration'] / 1000:.1f} 秒")
        facts.append(f"来自 {raw.get('source') or raw.get('provider') or '?'}")
        return {
            **self._item(raw),
            "previews": [p for p in (external.web_url(raw.get("thumbnail")),) if p],
            "description": " · ".join(facts),
            "authors": (raw.get("creator") or "").strip(),
            "license": _license(raw),
            "attribution": "" if str(raw.get("license")).lower() in ("cc0", "pdm") else (raw.get("attribution") or "").strip(),
            "credit": CREDIT,
            "note": "文件从它原来所在的网站下载，个别网站可能拒绝程序下载，那就点原页面手动下。",
            "options": [{"key": "original", "label": f"原文件{f'（{ext.upper()}）' if ext else ''}",
                         "size": raw.get("filesize") if type(raw.get("filesize")) is int else None, "ready": True}]
            if url else [],
        }

    def option(self, item_id: str, key: str) -> dict:
        """{parts, label}: the original file, named after the item; KeyError if none."""
        raw = self._raw(item_id)
        url = external.web_url(raw.get("url"))
        if key != "original" or not url:
            raise KeyError(key)
        ext = _extension(raw, url)
        out = safe_name(f"{self._item(raw)['title']}{'.' + ext if ext else ''}", 0)
        try:
            downloads.check_out(out)
        except ValueError:
            out = f"{item_id}{'.' + ext if ext else ''}"
        size = raw.get("filesize") if type(raw.get("filesize")) is int else None
        return {"parts": [{"url": url, "out": out, "size": size, "md5": None}], "label": "原文件"}


IMAGES = Kind("images", "openverse_images", "Openverse 图片", "图片")
AUDIO = Kind("audio", "openverse_audio", "Openverse 音频", "音频")
