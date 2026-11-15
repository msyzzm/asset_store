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
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from assetindex import attempts
from assetindex.archives import ArchiveError, read_unity_previews
from assetindex.extract import ExtractError, ExtractTimeout, read_member
from assetindex.sequences import sequence_of
from assetindex.thumbs import (
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
SPARE_CANDIDATES = 6  # replacements for images that fail to extract or decode
ARCHIVE_TIME_BUDGET_SECONDS = 60
CELL_BOX = (160, 160)
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
    Real images come first; .uasset thumbnails (often missing, costly to extract) after."""
    entries = list(entries)  # may be a generator; iterated twice below
    images = [e for e in entries if _ext(e[1]) != "uasset"]
    uassets = [e for e in entries if _ext(e[1]) == "uasset"] if len(images) < limit else []
    picked = _round_robin(images, limit)
    return picked + _round_robin(uassets, limit - len(picked))


def _round_robin(entries: Iterable[tuple[int, str, int]], limit: int) -> list[tuple[int, str]]:
    by_folder: dict[str, list[tuple[int, str]]] = {}
    seen: set[str] = set()
    for entry_id, path, size in sorted(entries, key=lambda e: e[1]):
        max_size = source_limit(_ext(path))  # images and .uasset (embedded editor thumbnail)
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
                continue  # e.g. a .uasset saved without a thumbnail
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


def _render(
    conn: sqlite3.Connection, row: sqlite3.Row, archive: Path, thumb_dir: Path, tool: str | None
) -> list[Image.Image]:
    shutil.rmtree(archive_preview_dir(thumb_dir, row["id"]), ignore_errors=True)
    images = (
        _unity_images(conn, row["id"], archive, row["mtime"], thumb_dir) if row["ext"] == "unitypackage"
        else _member_images(conn, row["id"], archive, tool)
    )
    if images:
        compose_sheet(images, sheet_path(thumb_dir, row["id"], row["mtime"]))
    return images


def build_previews(
    conn: sqlite3.Connection, root_name: str, root: Path, thumb_dir: Path, tool: str | None
) -> PreviewStats:
    rows = conn.execute(
        """
        SELECT f.id, f.rel_path, f.ext, a.size, a.mtime
        FROM archives a JOIN files f ON f.id = a.file_id
        WHERE f.root = ? AND a.status = 'ok' AND a.preview_state IS NULL
        """,
        (root_name,),
    ).fetchall()
    built = empty = skipped = 0
    for row in rows:
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
            except (OSError, MemoryError, _PreviewPending) as err:
                # NAS hiccup, disk full, memory pressure, timeouts: retry next scan.
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
                "UPDATE archives SET preview_state = ? WHERE file_id = ? AND size = ? AND mtime = ?",
                (1 if images else 0, row["id"], row["size"], row["mtime"]),
            )
    return PreviewStats(built=built, empty=empty, skipped=skipped)
