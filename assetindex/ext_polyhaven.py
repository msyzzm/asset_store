"""Poly Haven (CC0 HDRIs, textures, models). Its API has no search: the list of every
asset (~2400, under 1 MB) is cached for 12 hours and matched here, by name, tags and
categories, most downloaded first.

HDRIs download as one file. Textures and models are sets of files: every map of one
resolution and format, or a model file with the files it includes (textures/...),
downloaded together into one folder."""

from urllib.parse import unquote, urlsplit

from assetindex import external

NAME = "polyhaven"
LABEL = "Poly Haven"
CREDIT = "https://polyhaven.com"
LICENSE = "CC0"
MATCHES_LIBRARY = True  # its ids are in its file names
API = "https://api.polyhaven.com"
CDN = "https://cdn.polyhaven.com/asset_img"
KINDS = {0: "HDRI", 1: "贴图", 2: "模型"}
MODEL_FORMATS = ("fbx", "gltf", "blend")
NOT_MAPS = frozenset({*MODEL_FORMATS, "mtlx", "usd", "hdri", "tonemapped", "backplates", "colorchart"})
MAP_FORMATS = ("jpg", "png")
HDRI_FORMATS = ("hdr", "exr")

_assets = external.TtlCache(12 * 3600)
_details = external.TtlCache(3600)


def _all() -> dict:
    return _assets.fetch("all", lambda: external.expect(external.get_json(f"{API}/assets"), dict, "素材列表"))


def _item(asset_id: str, asset: dict) -> dict:
    return {
        "source": NAME,
        "id": asset_id,
        "title": asset.get("name") or asset_id,
        "kind": KINDS.get(asset.get("type"), ""),
        "thumb": f"{CDN}/thumbs/{asset_id}.png?width=256&height=256",
        "page": f"https://polyhaven.com/a/{asset_id}",
        "tags": list(dict.fromkeys([*(asset.get("categories") or []), *(asset.get("tags") or [])]))[: external.MAX_TAGS],
    }


def _matches(asset_id: str, asset: dict, words: list[str]) -> bool:
    text = " ".join([asset_id, asset.get("name") or "", *(asset.get("tags") or []),
                     *(asset.get("categories") or [])]).lower()
    return all(w in text for w in words)


def search(q: str, offset: int = 0) -> dict:
    words = external.terms(q)
    found = sorted(
        ((k, a) for k, a in _all().items() if isinstance(a, dict) and _matches(k, a, words)),
        key=lambda pair: -(pair[1].get("download_count") or 0),
    )
    page = found[offset: offset + external.PAGE_SIZE]
    return {"items": [_item(k, a) for k, a in page], "total": len(found)}


def _res_key(res: str) -> int:
    digits = "".join(c for c in res if c.isdigit())
    return int(digits) if digits else 0


def _sized(entry: dict) -> int:
    return (entry.get("size") or 0) + sum(i.get("size") or 0 for i in (entry.get("include") or {}).values())


def _file_name(url: str) -> str:
    return unquote(urlsplit(url).path.rpartition("/")[2])


def _part(url: str, out: str, entry: dict) -> dict:
    return {"url": url, "out": out, "size": entry.get("size"), "md5": entry.get("md5")}


def _map_parts(files: dict, res: str, fmt: str) -> list[dict]:
    """Every map (diffuse, normal, roughness...) of one resolution and format."""
    maps = [v for k, v in files.items() if k not in NOT_MAPS and isinstance(v, dict)]
    entries = [(v.get(res) or {}).get(fmt) for v in maps]
    return [_part(e["url"], _file_name(e["url"]), e) for e in entries if isinstance(e, dict) and e.get("url")]


def _model_parts(entry: dict) -> list[dict]:
    """A model file and the files it includes, at the paths it expects them."""
    includes = entry.get("include") or {}
    return [_part(entry["url"], _file_name(entry["url"]), entry),
            *(_part(i["url"], out, i) for out, i in includes.items() if isinstance(i, dict) and i.get("url"))]


def _options(kind: int, files: dict) -> list[dict]:
    options = []
    if kind == 0:
        for res in sorted(files.get("hdri") or {}, key=_res_key):
            for fmt in HDRI_FORMATS:
                entry = files["hdri"][res].get(fmt)
                if entry:
                    options.append({"key": f"hdri/{res}/{fmt}", "label": f"{res.upper()} {fmt.upper()}",
                                    "size": entry.get("size"), "ready": True})
    elif kind == 1:
        maps = {k: v for k, v in files.items() if k not in NOT_MAPS and isinstance(v, dict)}
        for res in sorted({r for v in maps.values() for r in v}, key=_res_key):
            for fmt in MAP_FORMATS:
                parts = _map_parts(files, res, fmt)
                if parts:
                    options.append({"key": f"maps/{res}/{fmt}", "label": f"{res.upper()} {fmt.upper()} 全套贴图",
                                    "size": sum(p["size"] or 0 for p in parts) or None, "ready": True})
    else:
        for fmt in MODEL_FORMATS:
            for res in sorted(files.get(fmt) or {}, key=_res_key):
                entry = (files[fmt][res] or {}).get(fmt)
                if entry:
                    options.append({"key": f"{fmt}/{res}", "label": f"{fmt.upper()}（{res.upper()} 贴图）",
                                    "size": _sized(entry), "ready": True})
    return options


def _info_and_files(asset_id: str) -> tuple[dict, dict]:
    def load():
        return (external.expect(external.get_json(f"{API}/info/{asset_id}"), dict, "素材信息"),
                external.expect(external.get_json(f"{API}/files/{asset_id}"), dict, "文件列表"))

    return _details.fetch(asset_id, load)


def detail(asset_id: str) -> dict:
    info, files = _info_and_files(asset_id)
    return {
        **_item(asset_id, info),
        "previews": [f"{CDN}/primary/{asset_id}.png?height=780"],
        "description": info.get("description") or info.get("info") or "",
        "authors": "、".join(info.get("authors") or {}),
        "license": "CC0",
        "credit": CREDIT,
        "options": _options(info.get("type"), files),
    }


def option(asset_id: str, key: str) -> dict:
    """What option `key` downloads: {url, size, md5, label} for one file ("hdri/2k/hdr"),
    {parts, label} for several ("maps/1k/jpg", "fbx/2k"); KeyError if none."""
    info, files = _info_and_files(asset_id)
    label = next((o["label"] for o in _options(info.get("type"), files) if o["key"] == key), None)
    if label is None:
        raise KeyError(key)
    head, _, rest = key.partition("/")
    if head == "hdri":
        res, fmt = rest.split("/")
        entry = files["hdri"][res][fmt]
        return {"url": entry["url"], "size": entry.get("size"), "md5": entry.get("md5"), "label": label}
    if head == "maps":
        res, fmt = rest.split("/")
        return {"parts": _map_parts(files, res, fmt), "label": label}
    return {"parts": _model_parts(files[head][rest][head]), "label": label}
