"""Searching other sites' free assets (Poly Haven, ambientCG) from the web UI.

Each site has a module (ext_polyhaven.py, ext_ambientcg.py) with the same three calls:
search(q, offset) -> {"items", "total"}, detail(asset_id) -> dict, and option(asset_id,
key) -> what to download. They reach the sites through get_json() below, which tests
replace with recorded responses.

An item: {source, id, title, kind, thumb, page, tags}. A detail adds previews,
description, authors, license, credit, and options: [{key, label, size, ready}];
ready=False options need a download of several files, not supported yet.
"""

import json
import re
import threading
import time
import urllib.error
import urllib.request

from assetindex.fetch_job import USER_AGENT

TIMEOUT_SECONDS = 10
PAGE_SIZE = 24
MAX_TAGS = 8
_CJK = re.compile(r"[㐀-鿿豈-﫿]")


class SourceError(Exception):
    """The site did not answer, or answered with something unexpected."""


def get_json(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as err:
        raise SourceError(f"网站返回 HTTP {err.code}") from None
    except (OSError, ValueError) as err:
        raise SourceError(f"连不上网站：{err.__class__.__name__}") from None


def web_url(value) -> str | None:
    """A link from a site, kept only if it is http(s): never javascript: or the like."""
    return value if isinstance(value, str) and value.lower().startswith(("https://", "http://")) else None


def expect(value, kind: type, what: str):
    """`value` if it is a `kind`, else SourceError: an answer we cannot read is not cached."""
    if not isinstance(value, kind):
        raise SourceError(f"网站返回的{what}看不懂（接口可能改了）")
    return value


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
