"""Build archive previews at scan time: one contact sheet per archive, plus the per-asset
previews Unity bundles inside a .unitypackage.

State per archive (`archives.preview_state`): NULL = not attempted yet (or retry: tool
missing, archive unreadable), 0 = nothing previewable, 1 = sheet built. A preview failure
never touches the listing itself.
"""

import io
import logging
import re
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from assetindex import attempts
from assetindex.archives import ArchiveError, read_unity_previews
from assetindex.categories import DUPLICATES, TO_DELETE
from assetindex.extract import ExtractError, ExtractTimeout, read_member
from assetindex.scan_request import ScanStopped, never, no_progress
from assetindex.sequences import sequence_of
from assetindex.thumbs import (
    EMBEDDED_PREVIEW_EXTS,
    THUMB_SIZE,
    archive_preview_dir,
    compose_sheet,
    open_image,
    preview_image_bytes,
    save_webp,
    sheet_path,
    source_limit,
    unity_preview_path,
)

log = logging.getLogger(__name__)

SHEET_IMAGES = 9
STAGE = "preview"
MAX_POSTPONED = 3  # scans in a row an archive's preview may time out before it is given up
ASIDE = (TO_DELETE + "/", DUPLICATES + "/")  # archives on their way out get no preview
SPARE_CANDIDATES = 6  # replacements for images that fail to extract or decode
ARCHIVE_TIME_BUDGET_SECONDS = 60
# Opened at full cover size: a lone image is the whole cover, and compose_sheet
# shrinks them for a 2x2 or 3x3 grid.
CELL_BOX = THUMB_SIZE
# Written with each attempt. Built sheets older than SHEET_LAYOUT_VERSION are redone on
# the next scan; archives found empty before DRAWN_VERSION are retried if they hold
# a kind of file drawn since.
# 2: a single image fills the cover instead of sitting half-size in the middle.
# 3: .svg and .obj are drawn too.
SHEET_VERSION = 3
SHEET_LAYOUT_VERSION = 2
DRAWN_VERSION = 3
DRAWN_EXTS = ("svg", "obj")
UNITY_PREVIEW_BOX = (128, 128)
MAX_UNITY_PREVIEWS = 2000
MAX_UNITY_PREVIEW_BYTES = 512 * 1024
# Unity GUIDs are 32 lowercase hex chars; anything else in a package is not trusted as a name.
_GUID = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class PreviewStats:
    built: int
    empty: int
    skipped: int  # left pending: tool missing or archive unreadable


def _is_junk(path: str) -> bool:
    parts = path.split("/")
    return "__MACOSX" in parts or parts[-1].startswith("._")


def _ext(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def choose_candidates(entries: Iterable[tuple[int, str, int]], limit: int) -> list[tuple[int, str]]:
    """Pick images for a sheet: one per frame sequence, round-robin across folders.
    Real images come first; files a picture is got from (.uasset, .eps: often missing,
    and the whole file must be extracted to find one; .svg, .obj: drawn) fill up after."""
    entries = list(entries)  # may be a generator; iterated twice below
    images = [e for e in entries if _ext(e[1]) not in EMBEDDED_PREVIEW_EXTS]
    embedded = [e for e in entries if _ext(e[1]) in EMBEDDED_PREVIEW_EXTS]
    picked = _round_robin(images, limit)  # drops what is no picture (.mtl, .txt): count after
    return picked + _round_robin(embedded, limit - len(picked))


def _round_robin(entries: Iterable[tuple[int, str, int]], limit: int) -> list[tuple[int, str]]:
    by_folder: dict[str, list[tuple[int, str]]] = {}
    seen: set[str] = set()
    for entry_id, path, size in sorted(entries, key=lambda e: e[1]):
        max_size = source_limit(_ext(path))  # images, and files with an embedded preview
        if max_size is None or size > max_size or _is_junk(path):
            continue
        sequence = sequence_of(path, "image")
        key = sequence[0] if sequence else path
        if key in seen:
            continue
        seen.add(key)
        folder = path.rsplit("/", 1)[0] if "/" in path else ""
        by_folder.setdefault(folder, []).append((entry_id, path))

    queues = [by_folder[folder] for folder in sorted(by_folder)]
    if limit <= 0:
        return []
    chosen: list[tuple[int, str]] = []
    depth = 0
    while len(chosen) < limit and any(depth < len(q) for q in queues):
        chosen.extend(q[depth] for q in queues if depth < len(q))
        depth += 1
    return chosen[:limit]


class _PreviewPending(Exception):
    """Nothing usable yet, but only for transient reasons (timeouts): retry next scan."""


def _member_images(conn: sqlite3.Connection, file_id: int, archive: Path, tool: str) -> list[Image.Image]:
    entries = conn.execute(
        "SELECT id, path, size FROM archive_entries WHERE file_id = ?", (file_id,)
    ).fetchall()
    candidates = choose_candidates(((r["id"], r["path"], r["size"]) for r in entries), SHEET_IMAGES + SPARE_CANDIDATES)
    deadline = time.monotonic() + ARCHIVE_TIME_BUDGET_SECONDS
    images: list[Image.Image] = []
    transient = False
    for _, path in candidates:
        if len(images) >= SHEET_IMAGES:
            break
        if time.monotonic() > deadline:
            transient = True
            break
        try:
            ext = _ext(path)
            encoded = preview_image_bytes(ext, read_member(archive, path, source_limit(ext), tool))
            if encoded is None:
                continue  # e.g. a .uasset saved without a thumbnail, a plain .eps
            images.append(open_image(io.BytesIO(encoded), CELL_BOX))
        except ExtractTimeout as err:
            transient = True
            log.info("timed out extracting %r from %s: %s", path, archive, err)
        except ExtractError as err:
            log.debug("cannot extract %r from %s: %s", path, archive, err)
        except Exception as err:  # Pillow raises many types for malformed images
            log.debug("cannot decode %r from %s: %r", path, archive, err)
    if not images and transient:
        raise _PreviewPending(str(archive))
    return images


def _unity_images(
    conn: sqlite3.Connection, file_id: int, archive: Path, mtime: float, thumb_dir: Path
) -> list[Image.Image]:
    rows = conn.execute(
        "SELECT id, guid FROM archive_entries WHERE file_id = ? AND has_preview = 1 ORDER BY path, id LIMIT ?",
        (file_id, MAX_UNITY_PREVIEWS),
    ).fetchall()
    by_guid = {r["guid"]: r["id"] for r in rows if r["guid"] and _GUID.match(r["guid"])}
    try:
        raw = read_unity_previews(archive, set(by_guid), MAX_UNITY_PREVIEW_BYTES)
    except ArchiveError as err:
        log.info("cannot read previews from %s: %s", archive, err)
        return []
    images: list[Image.Image] = []
    for guid, entry_id in by_guid.items():
        if guid not in raw:
            continue
        try:
            image = open_image(io.BytesIO(raw[guid]), UNITY_PREVIEW_BOX)
        except Exception as err:  # Pillow raises many types for malformed images
            log.debug("bad unity preview %s in %s: %r", guid, archive, err)
            continue
        save_webp(image, unity_preview_path(thumb_dir, file_id, mtime, entry_id))
        if len(images) < SHEET_IMAGES:
            images.append(image)
    return images


def _inside_root(root: Path, rel_path: str) -> Path | None:
    """The archive's real path, or None if it can't be read or escapes the root (symlink)."""
    try:
        root_real = root.resolve()
        path = (root_real / rel_path).resolve(strict=True)
    except OSError:
        return None
    return path if path.is_relative_to(root_real) else None


def _postpone(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """Count one more scan whose preview timed out; False once that makes MAX_POSTPONED."""
    with conn:
        conn.execute("UPDATE archives SET preview_postponed = IFNULL(preview_postponed, 0) + 1 WHERE file_id = ?",
                     (row["id"],))
    count = conn.execute("SELECT preview_postponed FROM archives WHERE file_id = ?", (row["id"],)).fetchone()[0]
    return count < MAX_POSTPONED


def _is_rebuild(row: sqlite3.Row) -> bool:
    """A sheet from an older SHEET_VERSION, as opposed to a first attempt."""
    return row["preview_state"] == 1


def _render(
    conn: sqlite3.Connection, row: sqlite3.Row, archive: Path, thumb_dir: Path, tool: str | None
) -> list[Image.Image]:
    if not _is_rebuild(row):  # a rebuild overwrites the sheet, keeping it if the rebuild fails
        shutil.rmtree(archive_preview_dir(thumb_dir, row["id"]), ignore_errors=True)
    images = (
        _unity_images(conn, row["id"], archive, row["mtime"], thumb_dir) if row["ext"] == "unitypackage"
        else _member_images(conn, row["id"], archive, tool)
    )
    if images:
        compose_sheet(images, sheet_path(thumb_dir, row["id"], row["mtime"]))
    return images


def build_previews(
    conn: sqlite3.Connection,
    root_name: str,
    root: Path,
    thumb_dir: Path,
    tool: str | None,
    should_stop: Callable[[], bool] = never,
    progress: Callable[[str, int, int | None], None] = no_progress,
) -> PreviewStats:
    rows = conn.execute(
        """
        SELECT f.id, f.rel_path, f.ext, a.size, a.mtime, a.preview_state
        FROM archives a JOIN files f ON f.id = a.file_id
        WHERE f.root = ? AND a.status = 'ok' AND (
            a.preview_state IS NULL
            -- Unity's own previews are small already: older sheets of them look the same.
            OR (a.preview_state = 1 AND IFNULL(a.sheet_version, 0) < ? AND f.ext != 'unitypackage')
            OR (a.preview_state = 0 AND IFNULL(a.sheet_version, 0) < ? AND f.ext != 'unitypackage'
                AND EXISTS (SELECT 1 FROM archive_entries e WHERE e.file_id = a.file_id AND e.ext IN (?, ?)))
        )
        """,
        (root_name, SHEET_LAYOUT_VERSION, DRAWN_VERSION, *DRAWN_EXTS),
    ).fetchall()
    rows = [r for r in rows if not r["rel_path"].startswith(ASIDE)]  # on their way out: no picture needed
    built = empty = skipped = 0
    for i, row in enumerate(rows):
        progress("previews", i, len(rows))
        if should_stop():  # before attempts.begin: a stop is not a crash
            raise ScanStopped("stopped while building archive previews")
        is_unity = row["ext"] == "unitypackage"
        if not is_unity and tool is None:
            skipped += 1
            continue
        archive = _inside_root(root, row["rel_path"])
        if archive is None:
            log.warning("archive %r unreadable or outside its root; skipped", row["rel_path"])
            skipped += 1
            continue
        if attempts.begin(conn, row["id"], STAGE):
            try:
                images = _render(conn, row, archive, thumb_dir, tool)
            except _PreviewPending as err:
                # Timeouts: retried next scan, but a big solid 7z times out every time.
                if _postpone(conn, row):
                    skipped += 1
                    continue
                log.warning("previews for %r timed out %d scans in a row; given up: %r",
                            row["rel_path"], MAX_POSTPONED, err)
                images = []
            except (OSError, MemoryError) as err:
                # NAS hiccup, disk full, memory pressure: retry next scan.
                log.warning("previews for %r postponed: %r", row["rel_path"], err)
                skipped += 1
                continue
            finally:
                attempts.finish(conn, row["id"], STAGE)
        else:
            log.warning("previews for %r killed the scanner %d times; given up", row["rel_path"], attempts.MAX_ATTEMPTS)
            attempts.finish(conn, row["id"], STAGE)
            images = []
        if images:
            built += 1
        else:
            empty += 1
        with conn:
            # Guarded by size/mtime: if the archive was relisted meanwhile, leave it pending.
            conn.execute(
                "UPDATE archives SET preview_state = ?, sheet_version = ? WHERE file_id = ? AND size = ? AND mtime = ?",
                (1 if images or _is_rebuild(row) else 0, SHEET_VERSION, row["id"], row["size"], row["mtime"]),
            )
    progress("previews", len(rows), len(rows))
    return PreviewStats(built=built, empty=empty, skipped=skipped)
