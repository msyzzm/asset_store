"""Searching other sites' free assets (Poly Haven, ambientCG, OpenGameArt, itch.io) from
the web UI.

Each site has a module (ext_polyhaven.py, ...) with the same three calls: search(q,
offset) -> {"items", "total", "more"?}, detail(asset_id) -> dict, and option(asset_id,
key) -> what to download. They reach the sites through get_json() / get_text() below,
which tests replace with recorded responses.

An item: {source, id, title, kind, thumb, page, tags}. A detail adds previews,
description, authors, license, credit, note?, and options: [{key, label, size, ready}].
A search result with total None does not know its total; `notice`, if given, says what
its results leave out; `more` says if another page may follow; `next`, if given, is the offset to ask for it (rows a site gave but that were
skipped still count).
"""

import json
import re
import threading
import time
import urllib.error
import urllib.request

from assetindex.fetch_job import USER_AGENT

TIMEOUT_SECONDS = 10
MAX_PAGE_BYTES = 4 * 1024 * 1024
PAGE_SIZE = 24
MAX_TAGS = 8
_CJK = re.compile(r"[㐀-鿿豈-﫿]")


class SourceError(Exception):
    """The site did not answer, or answered with something unexpected."""

    transient = False  # worth asking again in a while (unreachable, busy)


class SiteBusy(SourceError):
    """The site is unreachable or busy (timeouts, 429, 5xx) for now."""

    transient = True


class NeedsKey(SourceError):
    """The site needs a key (an API token) that is not set on the 外部素材 page."""


def get_json(url: str, headers: dict | None = None):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as err:
        if err.code in (401, 403) and headers:
            raise SourceError(f"网站拒绝了（HTTP {err.code}）：key 不对（到「网站账号」里检查），或这个素材不让下载") from None
        if err.code == 429 or err.code >= 500:
            raise SiteBusy(f"网站忙（HTTP {err.code}）") from None
        raise SourceError(f"网站返回 HTTP {err.code}") from None
    except OSError as err:
        raise SiteBusy(f"连不上网站：{err.__class__.__name__}") from None
    except ValueError as err:
        raise SourceError(f"网站返回的内容看不懂：{err.__class__.__name__}") from None


def get_text(url: str) -> str:
    """A web page or feed (OpenGameArt, itch.io: no JSON API)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read(MAX_PAGE_BYTES).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as err:
        raise SourceError(f"网站返回 HTTP {err.code}") from None
    except OSError as err:
        raise SourceError(f"连不上网站：{err.__class__.__name__}") from None


class Gate:
    """At most one request every `seconds` to a site (OpenGameArt asks for 10). A caller
    who would wait longer than `max_wait` gets a SourceError instead of hanging."""

    def __init__(self, seconds: float, max_wait: float, label: str) -> None:
        self.seconds, self.max_wait, self.label = seconds, max_wait, label
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            if start - now > self.max_wait:
                raise SourceError(f"{self.label} 要求每 {self.seconds:g} 秒最多请求一次，等一会儿再试")
            self._next = start + self.seconds
        time.sleep(start - now)


def web_url(value) -> str | None:
    """A link from a site, kept only if it is http(s): never javascript: or the like."""
    return value if isinstance(value, str) and value.lower().startswith(("https://", "http://")) else None


def expect(value, kind: type, what: str):
    """`value` if it is a `kind`, else SourceError: an answer we cannot read is not cached."""
    if not isinstance(value, kind):
        raise SourceError(f"网站返回的{what}看不懂（接口可能改了）")
    return value


AUDIO_EXTENSIONS = (".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg", ".m4a")


def without_audio_extension(name: str) -> str:
    """A sound's name without the extension many carry ("DoorCreaking11.flac")."""
    stem = name.rpartition(".")[0].strip() if name.lower().endswith(AUDIO_EXTENSIONS) else name
    return stem or name


def has_cjk(text: str) -> bool:
    return bool(_CJK.search(text))


def terms(q: str) -> list[str]:
    return [t for t in q.lower().split() if t]


class TtlCache:
    """A small thread-safe cache. get() returns (value, fresh); a stale value is kept to
    fall back on when the site is down."""

    def __init__(self, seconds: float, max_items: int = 256) -> None:
        self.seconds, self.max_items = seconds, max_items
        self._items: dict = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._items.get(key)
        if hit is None:
            return None, False
        stored, value = hit
        return value, time.monotonic() - stored < self.seconds

    def put(self, key, value) -> None:
        with self._lock:
            if len(self._items) >= self.max_items and key not in self._items:
                self._items.pop(next(iter(self._items)))  # the oldest
            self._items[key] = (time.monotonic(), value)

    def fetch(self, key, load):
        """The cached value if fresh, else load() it; the stale one if loading fails."""
        value, fresh = self.get(key)
        if fresh:
            return value
        try:
            value = load()
        except SourceError:
            if value is not None:
                self.put(key, value)  # the site is down: the old answer serves a while, no wait each time
                return value
            raise
        self.put(key, value)
        return value
