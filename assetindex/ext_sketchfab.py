"""Sketchfab (3D models; licenses vary: CC0, CC-BY, CC-BY-NC...). Its search is public;
a download needs the user's API token (set on the 外部素材 page) and gives a link that
expires within minutes, so the queue keeps "sketchfab:<uid>:<format>" and the fetcher
asks for a fresh link each time it hands the download to aria2 (resolve())."""

from urllib.parse import urlencode, urlsplit

from assetindex import ext_keys, external
from assetindex.fetch_job import safe_name

NAME = "sketchfab"
LABEL = "Sketchfab"
CREDIT = "https://sketchfab.com"
LICENSE = "授权各异"
MATCHES_LIBRARY = False  # its ids are random, not in file names
API = "https://api.sketchfab.com/v3"
FORMATS = {  # format: (label, extension if its link has none)
    "glb": ("GLB（单个文件，带贴图）", ".glb"),
    "gltf": ("glTF（zip）", ".zip"),
    "usdz": ("USDZ", ".usdz"),
    "source": ("原始文件（作者上传的格式）", ".zip"),
}
THUMB_WIDTH = 256
PREVIEW_WIDTH = 1024
_UID_CHARS = frozenset("0123456789abcdef")

_searches = external.TtlCache(600)
_details = external.TtlCache(3600)
_archives = external.TtlCache(6 * 3600, max_items=5000)  # each model's formats and sizes, as searches list them


def _check_uid(uid: str) -> str:
    if not (len(uid) == 32 and set(uid) <= _UID_CHARS):
        raise KeyError(uid)
    return uid


def _picture(model: dict, width: int) -> str | None:
    images = [i for i in ((model.get("thumbnails") or {}).get("images") or []) if isinstance(i, dict)]
    fits = sorted((i for i in images if (i.get("width") or 0) >= width), key=lambda i: i.get("width") or 0)
    best = fits[0] if fits else max(images, key=lambda i: i.get("width") or 0, default={})
    return external.web_url(best.get("url"))


def _license(model: dict) -> str:
    return ((model.get("license") or {}).get("label") or "").strip()


def _item(model: dict) -> dict:
    uid = model["uid"]
    return {
        "source": NAME,
        "id": uid,
        "title": (model.get("name") or uid).strip(),
        "kind": "模型",
        "thumb": _picture(model, THUMB_WIDTH),
        "page": external.web_url(model.get("viewerUrl")) or f"https://sketchfab.com/3d-models/{uid}",
        "tags": [t["name"] for t in model.get("tags") or [] if isinstance(t, dict) and isinstance(t.get("name"), str)]
        [: external.MAX_TAGS],
    }


def _parse_search(raw, offset: int) -> dict:
    raw = external.expect(raw, dict, "搜索结果")
    models = [m for m in external.expect(raw.get("results"), list, "搜索结果")
              if isinstance(m, dict) and isinstance(m.get("uid"), str) and not m.get("isAgeRestricted")]
    for m in models:
        if isinstance(m.get("archives"), dict):
            _archives.put(m["uid"], m["archives"])
    return {"items": [_item(m) for m in models], "total": None, "more": bool(raw.get("next")),
            "next": offset + external.PAGE_SIZE}


def search(q: str, offset: int = 0) -> dict:
    params = {"type": "models", "q": q, "downloadable": "true", "count": external.PAGE_SIZE, "cursor": offset}
    return _searches.fetch((q, offset), lambda: _parse_search(external.get_json(f"{API}/search?{urlencode(params)}"), offset))


def _model(uid: str) -> dict:
    def load():
        try:
            return external.expect(external.get_json(f"{API}/models/{uid}"), dict, "模型信息")
        except external.SourceError as err:
            if "HTTP 404" in str(err):
                raise KeyError(uid) from None
            raise

    return _details.fetch(_check_uid(uid), load)


def _options(archives: dict | None) -> list[dict]:
    if not isinstance(archives, dict):  # not seen in a search lately: the usual formats, sizes unknown
        archives = {f: {} for f in ("glb", "gltf", "source")}
    return [{"key": f, "label": label, "size": (archives[f] or {}).get("size"), "ready": True}
            for f, (label, _) in FORMATS.items() if f in archives]


def detail(uid: str) -> dict:
    model = _model(uid)
    user = model.get("user") or {}
    options = _options(_archives.get(uid)[0]) if model.get("isDownloadable") else []
    return {
        **_item(model),
        "previews": [p for p in (_picture(model, PREVIEW_WIDTH),) if p],
        "description": (model.get("description") or "").strip()[:2000],
        "authors": (user.get("displayName") or user.get("username") or "").strip(),
        "license": _license(model),
        "credit": CREDIT,
        "note": "" if ext_keys.get(NAME) or not options else "下载要先在「网站账号」里填 Sketchfab 的 API token。",
        "options": options,
    }


def option(uid: str, key: str) -> dict:
    """{url, size, md5, label, resolve}: the queue keeps the model's page as its link and
    `resolve` for the fetcher to get the real one; KeyError if no such format."""
    if key not in FORMATS:
        raise KeyError(key)
    if not ext_keys.get(NAME):
        raise external.NeedsKey("下载 Sketchfab 的模型要先在「网站账号」里填 API token")
    found = next((o for o in detail(uid)["options"] if o["key"] == key), None)
    if found is None:
        raise KeyError(key)
    return {"url": _item(_model(uid))["page"], "size": found["size"], "md5": None, "label": key.upper(),
            "resolve": f"{NAME}:{uid}:{key}"}


def resolve(uid: str, fmt: str, title: str) -> tuple[str, str]:
    """A fresh download link of the model in `fmt`, and the file name to save it as."""
    token = ext_keys.get(NAME)
    if not token:
        raise external.NeedsKey("没填 Sketchfab 的 API token（「外部素材」页的「网站账号」）")
    raw = external.expect(external.get_json(f"{API}/models/{_check_uid(uid)}/download",
                                            headers={"Authorization": f"Token {token}"}), dict, "下载信息")
    url = external.web_url((raw.get(fmt) or {}).get("url"))
    if not url:
        raise external.SourceError(f"Sketchfab 没给这个模型的 {fmt} 格式")
    path = urlsplit(url).path.rpartition("/")[2]
    suffix = path.rpartition(".")[2].lower() if "." in path else ""
    ext = f".{suffix}" if suffix.isalnum() and len(suffix) <= 6 else FORMATS[fmt][1]
    return url, safe_name(f"{title}{ext}", 0)
