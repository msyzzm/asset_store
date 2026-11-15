"""Thumbnail rendering and cache paths, shared by the web server and the scanner.

Every image decoded here is untrusted (NAS files, archive contents): Pillow may only use
an allowlist of decoders, and the pixel count is checked from the header before decoding.
"""

import hashlib
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO

from PIL import Image

from assetindex.eps import MAX_EPS_BYTES, find_preview
from assetindex.kinds import THUMBNAIL_EXTS
from assetindex.uasset import MAX_UASSET_BYTES, find_thumbnail

THUMB_SIZE = (320, 320)
THUMB_QUALITY = 80
FILES_PER_THUMB_DIR = 1000
# Checked from the header before decoding, so oversized images never hit memory.
MAX_THUMB_SOURCE_PIXELS = 50_000_000
# Largest image extracted from an archive for a preview (checked before extracting).
MAX_ARCHIVE_IMAGE_BYTES = 20 * 1024**2
# Pillow sniffs content, not extension: only let it use decoders we expect.
THUMB_DECODERS = ("PNG", "JPEG", "GIF", "WEBP", "BMP", "TGA", "PSD", "TIFF")


# Files that aren't images but carry one: an .uasset's editor thumbnail, an .eps preview.
EMBEDDED_PREVIEW_EXTS = frozenset({"uasset", "eps"})


def source_limit(ext: str) -> int | None:
    """Most bytes to read for a preview of this file type, or None if it has no preview."""
    if ext in THUMBNAIL_EXTS:
        return MAX_ARCHIVE_IMAGE_BYTES
    if ext == "uasset":
        return MAX_UASSET_BYTES
    if ext == "eps":
        return MAX_EPS_BYTES
    return None


def preview_image_bytes(ext: str, data: bytes) -> bytes | None:
    """The encoded image to decode: the file itself, or the one embedded in it."""
    if ext == "uasset":
        return find_thumbnail(data)
    if ext == "eps":
        return find_preview(data)
    return data


def file_thumb_path(thumb_dir: Path, file_id: int, mtime: float) -> Path:
    # mtime in the name: a changed source file gets a fresh thumbnail.
    return thumb_dir / str(file_id // FILES_PER_THUMB_DIR) / f"{file_id}_{int(mtime)}.webp"


def archive_preview_dir(thumb_dir: Path, file_id: int) -> Path:
    """Previews built at scan time for one archive; rebuilt from scratch on each relist."""
    return thumb_dir / "archives" / str(file_id)


def sheet_path(thumb_dir: Path, file_id: int, mtime: float) -> Path:
    return archive_preview_dir(thumb_dir, file_id) / f"sheet_{int(mtime)}.webp"


def unity_preview_path(thumb_dir: Path, file_id: int, mtime: float, entry_id: int) -> Path:
    # mtime in the name: entry ids are reused after a relist, so an old file must not match.
    return archive_preview_dir(thumb_dir, file_id) / f"u_{int(mtime)}_{entry_id}.webp"


def entry_thumb_path(thumb_dir: Path, file_id: int, mtime: float, size: int, inner_path: str) -> Path:
    """On-demand thumbnail of a file inside an archive. Entry ids are reused after a relist,
    so the key is derived from the archive's identity and the inner path instead."""
    key = hashlib.sha256(f"{file_id}\0{mtime}\0{size}\0{inner_path}".encode()).hexdigest()
    return thumb_dir / "entries" / key[:2] / f"{key}.webp"


def open_image(source: Path | BinaryIO, box: tuple[int, int]) -> Image.Image:
    """Decode an untrusted image and shrink it to fit `box`. Raises on anything unexpected."""
    with Image.open(source, formats=THUMB_DECODERS) as im:
        width, height = im.size
        if width * height > MAX_THUMB_SOURCE_PIXELS:
            raise ValueError(f"image too large for thumbnail: {width}x{height}")
        im.draft("RGB", box)  # fast downscale for JPEG; no-op otherwise
        im.thumbnail(box)
        return im.copy() if im.mode in ("RGB", "RGBA") else im.convert("RGBA")


def save_webp(image: Image.Image, target: Path) -> None:
    """Write atomically: a reader never sees a half-written file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.stem}.{uuid.uuid4().hex}.tmp")
    try:
        image.save(tmp, "WEBP", quality=THUMB_QUALITY)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


def render_thumbnail(source: Path | BinaryIO, target: Path, box: tuple[int, int] = THUMB_SIZE) -> None:
    save_webp(open_image(source, box), target)


def _grid(count: int) -> int:
    return 1 if count == 1 else 2 if count <= 4 else 3


def compose_sheet(images: Sequence[Image.Image], target: Path) -> None:
    """Contact sheet: 1, 2x2 or 3x3 cells, each image centred in its cell."""
    if not images:
        raise ValueError("no images for sheet")
    cols = _grid(len(images))
    cell = THUMB_SIZE[0] // cols
    sheet = Image.new("RGBA", (cell * cols, cell * cols), (0, 0, 0, 0))
    for index, image in enumerate(images[: cols * cols]):
        fitted = image.copy()
        fitted.thumbnail((cell - 4, cell - 4))
        # Tiny sprites would be dots in the middle of a cell: scale up by a whole
        # factor with nearest-neighbour so pixel art stays crisp.
        factor = int(min((cell - 4) / fitted.width, (cell - 4) / fitted.height))
        if factor > 1:
            fitted = fitted.resize((fitted.width * factor, fitted.height * factor), Image.Resampling.NEAREST)
        row, col = divmod(index, cols)
        x = col * cell + (cell - fitted.width) // 2
        y = row * cell + (cell - fitted.height) // 2
        sheet.paste(fitted, (x, y), fitted if fitted.mode == "RGBA" else None)
    save_webp(sheet, target)
