"""Poly Pizza (low-poly 3D models, CC0 or CC-BY), through its API (v1.1) with the user's
key, which its file server wants too. So a download keeps "polypizza:<id>:glb" and the
fetcher adds the key when it starts the download (resolve()): the key never goes in the
queue. Every model is one GLB file; a CC-BY one comes with ready-made credit text."""

from urllib.parse import quote, urlencode

from assetindex import ext_keys, external
from assetindex.fetch_job import safe_name

NAME = "polypizza"
LABEL = "Poly Pizza"
CREDIT = "https://poly.pizza"
LICENSE = "CC0 或 CC-BY"
MATCHES_LIBRARY = False  # its ids are random
API = "https://api.poly.pizza/v1.1"
PAGE = 24  # its maximum is 32
CC0_ONLY = 1  # its License filter: 0 = CC-BY, 1 = CC0
# Its keyword search answers 500 for many words ("tree", "car") whenever their CC-BY
# models are in the results, but works with only the CC0 ones (seen 2026-11-18).
CC0_NOTICE = "Poly Pizza 的服务器搜这个词的 CC-BY 模型时出错，这里只列出了 CC0 模型。"
_ID_CHARS = frozenset("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")

_searches = external.TtlCache(600)
_details = external.TtlCache(3600)


def _headers() -> dict:
    key = ext_keys.get(NAME)
    if not key:
        raise external.NeedsKey("Poly Pizza 要 API key 才能搜：在「网站账号」里填上")
    return {"x-auth-token": key}


def _check_id(model_id: str) -> str:
    if not (0 < len(model_id) <= 20 and set(model_id) <= _ID_CHARS):
        raise KeyError(model_id)
    return model_id


def _item(model: dict) -> dict:
    model_id = model["ID"]
    tags = [t for t in model.get("Tags") or [] if isinstance(t, str)]
    category = model.get("Category")
    return {
        "source": NAME,
        "id": model_id,
        "title": (model.get("Title") or model_id).strip(),
        "kind": "动画模型" if model.get("Animated") else "模型",
        "thumb": external.web_url(model.get("Thumbnail")),
        "page": f"https://poly.pizza/m/{model_id}",
        "tags": ([category] if isinstance(category, str) else []) + tags[: external.MAX_TAGS - 1],
    }


def _models(raw) -> list[dict]:
    results = external.expect(external.expect(raw, dict, "搜索结果").get("results"), list, "搜索结果")
    return [m for m in results if isinstance(m, dict) and isinstance(m.get("ID"), str) and set(m["ID"]) <= _ID_CHARS]


def search(q: str, offset: int = 0) -> dict:
    page = offset // PAGE
    params = {"Limit": PAGE, "Page": page}
    path = f"{API}/search/{quote(q, safe='')}" if q else f"{API}/search"  # no keyword: it wants a filter
    headers = _headers()

    def ask(only_cc0: bool):
        return external.get_json(f"{path}?{urlencode({**params, 'License': CC0_ONLY} if only_cc0 else params)}",
                                 headers=headers)

    def load():
        notice = ""
        try:
            raw = ask(only_cc0=not q)
        except external.SiteBusy as err:
            if not (q and "HTTP 500" in str(err)):
                raise
            raw, notice = ask(only_cc0=True), CC0_NOTICE
        models = _models(raw)
        for m in models:
            if external.web_url(m.get("Download")):  # whole enough to stand for its detail
                _details.put(m["ID"], m)
        return {"items": [_item(m) for m in models], "total": int(raw.get("total") or 0), "next": (page + 1) * PAGE,
                "notice": notice}

    return _searches.fetch((q, page), load)


def _model(model_id: str) -> dict:
    _check_id(model_id)
    headers = _headers()

    def load():
        try:
            raw = external.get_json(f"{API}/model/{model_id}", headers=headers)
        except external.SourceError as err:
            if "HTTP 404" in str(err):
                raise KeyError(model_id) from None
            raise
        model = external.expect(raw, dict, "模型信息")
        if model.get("ID") != model_id:
            raise external.SourceError("网站返回的模型信息看不懂（接口可能改了）")
        return model

    return _details.fetch(model_id, load)


def _license(model: dict) -> str:
    return str(model.get("Licence") or "").strip()


def detail(model_id: str) -> dict:
    model = _model(model_id)
    creator = model.get("Creator") or {}
    tris = model.get("Tri Count")
    facts = " · ".join(x for x in (str(model.get("Category") or ""), f"{tris} 个三角面" if isinstance(tris, int) else "") if x)
    attribution = (model.get("Attribution") or "").strip()
    return {
        **_item(model),
        "previews": [p for p in (external.web_url(model.get("Thumbnail")),) if p],
        "description": "\n".join(x for x in ((model.get("Description") or "").strip(), facts) if x),
        "authors": (creator.get("Username") or "").strip() if isinstance(creator, dict) else "",
        "license": _license(model),
        "attribution": "" if _license(model).upper().startswith("CC0") else attribution,
        "credit": CREDIT,
        "options": [{"key": "glb", "label": "GLB（单个文件，带贴图）", "size": None, "ready": True}]
        if external.web_url(model.get("Download")) else [],
    }


def option(model_id: str, key: str) -> dict:
    """{url, size, md5, label, resolve}: the model's page stands for it in the queue; the
    fetcher gets the file with the key (resolve()). KeyError if no such option."""
    if key != "glb" or not detail(model_id)["options"]:
        raise KeyError(key)
    return {"url": f"https://poly.pizza/m/{model_id}", "size": None, "md5": None, "label": "GLB",
            "resolve": f"{NAME}:{model_id}:glb"}


def resolve(model_id: str, fmt: str, title: str) -> tuple[str, str, dict]:
    """The model's file link, the name to save it as, and the key its file server wants."""
    model = _model(model_id)
    url = external.web_url(model.get("Download"))
    if fmt != "glb" or not url:
        raise external.SourceError("Poly Pizza 没给这个模型的文件")
    return url, safe_name(f"{title}.glb", 0), _headers()
