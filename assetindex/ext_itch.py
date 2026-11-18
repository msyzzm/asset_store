"""itch.io free game assets, by tag. Its robots.txt forbids its search and download pages,
so this lists the free assets of one tag ("pixel art" -> tag-pixel-art) from its RSS feed,
36 a page, most popular first, and links to each asset's page to download it by hand."""

import html
import re
import xml.etree.ElementTree as ET

from assetindex import external

NAME = "itch"
LABEL = "itch.io"
CREDIT = "https://itch.io/game-assets/free"
LICENSE = ""
MATCHES_LIBRARY = False  # its ids are page names, not in its file names
FEED = "https://itch.io/game-assets/free"
FEED_PAGE = 36
MAX_DESCRIPTION = 1000
NOTE = "itch.io 不让程序下载：点原页面，在那里下载（免费素材有时要先点 Download 或填 0 元），下好后放进素材库。"
_TAG = re.compile(r"[a-z0-9-]{1,60}")
_LINK = re.compile(r"https://([a-z0-9-]+)\.itch\.io/([a-z0-9_-]+)")
_ID = re.compile(r"([a-z0-9-]+)\.([a-z0-9_-]+)")

_feeds = external.TtlCache(600)
_seen = external.TtlCache(6 * 3600, max_items=5000)  # items listed lately, for their details


def tag(q: str) -> str:
    return "-".join(external.terms(q))


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment.split("<img")[0])).split())


def _item(entry: ET.Element) -> dict | None:
    found = _LINK.fullmatch((entry.findtext("link") or "").strip())
    if not found:
        return None
    user, slug = found[1], found[2]
    return {
        "source": NAME,
        "id": f"{user}.{slug}",
        "title": (entry.findtext("plainTitle") or entry.findtext("title") or slug).strip(),
        "kind": "",
        "thumb": external.web_url((entry.findtext("imageurl") or "").strip()),
        "page": found[0],
        "tags": [user],
        "description": _text(entry.findtext("description") or "")[:MAX_DESCRIPTION],
    }


def _parse(text: str) -> dict:
    try:
        entries = ET.fromstring(text).findall("./channel/item")
    except ET.ParseError:
        raise external.SourceError("网站返回的列表看不懂（接口可能改了）") from None
    items = [i for i in map(_item, entries) if i]
    return {"items": items, "full": len(entries) >= FEED_PAGE}


def search(q: str, offset: int = 0) -> dict:
    name = tag(q)
    if name and not _TAG.fullmatch(name):
        raise external.SourceError("itch.io 只能按英文标签找，比如 pixel art、3d、tileset")
    page = offset // FEED_PAGE + 1
    url = (f"{FEED}/tag-{name}.xml" if name else f"{FEED}.xml") + (f"?page={page}" if page > 1 else "")
    found = _feeds.fetch((name, page), lambda: _parse(external.get_text(url)))
    for item in found["items"]:
        _seen.put(item["id"], item)
    return {"items": found["items"], "total": None, "more": found["full"], "next": page * FEED_PAGE}


def detail(asset_id: str) -> dict:
    """What its feed said of it (no request: its pages are not read)."""
    item, _ = _seen.get(asset_id)
    if item is None:
        found = _ID.fullmatch(asset_id)
        if not found:
            raise KeyError(asset_id)
        item = {"source": NAME, "id": asset_id, "title": found[2], "kind": "", "thumb": None,
                "page": f"https://{found[1]}.itch.io/{found[2]}", "tags": [found[1]], "description": ""}
    return {**item, "tags": [], "previews": [item["thumb"]] if item["thumb"] else [], "authors": item["tags"][0],
            "license": LICENSE, "credit": CREDIT, "note": NOTE, "options": []}


def option(asset_id: str, key: str) -> dict:
    raise KeyError(key)
