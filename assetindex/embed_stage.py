"""Scan stage: compute semantic-search vectors for new and changed cards.

One text vector per card (its path, plus names inside an archive) and, when the API
takes images, one image vector per card that has a picture (image files, .uasset
thumbnails, .eps previews, archive contact sheets). Hidden sequence frames are skipped: their card is
the head frame.

Runs after the rest of the scan and never fails it: if the API is down or the settings
are wrong, the stage logs, stops, and the next scan carries on where it left off.
Inputs the API rejects (bad image, too long) are recorded so they aren't resent every
scan; `scan --retry-errors` sends them again.
"""

import io
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import numpy as np
from PIL import Image

from assetindex.config import Settings
from assetindex.embedding import EmbedClient, EmbedConfig, EmbedError
from assetindex.kinds import LISTABLE_ARCHIVE_EXTS, THUMBNAIL_EXTS
from assetindex.scan_request import ScanStopped, never
from assetindex.thumbs import (
    EMBEDDED_PREVIEW_EXTS,
    file_thumb_path,
    open_image,
    preview_image_bytes,
    sheet_path,
    source_limit,
)

TEXT, IMAGE = "text", "image"
IMAGE_BOX = (256, 256)  # image tokens grow with pixels: small is cheap and enough to tell what it is
SHEET_BOX = (384, 384)  # a 3x3 contact sheet needs a little more
BACKGROUND = (128, 128, 128)  # transparent pixels (VFX sprites) go on neutral grey
JPEG_QUALITY = 85
ARCHIVE_ENTRIES_SAMPLED = 300
MAX_TEXT_CHARS = 1000
MAX_ERROR_CHARS = 300


@dataclass(frozen=True)
class EmbedStats:
    texts: int = 0
    images: int = 0
    rejected: int = 0  # inputs the API refused, or images that couldn't be decoded
    error: str | None = None  # why the stage stopped early


class _Unusable(Exception):
    """This card has no usable input for this source."""


def _readable(path: str) -> str:
    return path.replace("\\", "/").replace("/", " / ").replace("_", " ")


def card_text(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """What a card is about, in words: its path, what its picture shows (if described),
    and for archives the names inside. The description goes first so a long list of
    names can't push it past MAX_TEXT_CHARS."""
    text = _readable(row["rel_path"])
    caption = conn.execute("SELECT text FROM captions WHERE file_id = ? AND text IS NOT NULL", (row["id"],)).fetchone()
    if caption:
        text += " | 画面: " + " ".join(caption[0].split())
    if row["ext"] in LISTABLE_ARCHIVE_EXTS:
        names: dict[str, None] = {}  # ordered set
        entries = conn.execute(
            "SELECT path FROM archive_entries WHERE file_id = ? ORDER BY id LIMIT ?",
            (row["id"], ARCHIVE_ENTRIES_SAMPLED),
        )
        for (path,) in entries:
            pure = PurePosixPath(path.replace("\\", "/"))
            for part in (*pure.parent.parts, pure.stem):
                names.setdefault(part)
        if names:
            text += " | 包含: " + ", ".join(names)
    return text[:MAX_TEXT_CHARS]


def _has_image(row: sqlite3.Row) -> bool:
    ext = row["ext"]
    if ext in LISTABLE_ARCHIVE_EXTS:
        return row["preview_state"] == 1
    return ext in THUMBNAIL_EXTS or ext in EMBEDDED_PREVIEW_EXTS


def _source_on_disk(settings: Settings, row: sqlite3.Row) -> Path:
    root = settings.roots[row["root"]].resolve()
    path = (root / row["rel_path"]).resolve()
    if not path.is_relative_to(root):
        raise _Unusable("path leaves its root")
    return path


def _picture(settings: Settings, row: sqlite3.Row) -> tuple[Path | io.BytesIO, tuple[int, int]]:
    """The image to embed for a card, and how big to make it."""
    if row["ext"] in LISTABLE_ARCHIVE_EXTS:
        return sheet_path(settings.thumb_dir, row["id"], row["mtime"]), SHEET_BOX
    cached = file_thumb_path(settings.thumb_dir, row["id"], row["mtime"])
    if cached.exists():
        return cached, IMAGE_BOX
    path = _source_on_disk(settings, row)
    if row["ext"] not in EMBEDDED_PREVIEW_EXTS:
        return path, IMAGE_BOX
    limit = source_limit(row["ext"])
    with path.open("rb") as f:  # bounded: the file may have grown since the scan
        data = f.read(limit + 1)
    encoded = preview_image_bytes(row["ext"], data) if len(data) <= limit else None
    if encoded is None:
        raise _Unusable("no embedded preview")
    return io.BytesIO(encoded), IMAGE_BOX


def card_jpeg(settings: Settings, row: sqlite3.Row, box: tuple[int, int] | None = None) -> bytes:
    """The card's picture as a JPEG, at most `box` (default: small, for embedding)."""
    source, default_box = _picture(settings, row)
    try:
        image = open_image(source, box or default_box)
    except Exception as err:  # Pillow raises many types for malformed files
        raise _Unusable(f"cannot decode image: {err!r}") from None
    if image.mode == "RGBA":
        flat = Image.new("RGB", image.size, BACKGROUND)
        flat.paste(image, mask=image.getchannel("A"))
        image = flat
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def _pending(conn: sqlite3.Connection, settings: Settings, source: str, model: str) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT f.id, f.root, f.rel_path, f.ext, f.mtime, a.preview_state
        FROM files f
        LEFT JOIN archives a ON a.file_id = f.id
        LEFT JOIN embeddings e ON e.file_id = f.id AND e.source = ?
        WHERE (f.seq_frames IS NULL OR f.seq_frames > 0)
          AND (e.file_id IS NULL OR e.model != ? OR e.mtime != f.mtime)
        ORDER BY f.id
        """,
        (source, model),
    ).fetchall()
    rows = [r for r in rows if r["root"] in settings.roots]
    return [r for r in rows if _has_image(r)] if source == IMAGE else rows


def _save(conn: sqlite3.Connection, row: sqlite3.Row, source: str, model: str, vector=None, error=None) -> bool:
    """Store a vector (unit length) or an error; True if a vector was stored."""
    blob = None
    if vector is not None:
        arr = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(arr))
        if norm > 0 and np.isfinite(norm):
            blob = (arr / norm).astype(np.float32).tobytes()
        else:
            error = "the API returned a zero vector"
    conn.execute(
        "INSERT OR REPLACE INTO embeddings (file_id, source, model, mtime, vector, error) VALUES (?, ?, ?, ?, ?, ?)",
        (row["id"], source, model, row["mtime"], blob, error[:MAX_ERROR_CHARS] if error else None),
    )
    return blob is not None


@dataclass
class _Tally:
    done: int = 0
    rejected: int = 0


def _embed_chunk(chunk, embed: Callable[[list], list]) -> list[tuple[sqlite3.Row, list | None, str | None]]:
    """Embed (row, input) pairs: (row, vector, None) or (row, None, why it was rejected).
    On a rejected batch, finds the bad inputs one at a time. Raises EmbedError when the
    server or the settings, not an input, are the problem. No database work here: the
    calls can take a while, and must not hold SQLite's write lock meanwhile."""
    try:
        vectors = embed([inp for _, inp in chunk])
        return [(row, vector, None) for (row, _), vector in zip(chunk, vectors, strict=True)]
    except EmbedError as err:
        if not err.item_problem:
            raise
        if len(chunk) == 1:
            return [(chunk[0][0], None, str(err))]
        results = [r for pair in chunk for r in _embed_chunk([pair], embed)]
        if all(vector is None for _, vector, _ in results):
            # Every input refused: the request shape or model is wrong, not the inputs.
            # Recording them all would stop them from being retried once that's fixed.
            raise EmbedError(f"整批都被拒绝，可能是设置不对：{err}") from None
        return results


def _without_new_captions(conn: sqlite3.Connection, results: list, started: float) -> list:
    """Drop text vectors of cards described (by the captioner, another process) while
    they were being embedded: they hold the text from before the caption. Left out,
    those cards stay pending and get embedded again, with the caption."""
    ids = [row["id"] for row, _, _ in results]
    if not ids:
        return results
    fresh = {
        r[0]
        for r in conn.execute(
            f"SELECT file_id FROM captions WHERE text IS NOT NULL AND created >= ?"
            f" AND file_id IN ({','.join('?' * len(ids))})",
            (started, *ids),
        )
    }
    return [r for r in results if r[0]["id"] not in fresh]


def _run_source(
    conn, rows, source, config: EmbedConfig, prepare, embed, should_stop: Callable[[], bool] = never
) -> tuple[_Tally, str | None]:
    tally = _Tally()
    for start in range(0, len(rows), config.batch_size):
        if should_stop():
            raise ScanStopped("stopped while embedding")
        started = time.time()  # before card_text reads any caption
        chunk, unusable = [], []
        for row in rows[start : start + config.batch_size]:
            try:
                chunk.append((row, prepare(row)))
            except Exception as err:  # gone since the scan, undecodable...: never let one card
                unusable.append((row, None, str(err) or type(err).__name__))  # block the rest
        try:
            results = _embed_chunk(chunk, embed) if chunk else []
        except EmbedError as err:
            return tally, f"{source}: {err}"
        if source == TEXT:
            results = _without_new_captions(conn, results, started)
        with conn:  # one short write per chunk: a stopped scanner resumes from here
            for row, vector, error in [*results, *unusable]:
                if _save(conn, row, source, config.model, vector=vector, error=error):
                    tally.done += 1
                else:
                    tally.rejected += 1
    return tally, None


def embed_pending(
    conn: sqlite3.Connection,
    settings: Settings,
    config: EmbedConfig,
    retry_errors: bool = False,
    client: EmbedClient | None = None,
    should_stop: Callable[[], bool] = never,
) -> EmbedStats:
    if not config.active:
        return EmbedStats()
    client = client or EmbedClient(config)
    if retry_errors:
        with conn:
            conn.execute("DELETE FROM embeddings WHERE vector IS NULL")

    texts, error = _run_source(
        conn, _pending(conn, settings, TEXT, config.model), TEXT, config,
        lambda row: card_text(conn, row), client.embed_texts, should_stop,
    )
    images = _Tally()
    if error is None and config.embeds_images:
        images, error = _run_source(
            conn, _pending(conn, settings, IMAGE, config.model), IMAGE, config,
            lambda row: card_jpeg(settings, row), client.embed_images, should_stop,
        )
    return EmbedStats(texts.done, images.done, texts.rejected + images.rejected, error)


def progress(conn: sqlite3.Connection, model: str) -> dict:
    """How far embedding has got with this model, for the settings page."""
    visible = "(f.seq_frames IS NULL OR f.seq_frames > 0)"
    current = f"JOIN files f ON f.id = e.file_id AND e.mtime = f.mtime WHERE e.model = ? AND {visible}"
    picture_exts = sorted(THUMBNAIL_EXTS | EMBEDDED_PREVIEW_EXTS)
    archive_exts = sorted(LISTABLE_ARCHIVE_EXTS)

    def count(sql: str, params=()) -> int:
        return conn.execute(sql, params).fetchone()[0]

    return {
        "cards": count(f"SELECT COUNT(*) FROM files f WHERE {visible}"),
        "text": count(f"SELECT COUNT(*) FROM embeddings e {current} AND e.source = 'text' AND e.vector IS NOT NULL", (model,)),
        "image": count(f"SELECT COUNT(*) FROM embeddings e {current} AND e.source = 'image' AND e.vector IS NOT NULL", (model,)),
        "image_cards": count(
            f"SELECT COUNT(*) FROM files f LEFT JOIN archives a ON a.file_id = f.id WHERE {visible} AND"
            f" (f.ext IN ({','.join('?' * len(picture_exts))})"
            f" OR (f.ext IN ({','.join('?' * len(archive_exts))}) AND a.preview_state = 1))",
            (*picture_exts, *archive_exts),
        ),
        "rejected": count(f"SELECT COUNT(*) FROM embeddings e {current} AND e.vector IS NULL", (model,)),
    }
