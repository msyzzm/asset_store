"""Settings and HTTP client for an OpenAI-compatible /embeddings API (semantic search).

Settings are edited from the web UI and stored in the data dir, which the web server
and the scanner share; the scanner re-reads them every round. The API key is
write-only: it is never sent back to a browser.

Only text input is standardised by OpenAI's API. Servers that also embed images take
them in different shapes, so the shape is a setting:
- input_object: "input": [{"image": "data:image/jpeg;base64,..."}]   (Jina-style)
- input_string: "input": ["data:image/jpeg;base64,..."]
- messages:     "messages": [{"role": "user", "content": [{"type": "image_url", ...}]}]
                one image per request (vLLM-style)
"""

import base64
import http.client
import io
import ipaddress
import json
import logging
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

CONFIG_NAME = "embedding.json"
IMAGE_FORMATS = ("none", "input_object", "input_string", "messages")
MAX_BATCH = 256
MAX_MODEL_LENGTH = 200
MAX_URL_LENGTH = 500
MAX_KEY_LENGTH = 1000
KEY_CHARS = ("!", "~")  # printable ASCII without space: anything else can't go in a header
DEFAULT_TIMEOUT_SECONDS = 60  # per request, including reading the whole answer
ERROR_DETAIL_CHARS = 300
# The address is whatever someone typed in: bound what we read back from it.
MAX_RESPONSE_BYTES = 64 * 1024**2
MAX_ERROR_BYTES = 4096
MAX_DIMENSIONS = 8192
READ_CHUNK_BYTES = 64 * 1024
# Statuses that blame the input (bad image, too long), not the server or the settings.
ITEM_PROBLEM_STATUSES = frozenset({400, 413, 415, 422})


@dataclass(frozen=True)
class EmbedConfig:
    enabled: bool = False
    base_url: str = ""  # e.g. http://192.168.1.10:8000/v1; "/embeddings" is appended
    model: str = ""
    api_key: str = ""
    image_format: str = "none"
    batch_size: int = 16

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.base_url) and bool(self.model)

    @property
    def embeds_images(self) -> bool:
        return self.active and self.image_format != "none"


def _forbidden_host(host: str) -> bool:
    """Link-local (incl. cloud metadata at 169.254.169.254) and unspecified addresses:
    never an embedding server, only ever a way to poke at the machine's own network."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # a name, resolved at request time
    return ip.is_link_local or ip.is_unspecified or ip.is_multicast


def check_url(url: str) -> str:
    """A typed-in API base address, normalised; ValueError if it is not one."""
    url = url.strip().rstrip("/")
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url)
    try:
        parts.port  # noqa: B018 - raises on a non-numeric or out-of-range port
    except ValueError:
        parts = None
    if (
        parts is None
        or len(url) > MAX_URL_LENGTH
        or parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
        or "?" in url  # a query would swallow the "/embeddings" we append
        or "#" in url
        or any(c.isspace() or not c.isprintable() for c in url)
        or _forbidden_host(parts.hostname)
    ):
        raise ValueError("接口地址要是 http:// 或 https:// 开头的完整地址，例如 http://192.168.1.10:8000/v1")
    return url


_TYPES = {"enabled": bool, "base_url": str, "model": str, "api_key": str, "image_format": str, "batch_size": int}


def _check_types(update: dict) -> None:
    unknown = set(update) - {f.name for f in fields(EmbedConfig)}
    if unknown:
        raise ValueError(f"未知设置项：{', '.join(sorted(unknown))}")
    for name, value in update.items():
        if not isinstance(value, _TYPES[name]) or (_TYPES[name] is int and isinstance(value, bool)):
            raise ValueError(f"设置项 {name} 的类型不对")


def next_key(update: dict, stored: str, same_address: bool) -> str:
    """The API key after an update: an omitted key keeps the stored one only while the
    address stays the same; "" clears it. ValueError if it can't go in a header."""
    api_key = update.get("api_key")
    if api_key is None:
        api_key = stored if same_address else ""
    api_key = api_key.strip()
    if len(api_key) > MAX_KEY_LENGTH:
        raise ValueError("API key 太长")
    if not all(KEY_CHARS[0] <= c <= KEY_CHARS[1] for c in api_key):
        raise ValueError("API key 只能包含英文字母、数字和符号，不能有空格或换行")
    return api_key


def apply_update(current: EmbedConfig, update: dict) -> EmbedConfig:
    """New settings from a partial update. An omitted key keeps the stored one, "" clears
    it, and a new address without a new key clears it too, so whoever can open the
    settings page can't redirect the stored key to a host of their choosing."""
    _check_types(update)
    base_url = check_url(update.get("base_url", current.base_url))
    model = update.get("model", current.model).strip()
    if len(model) > MAX_MODEL_LENGTH:
        raise ValueError("模型名太长")
    image_format = update.get("image_format", current.image_format)
    if image_format not in IMAGE_FORMATS:
        raise ValueError(f"图片格式只能是 {', '.join(IMAGE_FORMATS)}")
    batch_size = update.get("batch_size", current.batch_size)
    if not 1 <= batch_size <= MAX_BATCH:
        raise ValueError(f"每批数量要在 1 到 {MAX_BATCH} 之间")
    api_key = next_key(update, current.api_key, base_url == current.base_url)

    config = EmbedConfig(
        enabled=update.get("enabled", current.enabled),
        base_url=base_url,
        model=model,
        api_key=api_key,
        image_format=image_format,
        batch_size=batch_size,
    )
    if config.enabled and not config.active:
        raise ValueError("启用前要填好接口地址和模型名")
    return config


def load_config(data_dir: Path) -> EmbedConfig:
    """Stored settings; defaults (off) if none or unreadable."""
    try:
        raw = json.loads((data_dir / CONFIG_NAME).read_text(encoding="utf-8"))
        return apply_update(EmbedConfig(), raw if isinstance(raw, dict) else {})
    except (OSError, ValueError):
        return EmbedConfig()


def save_private_json(data_dir: Path, name: str, data: dict) -> None:
    """Write settings that hold an API key: owner-only, and atomically."""
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / name
    tmp = target.with_name(f"{name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, target)


def save_config(data_dir: Path, config: EmbedConfig) -> None:
    save_private_json(data_dir, CONFIG_NAME, asdict(config))


def public_view(config: EmbedConfig) -> dict:
    view = asdict(config)
    del view["api_key"]
    return {**view, "has_key": bool(config.api_key), "active": config.active}


class EmbedError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def item_problem(self) -> bool:
        """The server rejected this input; other inputs may still work."""
        return self.status in ITEM_PROBLEM_STATUSES


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect would carry the Authorization header to wherever it points.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _data_url(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")


def _error_detail(raw: bytes) -> str:
    """The API's own error message, if it sent one the OpenAI way. Anything else (an
    unrelated service's error page) stays out of the browser; it goes to the log."""
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        message = error.get("message") if isinstance(error, dict) else error
        message = message if isinstance(message, str) else body.get("detail")
        if isinstance(message, str) and message:
            return message[:ERROR_DETAIL_CHARS]
    log.warning("embedding API error body: %r", raw[:ERROR_DETAIL_CHARS])
    return ""


def _read_limited(resp, limit: int, deadline: float) -> bytes:
    chunks, total = [], 0
    while chunk := resp.read(READ_CHUNK_BYTES):
        total += len(chunk)
        if total > limit:
            raise EmbedError("接口返回的数据太大")
        if time.monotonic() > deadline:
            raise EmbedError("接口响应太慢")
        chunks.append(chunk)
    return b"".join(chunks)


def _vectors(body, expected: int) -> list[list[float]]:
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or len(data) != expected:
        raise EmbedError(f"返回的向量数量不对：要 {expected} 个")
    try:
        ordered = sorted(data, key=lambda d: d["index"])
        vectors = [[float(x) for x in d["embedding"]] for d in ordered]
    except (KeyError, TypeError, ValueError):
        raise EmbedError("返回内容不是 OpenAI embeddings 格式") from None
    dims = {len(v) for v in vectors}
    if len(dims) != 1 or 0 in dims or max(dims) > MAX_DIMENSIONS:
        raise EmbedError(f"返回的向量为空、长度不一致或超过 {MAX_DIMENSIONS} 维")
    if not all(math.isfinite(x) for v in vectors for x in v):
        raise EmbedError("返回的向量为空、长度不一致或含非法数值")
    return vectors


def post_json(url: str, api_key: str, body: dict, timeout: float):
    """POST JSON to an OpenAI-compatible API and parse the answer. Every failure becomes
    an EmbedError whose text is safe to show (never the key)."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    deadline = time.monotonic() + timeout
    try:
        with _opener.open(request, timeout=timeout) as resp:
            raw = _read_limited(resp, MAX_RESPONSE_BYTES, deadline)
    except urllib.error.HTTPError as err:
        detail = _error_detail(err.read(MAX_ERROR_BYTES))
        raise EmbedError(f"HTTP {err.code}" + (f": {detail}" if detail else ""), err.code) from None
    except (urllib.error.URLError, OSError) as err:
        raise EmbedError(f"连不上接口：{getattr(err, 'reason', err)}") from None
    except (http.client.HTTPException, ValueError):
        # Broken or truncated responses; ValueError's text could quote the auth header.
        raise EmbedError("接口返回的内容不完整或不是 HTTP") from None
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise EmbedError("返回内容不是 JSON") from None


class EmbedClient:
    def __init__(self, config: EmbedConfig, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.config = config
        self.timeout = timeout

    def _post(self, body: dict, expected: int) -> list[list[float]]:
        parsed = post_json(self.config.base_url + "/embeddings", self.config.api_key, body, self.timeout)
        return _vectors(parsed, expected)

    def _batches(self, items: list) -> list[list]:
        size = self.config.batch_size
        return [items[i : i + size] for i in range(0, len(items), size)]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for batch in self._batches(texts):
            body = {"model": self.config.model, "input": batch, "encoding_format": "float"}
            vectors += self._post(body, len(batch))
        return vectors

    def embed_images(self, jpegs: list[bytes]) -> list[list[float]]:
        fmt = self.config.image_format
        if fmt == "none":
            raise EmbedError("设置里没有选图片格式")
        if fmt == "messages":
            return [self._post(self._message_body(jpeg), 1)[0] for jpeg in jpegs]
        vectors = []
        for batch in self._batches(jpegs):
            urls = [_data_url(j) for j in batch]
            items = [{"image": u} for u in urls] if fmt == "input_object" else urls
            vectors += self._post({"model": self.config.model, "input": items, "encoding_format": "float"}, len(batch))
        return vectors

    def _message_body(self, jpeg: bytes) -> dict:
        content = [{"type": "image_url", "image_url": {"url": _data_url(jpeg)}}]
        return {"model": self.config.model, "messages": [{"role": "user", "content": content}], "encoding_format": "float"}


PROBE_TEXT = "红色的火焰爆炸特效"
PROBE_TIMEOUT_SECONDS = 20


def _probe_jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (220, 60, 20)).save(buf, "JPEG")
    return buf.getvalue()


def probe(config: EmbedConfig) -> dict:
    """Try the API once with text (and an image if configured); report, never raise."""
    client = EmbedClient(config, timeout=PROBE_TIMEOUT_SECONDS)

    def attempt(call) -> dict:
        try:
            return {"ok": True, "dim": len(call()[0])}
        except EmbedError as err:
            return {"ok": False, "error": str(err)}

    return {
        "text": attempt(lambda: client.embed_texts([PROBE_TEXT])),
        "image": attempt(lambda: client.embed_images([_probe_jpeg()])) if config.image_format != "none" else None,
    }
