"""Thumbnail rendering and cache paths, shared by the web server and the scanner.

Every image decoded here is untrusted (NAS files, archive contents): Pillow may only use
an allowlist of decoders, and the pixel count is checked from the header before decoding.
"""

import hashlib
import uuid
import warnings
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
# Baseline JPEGs are decoded at up to 1/8 scale (1/64 of the pixels), so print-size
# photos and scans fit. Pillow's own bomb check would refuse them on open, before
# that scale is chosen: raise it to our ceiling, which open_image enforces itself.
JPEG_MAX_REDUCTION = 8
# Scaling saves memory, not CPU (every block is still entropy-decoded): 1G pixels is
# twice the largest print scan in the samples and takes a few seconds at most.
MAX_JPEG_SOURCE_PIXELS = 1_000_000_000
Image.MAX_IMAGE_PIXELS = MAX_JPEG_SOURCE_PIXELS // 2  # Pillow errors above twice this
warnings.simplefilter("ignore", Image.DecompressionBombWarning)  # open_image checks sizes itself
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


def _decodes_at_reduced_scale(im: Image.Image) -> bool:
    """Baseline JPEG: libjpeg decodes it straight to 1/2..1/8 size, so memory follows
    the reduced size. A progressive JPEG keeps full-size coefficients whatever the scale."""
    return im.format == "JPEG" and not (im.info.get("progressive") or im.info.get("progression"))


def open_image(source: Path | BinaryIO, box: tuple[int, int]) -> Image.Image:
    """Decode an untrusted image and shrink it to fit `box`. Raises on anything unexpected.
    The pixel limit applies to what is actually decoded: a print-size JPEG is fine,
    since it is decoded at up to 1/8 scale."""
    with Image.open(source, formats=THUMB_DECODERS) as im:
        width, height = im.size
        reducible = _decodes_at_reduced_scale(im)
        ceiling = min(MAX_JPEG_SOURCE_PIXELS, MAX_THUMB_SOURCE_PIXELS * JPEG_MAX_REDUCTION**2) if reducible else MAX_THUMB_SOURCE_PIXELS
        if width * height > ceiling:
            raise ValueError(f"image too large for thumbnail: {width}x{height}")
        im.draft("RGB", box)  # JPEG: pick the decode scale now (and im.size with it); no-op otherwise
        decoded_w, decoded_h = im.size
        if decoded_w * decoded_h > MAX_THUMB_SOURCE_PIXELS:
            raise ValueError(f"image too large for thumbnail: {width}x{height}")
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
    save_webp(sheet_image(images), target)


def sheet_image(images: Sequence[Image.Image]) -> Image.Image:
    """The contact sheet itself (RGBA), without saving it."""
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
    return sheet
