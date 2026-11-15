"""Find the editor thumbnail Unreal stores inside a .uasset package.

The package's thumbnail table points at records of (int32 width, int32 height,
int32 size, image bytes): JPEG in UE5 (signalled by a negative height), PNG in UE4.
Walking the version-dependent package summary to reach the table is fragile, so we
search for an image signature preceded by a plausible record header instead. Texture
source data also embeds PNG/JPEG, but not behind such a header.
"""

import struct

UE_PACKAGE_MAGIC = 0x9E2A83C1
MAX_UASSET_BYTES = 64 * 1024**2  # read limit when a .uasset is extracted for its thumbnail
MAX_THUMBNAIL_SIDE = 1024  # editor thumbnails are 256px; anything far bigger is not one
# Signature hits checked per format. Real packages have a handful; a hostile file packed
# with signatures would otherwise keep a Python loop busy for tens of seconds.
MAX_SIGNATURE_PROBES = 256
_HEADER = struct.Struct("<iii")
_SIGNATURES = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")


def _record_at(blob: bytes, pos: int, is_jpeg: bool) -> bytes | None:
    if pos < _HEADER.size:
        return None
    width, height, size = _HEADER.unpack_from(blob, pos - _HEADER.size)
    # UE5 flags JPEG data with a negative height; UE4 PNG thumbnails have a positive one.
    if is_jpeg != (height < 0):
        return None
    if not (0 < width <= MAX_THUMBNAIL_SIDE and 0 < abs(height) <= MAX_THUMBNAIL_SIDE):
        return None
    if not 0 < size <= len(blob) - pos:
        return None
    return blob[pos : pos + size]


def find_thumbnail(blob: bytes) -> bytes | None:
    """Encoded thumbnail image (JPEG or PNG) from a .uasset's bytes, or None."""
    if len(blob) < 4 or struct.unpack_from("<I", blob)[0] != UE_PACKAGE_MAGIC:
        return None
    for signature in _SIGNATURES:
        is_jpeg = signature.startswith(b"\xff")
        pos = blob.find(signature)
        for _ in range(MAX_SIGNATURE_PROBES):
            if pos < 0:
                break
            record = _record_at(blob, pos, is_jpeg)
            if record is not None:
                return record
            pos = blob.find(signature, pos + 1)
    return None
