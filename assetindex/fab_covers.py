"""Covers from Fab (the Unreal marketplace) for UE packs with no picture of their own.

After a scan, each such pack's name is searched on fab.com; a listing is taken only when
it is the one result whose title is the name (letters and digits, case ignored) and it
is an Unreal Engine product: with no way to correct a wrong match, an ambiguous name
gets nothing. Results are kept by name, so moving or renaming a pack keeps its cover.
Only a well-formed answer is recorded: a network error, a refusal or a changed reply
stops this step and the next scan tries again.

The search is the endpoint fab.com's own pages use, not a published API: it may change.
"""

import io
import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from assetindex.thumbs import open_image, save_webp

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.fab.com/i/listings/search"
MEDIA_HOST = "media.fab.com"
USER_AGENT = "Mozilla/5.0 (compatible; asset-library)"
TIMEOUT_SECONDS = 20
REQUEST_GAP_SECONDS = 1.0
MAX_PER_SCAN = 100
MAX_IMAGE_BYTES = 8 * 1024**2
IMAGE_WIDTH = 640  # the smallest preview at least this wide (they come 144 to 1280 wide)
COVER_BOX = (640, 640)
UNREAL_FORMAT = "unreal-engine"
COVER_DIR = "fab_covers"
STATUS_NAME = "fab.status"
_ARCHIVE_EXT = re.compile(r"\.(zip|rar|7z|unitypackage)$", re.IGNORECASE)
_LISTING_ID = re.compile(r"^[0-9a-f-]{36}$")
_COVER_FILE = re.compile(r"^[0-9a-f-]{36}\.webp$")


class FabError(Exception):
    """Fab could not be asked, or answered something unexpected: try again next scan."""


@dataclass
class FabStats:
    checked: int = 0
    matched: int = 0
    error: str | None = None


def name_of(path: str) -> str:
    """The pack's own name, before the " - 中文说明" the organizer adds."""
    own = path.rpartition("/")[2].split(" - ")[0]
    return _ARCHIVE_EXT.sub("", own).strip()


def key_of(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def query_of(name: str) -> str:
    """"SwordAnimsetPro" and "Crystal_cave" as words: the search matches words."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).replace("_", " ").strip()


def is_cover_file(name: str) -> bool:
    return bool(_COVER_FILE.match(name))


def cover_url(conn: sqlite3.Connection, path: str) -> str | None:
    row = conn.execute(
        "SELECT image FROM fab_matches WHERE name_key = ? AND status = 'matched'", (key_of(name_of(path)),)
    ).fetchone()
    return f"/api/fab-covers/{row[0]}" if row and row[0] else None


def _get(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            data = response.read(limit + 1)
    except (urllib.error.URLError, OSError) as err:  # HTTPError (403, 429...) is a URLError
        raise FabError(f"{urllib.parse.urlsplit(url).netloc}: {err}") from None
    if len(data) > limit:
        raise FabError(f"answer over {limit} bytes")
    return data


def search(query: str) -> list[dict]:
    raw = _get(f"{SEARCH_URL}?{urllib.parse.urlencode({'q': query})}", MAX_IMAGE_BYTES)
    try:
        results = json.loads(raw)["results"]
    except (ValueError, KeyError, TypeError):
        raise FabError("search answered something other than its usual JSON") from None
    if not isinstance(results, list):
        raise FabError("search results are not a list")
    return results


def download(url: str) -> bytes:
    if urllib.parse.urlsplit(url).netloc != MEDIA_HOST:
        raise FabError(f"image from an unexpected host: {url}")
    return _get(url, MAX_IMAGE_BYTES)


def pick(results: list[dict], name: str) -> dict | None:
    """The one Unreal listing titled exactly `name`; None if there is none, or several."""
    same = [r for r in results if isinstance(r, dict) and key_of(str(r.get("title", ""))) == key_of(name)]
    if len(same) != 1:
        return None
    formats = {(f.get("assetFormatType") or {}).get("code") for f in same[0].get("assetFormats") or [] if isinstance(f, dict)}
    return same[0] if UNREAL_FORMAT in formats and _LISTING_ID.match(str(same[0].get("uid", ""))) else None


def image_url(listing: dict) -> str | None:
    try:
        images = [i for i in listing["thumbnails"][0]["images"] if i.get("url") and i.get("width")]
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    if not images:
        return None
    wide = [i for i in images if i["width"] >= IMAGE_WIDTH]
    chosen = min(wide, key=lambda i: i["width"]) if wide else max(images, key=lambda i: i["width"])
    return chosen["url"]


def _record(conn: sqlite3.Connection, name: str, listing: dict | None, image: str | None) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO fab_matches (name_key, query, status, listing, title, image, checked)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key_of(name), name, "matched" if image else "none", listing and listing.get("uid"),
             listing and listing.get("title"), image, time.time()),
        )


def _cover(data_dir: Path, listing: dict, fetch: Callable[[str], bytes]) -> str | None:
    url = image_url(listing)
    if url is None:
        return None
    try:
        picture = open_image(io.BytesIO(fetch(url)), COVER_BOX)
    except FabError:
        raise
    except Exception as err:  # Pillow raises many types on a malformed image
        raise FabError(f"cannot decode the cover of {listing.get('uid')}: {err!r}") from None
    name = f"{listing['uid']}.webp"
    save_webp(picture, data_dir / COVER_DIR / name)
    return name


def match_pending(
    conn: sqlite3.Connection,
    data_dir: Path,
    names: list[str],
    should_stop: Callable[[], bool] = lambda: False,
    find: Callable[[str], list[dict]] = search,
    fetch: Callable[[str], bytes] = download,
    sleep: Callable[[float], None] = time.sleep,
) -> FabStats:
    """Search the names not looked up yet (MAX_PER_SCAN at most, a second apart)."""
    known = {r[0] for r in conn.execute("SELECT name_key FROM fab_matches")}
    todo: dict[str, str] = {}
    for name in names:
        key = key_of(name)
        if key and key not in known and key not in todo:
            todo[key] = name
    stats = FabStats()
    for name in list(todo.values())[:MAX_PER_SCAN]:
        if should_stop():
            break
        if stats.checked:
            sleep(REQUEST_GAP_SECONDS)
        try:
            listing = pick(find(query_of(name)), name)
            image = _cover(data_dir, listing, fetch) if listing else None
        except FabError as err:
            stats.error = str(err)
            break
        _record(conn, name, listing, image)
        stats.checked += 1
        stats.matched += image is not None
    _write_status(data_dir, stats)
    return stats


def _write_status(data_dir: Path, stats: FabStats) -> None:
    status = {"checked": stats.checked, "matched": stats.matched, "error": stats.error, "finished": time.time()}
    try:
        (data_dir / STATUS_NAME).write_text(json.dumps(status), encoding="utf-8")
    except OSError as err:  # only for the web page to show
        log.warning("cannot write %s: %r", STATUS_NAME, err)


def read_status(data_dir: Path) -> dict:
    """The last run, for checking a deployment reaches Fab at all."""
    try:
        return json.loads((data_dir / STATUS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
