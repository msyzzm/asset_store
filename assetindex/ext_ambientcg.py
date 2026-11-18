"""ambientCG (CC0 materials, HDRIs, models): its search API does the matching, and
every download is a ready-made zip."""

from urllib.parse import urlencode

from assetindex import external

NAME = "ambientcg"
LABEL = "ambientCG"
CREDIT = "https://ambientcg.com"
LICENSE = "CC0"
MATCHES_LIBRARY = True  # its ids are in its file names
BASE = "https://ambientcg.com/api/v2/full_json"
INCLUDE = "downloadData,previewData,tagData,displayData"
KINDS = {"Material": "材质", "HDRI": "HDRI", "3DModel": "模型", "Atlas": "贴图集", "Decal": "贴花", "Terrain": "地形"}

_searches = external.TtlCache(600)
_details = external.TtlCache(3600)


def _item(asset: dict) -> dict:
    asset_id = asset["assetId"]
    previews = asset.get("previewImage") or {}
    return {
        "source": NAME,
        "id": asset_id,
        "title": asset.get("displayName") or asset_id,
        "kind": KINDS.get(asset.get("dataType"), asset.get("dataType") or ""),
        "thumb": external.web_url(previews.get("256-PNG")),
        "page": external.web_url(asset.get("shortLink")) or f"https://ambientcg.com/view?id={asset_id}",
        "tags": [t for t in asset.get("tags") or [] if t.lower() != asset_id.lower()][: external.MAX_TAGS],
    }


def _zips(asset: dict) -> list[dict]:
    folders = (asset.get("downloadFolders") or {}).get("default") or {}
    zips = (folders.get("downloadFiletypeCategories") or {}).get("zip") or {}
    return zips.get("downloads") or []


def search(q: str, offset: int = 0) -> dict:
    params = {"q": q, "limit": external.PAGE_SIZE, "offset": offset, "include": INCLUDE, "sort": "popular"}

    def load():
        raw = external.expect(external.get_json(f"{BASE}?{urlencode(params)}"), dict, "搜索结果")
        external.expect(raw.get("foundAssets"), list, "搜索结果")
        return raw

    raw = _searches.fetch((q, offset), load)
    assets = [a for a in raw["foundAssets"] if isinstance(a, dict) and isinstance(a.get("assetId"), str)]
    return {"items": [_item(a) for a in assets], "total": int(raw.get("numberOfResults") or 0)}


def _asset(asset_id: str) -> dict:
    def load():
        raw = external.expect(external.get_json(f"{BASE}?{urlencode({'id': asset_id, 'include': INCLUDE})}"), dict, "素材信息")
        found = external.expect(raw.get("foundAssets") or [], list, "素材信息")
        if not found:
            raise KeyError(asset_id)
        return external.expect(found[0], dict, "素材信息")

    return _details.fetch(asset_id, load)


def detail(asset_id: str) -> dict:
    asset = _asset(asset_id)
    previews = asset.get("previewImage") or {}
    return {
        **_item(asset),
        "previews": [p for p in (external.web_url(previews.get("1024-PNG")),) if p],
        "description": asset.get("description") or asset.get("displayCategory") or "",
        "authors": "",
        "license": "CC0",
        "credit": CREDIT,
        "options": [
            {"key": z["attribute"], "label": z["attribute"], "size": z.get("size"), "ready": True}
            for z in _zips(asset)
        ],
    }


def option(asset_id: str, key: str) -> dict:
    """{url, size, md5, label} of the zip `key` (e.g. "2K-JPG"); KeyError if none."""
    for z in _zips(_asset(asset_id)):
        if z["attribute"] == key:
            return {"url": z["downloadLink"], "size": z.get("size"), "md5": None, "label": key}
    raise KeyError(key)
