"""OpenGameArt (2D and 3D art, music, sounds; licenses vary: CC0, CC-BY, GPL...). It has
no API: its search and asset pages are read as HTML. Its robots.txt asks for at most one
request every 10 seconds, so every page request passes one Gate, and answers are cached.

A download is one of the asset's files, or all of them, landing as a folder named after
the asset."""

import html
import re
from urllib.parse import unquote, urlencode, urlsplit

from assetindex import downloads, external
from assetindex.fetch_job import safe_name

NAME = "opengameart"
LABEL = "OpenGameArt"
CREDIT = "https://opengameart.org"
LICENSE = "授权各异"
MATCHES_LIBRARY = False  # its ids are page names ("trees"), not in its file names
BASE = "https://opengameart.org"
ALL = "all"
REQUEST_SECONDS = 10
MAX_WAIT_SECONDS = 25
MAX_DESCRIPTION = 2000
MAX_PREVIEWS = 6
_SLUG = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_TOTAL = re.compile(r"Displaying\s+[\d,]+\s*-\s*[\d,]+\s+of\s+([\d,]+)")
_TITLE = re.compile(r'<span class="art-preview-title"><a href="/content/([^"/?#]+)">(.*?)</a>', re.S)
_IMG = re.compile(r"<img src='([^']+)'")
_TAG = re.compile(r"<a\s[^>]*>")
_ATTR = re.compile(r'([\w-]+)="([^"]*)"')
_LINK_TEXT = re.compile(r"<a\s[^>]*>(.*?)</a>", re.S)
_LICENSE = re.compile(r"<div class='license-name'>(.*?)</div>", re.S)
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")

_gate = external.Gate(REQUEST_SECONDS, MAX_WAIT_SECONDS, LABEL)
_searches = external.TtlCache(600)
_details = external.TtlCache(3600)


def _page(url: str) -> str:
    _gate.wait()
    return external.get_text(url)


def _text(fragment: str) -> str:
    fragment = re.sub(r"</p>|<br\s*/?>", "\n", fragment)
    lines = (" ".join(html.unescape(line).split()) for line in re.sub(r"<[^>]+>", " ", fragment).split("\n"))
    return "\n".join(line for line in lines if line)


def _attrs(tag: str) -> dict:
    return {k: html.unescape(v) for k, v in _ATTR.findall(tag)}


def _item(slug: str, title: str, thumb: str | None, kind: str = "", tags: list[str] | None = None) -> dict:
    return {
        "source": NAME,
        "id": slug,
        "title": title or slug,
        "kind": kind,
        "thumb": thumb,
        "page": f"{BASE}/content/{slug}",
        "tags": (tags or [])[: external.MAX_TAGS],
    }


def _parse_search(text: str, start: int) -> dict:
    if "art-search-advanced" not in text:
        raise external.SourceError("网站返回的搜索结果看不懂（页面可能改了）")
    items = []
    for row in text.split("art-previews-inline")[1:]:
        found = _TITLE.search(row)
        if not found or not _SLUG.fullmatch(found[1]):
            continue
        img = _IMG.search(row)
        items.append(_item(found[1], _text(found[2]), external.web_url(html.unescape(img[1])) if img else None))
    found = _TOTAL.search(text)
    total = int(found[1].replace(",", "")) if found else 0
    if (items and not found) or (not items and total > start):  # results it says it has, but none read
        raise external.SourceError("网站返回的搜索结果看不懂（页面可能改了）")
    # the next page by the site's own paging, even if a row of this one was skipped
    return {"items": items, "total": total, "next": start + external.PAGE_SIZE}


def search(q: str, offset: int = 0) -> dict:
    page = offset // external.PAGE_SIZE
    params = {"keys": q, "sort_by": "count", "sort_order": "DESC", "page": page}
    start = page * external.PAGE_SIZE
    return _searches.fetch((q, page), lambda: _parse_search(_page(f"{BASE}/art-search-advanced?{urlencode(params)}"), start))


def _field(body: str, name: str) -> str:
    """The HTML of one field of the asset page, up to the next field."""
    start = body.find(f"field-name-{name} ")
    if start < 0:
        return ""
    start = body.find(">", start) + 1  # after the field's own tag
    ends = [body.find(mark, start) for mark in ('<div class="field field-name-', '<ul class="links')]
    return body[start: min([e for e in ends if e >= 0], default=len(body))]


def _out(name: str, fid: str, taken: set[str]) -> str:
    """The file's name in the download's folder: safe, and unique in it."""
    out = safe_name(name, int(fid))
    try:
        downloads.check_out(out)
    except ValueError:
        out = f"file-{fid}"
    if out.lower() in taken:
        out = f"{fid}-{out}"
    taken.add(out.lower())
    return out


def _files(field: str) -> list[dict]:
    files, taken = [], set()
    for tag in _TAG.findall(field):
        attrs = _attrs(tag)
        url, fid = external.web_url(attrs.get("href")), attrs.get("data-fid", "")
        if not url or not fid.isdigit():
            continue
        name = attrs.get("download") or unquote(urlsplit(url).path.rpartition("/")[2])
        length = re.search(r"length=(\d+)", attrs.get("type", ""))
        files.append({"fid": fid, "url": url, "name": name, "out": _out(name, fid, taken),
                      "size": int(length[1]) if length else None})
    return files


def _previews(field: str) -> list[str]:
    links = [external.web_url(_attrs(tag).get("href")) for tag in _TAG.findall(field)]
    return [u for u in links if u and urlsplit(u).path.lower().endswith(_IMAGE_EXT)][:MAX_PREVIEWS]


def _words(body: str, name: str) -> list[str]:
    return [_text(t) for t in _LINK_TEXT.findall(_field(body, name))]


def _parse_asset(slug: str, text: str) -> dict:
    body = text.split('id="comments"')[0]  # comments have files and pictures too
    title = _text(_field(body, "title"))
    if not title:
        raise external.SourceError("网站返回的素材页看不懂（页面可能改了）")
    previews = _previews(_field(body, "field-art-preview"))
    return {
        **_item(slug, title, previews[0] if previews else None, "、".join(_words(body, "field-art-type")),
                _words(body, "field-art-tags")),
        "previews": previews,
        "description": _text(_field(body, "body"))[:MAX_DESCRIPTION],
        "authors": "、".join(_words(body, "author-submitter")),
        "license": "、".join(_text(n) for n in _LICENSE.findall(_field(body, "field-art-licenses"))),
        "credit": f"{BASE}/content/{slug}",
        "files": _files(_field(body, "field-art-files")),
    }


def _asset(slug: str) -> dict:
    if not _SLUG.fullmatch(slug):
        raise KeyError(slug)
    return _details.fetch(slug, lambda: _parse_asset(slug, _page(f"{BASE}/content/{slug}")))


def _options(files: list[dict]) -> list[dict]:
    options = [{"key": f["fid"], "label": f["name"], "size": f["size"], "ready": True} for f in files]
    if len(files) > 1:
        sizes = [f["size"] for f in files]
        options.insert(0, {"key": ALL, "label": f"全部 {len(files)} 个文件", "ready": True,
                           "size": sum(sizes) if None not in sizes else None})
    return options


def detail(slug: str) -> dict:
    asset = _asset(slug)
    return {**{k: v for k, v in asset.items() if k != "files"}, "options": _options(asset["files"])}


def option(slug: str, key: str) -> dict:
    """{parts, label}: one file of the asset (key = its file id) or all ("all"); KeyError if none."""
    files = _asset(slug)["files"]
    if key == ALL and len(files) > 1:
        chosen, label = files, "全部文件"
    else:
        chosen = [f for f in files if f["fid"] == key]
        if not chosen:
            raise KeyError(key)
        label = chosen[0]["out"].rpartition(".")[0] or chosen[0]["out"]
    return {"parts": [{"url": f["url"], "out": f["out"], "size": f["size"], "md5": None} for f in chosen],
            "label": label}
