import io

from PIL import Image

from assetindex import hdr
from assetindex.hdr import render_hdr

HEAD = b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n"


def _rle_line(pixels: list[tuple[int, int, int, int]]) -> bytes:
    """A new-style scanline: each channel as literal runs of up to 128 bytes."""
    width = len(pixels)
    out = bytes([2, 2, width >> 8, width & 0xFF])
    for channel in range(4):
        values = bytes(p[channel] for p in pixels)
        for start in range(0, width, 128):
            chunk = values[start:start + 128]
            out += bytes([len(chunk)]) + chunk
    return out


def _sky(width=16, height=8, rle=True, orientation=b"-Y") -> bytes:
    """Bright on top (exponent 129: about 1.0), dark below (exponent 120)."""
    lines = b""
    for y in range(height):
        pixel = (128, 160, 255, 129) if y < height // 2 else (128, 128, 128, 120)
        row = [pixel] * width
        lines += _rle_line(row) if rle else b"".join(bytes(p) for p in row)
    return HEAD + orientation + b" %d +X %d\n" % (height, width) + lines


def _open(data):
    return Image.open(io.BytesIO(data)).convert("RGB")


def test_a_run_length_hdr_becomes_a_picture_brighter_on_top():
    image = _open(render_hdr(_sky()))

    assert image.size == (16, 8)
    top, bottom = image.getpixel((8, 1)), image.getpixel((8, 6))
    assert sum(top) > sum(bottom) and top[2] > top[0]  # bright blue sky over a dark ground


def test_flat_and_upside_down_files_are_read_too():
    assert _open(render_hdr(_sky(rle=False))).getpixel((8, 1)) == _open(render_hdr(_sky())).getpixel((8, 1))
    flipped = _open(render_hdr(_sky(orientation=b"+Y")))
    assert sum(flipped.getpixel((8, 6))) > sum(flipped.getpixel((8, 1)))


def test_wide_pictures_keep_only_the_columns_a_thumbnail_needs(monkeypatch):
    monkeypatch.setattr(hdr, "OUT_WIDTH", 8)

    assert _open(render_hdr(_sky(width=32, height=8))).size == (8, 2)  # every 4th row and column


def test_broken_files_have_no_picture():
    whole = _sky()
    assert render_hdr(whole[:-20]) is None  # cut short
    assert render_hdr(b"P6\n1 1\n255\n\0\0\0") is None  # not Radiance
    assert render_hdr(HEAD + b"-Y 8 -X 16\n") is None  # an orientation it does not read
    assert render_hdr(b"#?RADIANCE\nFORMAT=32-bit_rle_xyze\n\n-Y 1 +X 1\n\0\0\0\0") is None
    zero_run = HEAD + b"-Y 1 +X 16\n" + bytes([2, 2, 0, 16, 0])
    assert render_hdr(zero_run) is None  # a run of 0 would never end
