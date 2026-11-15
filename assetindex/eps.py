"""Preview images embedded in EPS files.

EPS is PostScript, and drawing it needs Ghostscript: a big interpreter with a long
security record, not something to point at files from anywhere. Most EPS files made
for print and stock sites carry a ready-made preview instead: a "DOS EPS" binary
header points at a TIFF picture of the artwork. That TIFF is what we show.

Illustrator writes the TIFF as a palette image with an alpha channel (two samples per
pixel), a layout Pillow doesn't open. It is uncompressed, so that one layout is
decoded here by hand; any other TIFF is left to Pillow.
"""

import io
import struct

from PIL import Image, TiffImagePlugin

MAX_EPS_BYTES = 64 * 1024**2  # read limit when an .eps is opened for its preview
# Previews are screen-sized (the samples are at most 2346x1081); tighter than the 50M
# allowed for real images, since a "preview" this big is more likely an attack.
MAX_PREVIEW_PIXELS = 16_000_000
MAX_STRIPS = 65_536
PREVIEW_BOX = (1024, 1024)  # re-encoded previews are only ever shown as thumbnails
DOS_EPS_MAGIC = bytes([0xC5, 0xD0, 0xD3, 0xC6])
_HEADER = struct.Struct("<4s6IH")  # magic, PS offset/length, WMF offset/length, TIFF offset/length, checksum

# TIFF tags
_WIDTH, _HEIGHT, _BITS, _COMPRESSION, _PHOTOMETRIC = 256, 257, 258, 259, 262
_STRIP_OFFSETS, _SAMPLES, _STRIP_COUNTS, _PLANAR, _COLORMAP = 273, 277, 279, 284, 320
_PALETTE, _UNCOMPRESSED, _CHUNKY = 3, 1, 1


def _tiff_segment(data: bytes) -> bytes | None:
    if len(data) < _HEADER.size or data[:4] != DOS_EPS_MAGIC:
        return None
    _, _, _, _, _, offset, length, _ = _HEADER.unpack_from(data)
    if not length or offset < _HEADER.size or offset + length > len(data):
        return None
    return data[offset : offset + length]


def _pillow_can_read(tiff: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(tiff), formats=["TIFF"]) as image:
            width, height = image.size
            return width * height <= MAX_PREVIEW_PIXELS
    except Exception:  # Pillow raises many types for files it can't handle
        return False


def _declared_strips_ok(tiff: bytes) -> bool:
    """Read the strip counts straight from the raw directory before Pillow loads it:
    loading turns every listed value into a Python int, and a 64 MB file can list
    16 million of them."""
    order = {b"II": "<", b"MM": ">"}.get(tiff[:2])
    if order is None or len(tiff) < 8:
        return False
    (ifd_offset,) = struct.unpack_from(order + "I", tiff, 4)
    if ifd_offset + 2 > len(tiff):
        return False
    (entries,) = struct.unpack_from(order + "H", tiff, ifd_offset)
    for i in range(entries):
        at = ifd_offset + 2 + 12 * i
        if at + 12 > len(tiff):
            return False
        tag, _, count = struct.unpack_from(order + "HHI", tiff, at)
        if tag in (_STRIP_OFFSETS, _STRIP_COUNTS) and count > MAX_STRIPS:
            return False
    return True


def _tags(tiff: bytes) -> TiffImagePlugin.ImageFileDirectory_v2 | None:
    if not _declared_strips_ok(tiff):
        return None
    try:
        tags = TiffImagePlugin.ImageFileDirectory_v2(tiff[:8])
        stream = io.BytesIO(tiff)
        stream.seek(tags.next)
        tags.load(stream)
        return tags
    except Exception:  # malformed header or directory
        return None


def _strips_ok(tags: TiffImagePlugin.ImageFileDirectory_v2, size: int) -> bool:
    """Sane strip layout, checked before anything (Pillow included) acts on it. Strips
    pointing at the same bytes, or millions of tiny strips, would otherwise let a small
    file cost gigabytes of memory or minutes of CPU."""
    offsets, counts = tags.get(_STRIP_OFFSETS), tags.get(_STRIP_COUNTS)
    height = tags.get(_HEIGHT)
    if not offsets or not isinstance(height, int) or len(offsets) != len(counts or ()):
        return False
    if len(offsets) > min(height, MAX_STRIPS):
        return False
    spans = sorted(zip(offsets, counts, strict=True))
    if any(not isinstance(v, int) or v < 0 for span in spans for v in span):
        return False
    end = 0
    for offset, count in spans:
        if offset < end or offset + count > size:  # overlapping, or past the end
            return False
        end = offset + count
    return True


def _palette_alpha(tiff: bytes, tags: TiffImagePlugin.ImageFileDirectory_v2) -> bytes | None:
    """PNG of an uncompressed 8-bit palette + alpha TIFF, or None if it isn't one."""
    width, height = tags.get(_WIDTH), tags.get(_HEIGHT)
    colormap = tags.get(_COLORMAP) or ()
    layout_ok = (
        isinstance(width, int)
        and isinstance(height, int)
        and 0 < width * height <= MAX_PREVIEW_PIXELS
        and tags.get(_PHOTOMETRIC) == _PALETTE
        and tags.get(_SAMPLES) == 2
        and tags.get(_COMPRESSION, _UNCOMPRESSED) == _UNCOMPRESSED
        and tags.get(_PLANAR, _CHUNKY) == _CHUNKY
        and all(bits == 8 for bits in tags.get(_BITS, (8,)))
        and len(colormap) == 768
        and all(isinstance(v, int) and 0 <= v <= 0xFFFF for v in colormap)
    )
    if not layout_ok:
        return None
    needed = width * height * 2
    chunks, have = [], 0
    for offset, count in zip(tags[_STRIP_OFFSETS], tags[_STRIP_COUNTS], strict=True):
        if have >= needed:
            break
        chunks.append(tiff[offset : offset + count])
        have += count
    if have < needed:
        return None
    pixels = b"".join(chunks)[:needed]

    image = Image.frombytes("P", (width, height), pixels[0::2])
    # TIFF colormaps are 16-bit, all reds then all greens then all blues.
    image.putpalette(bytes(colormap[c * 256 + i] >> 8 for i in range(256) for c in range(3)))
    image = image.convert("RGBA")
    image.putalpha(Image.frombytes("L", (width, height), pixels[1::2]))
    image.thumbnail(PREVIEW_BOX)
    buf = io.BytesIO()
    image.save(buf, "PNG", compress_level=1)
    return buf.getvalue()


def find_preview(data: bytes) -> bytes | None:
    """Encoded preview image (TIFF or PNG) from an EPS file's bytes, or None. Never
    raises: a malformed file just has no preview."""
    try:
        tiff = _tiff_segment(data)
        tags = _tags(tiff) if tiff else None
        if tags is None or not _strips_ok(tags, len(tiff)):
            return None
        if _pillow_can_read(tiff):
            return tiff
        return _palette_alpha(tiff, tags)
    except Exception:  # odd tag types (inf, floats, huge ints) can trip any step above
        return None
