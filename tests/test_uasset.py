import io
import struct

from PIL import Image

from assetindex.uasset import UE_PACKAGE_MAGIC, find_thumbnail


def _image_bytes(fmt: str, size=(32, 32)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, fmt)
    return buf.getvalue()


def make_uasset(fmt: str = "JPEG", junk: bytes = b"\0" * 200) -> bytes:
    """Package magic, filler, then a thumbnail record: width, height, size, image bytes.
    UE5 marks JPEG thumbnails with a negative height; UE4 stores PNG with a positive one."""
    image = _image_bytes(fmt)
    height = -32 if fmt == "JPEG" else 32
    record = struct.pack("<iii", 32, height, len(image)) + image
    return struct.pack("<I", UE_PACKAGE_MAGIC) + junk + record + b"\0" * 64


def test_finds_ue5_jpeg_thumbnail():
    data = find_thumbnail(make_uasset("JPEG"))

    assert data is not None and data.startswith(b"\xff\xd8\xff")
    assert Image.open(io.BytesIO(data)).size == (32, 32)


def test_finds_ue4_png_thumbnail():
    data = find_thumbnail(make_uasset("PNG"))

    assert data is not None and data.startswith(b"\x89PNG")


def test_ignores_image_bytes_without_a_valid_thumbnail_header():
    # Texture source data embeds raw PNG/JPEG too, but not behind a (w, h, size) record.
    blob = struct.pack("<I", UE_PACKAGE_MAGIC) + b"\xff" * 12 + _image_bytes("JPEG") + _image_bytes("PNG")

    assert find_thumbnail(blob) is None


def test_rejects_files_that_are_not_ue_packages():
    blob = make_uasset("JPEG")

    assert find_thumbnail(b"XXXX" + blob[4:]) is None


def test_size_beyond_end_of_file_is_rejected():
    blob = make_uasset("JPEG")
    truncated = blob[: blob.find(b"\xff\xd8\xff") + 20]

    assert find_thumbnail(truncated) is None


def test_hostile_blob_of_signatures_is_bounded():
    import time

    blob = struct.pack("<I", UE_PACKAGE_MAGIC) + b"\xff\xd8\xff" * 2_000_000

    started = time.perf_counter()
    assert find_thumbnail(blob) is None
    assert time.perf_counter() - started < 0.5
