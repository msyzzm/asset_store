import io
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from assetindex import eps
from assetindex.thumbs import open_image

DOS_EPS_MAGIC = bytes([0xC5, 0xD0, 0xD3, 0xC6])
POSTSCRIPT = b"%!PS-Adobe-3.1 EPSF-3.0\n%%BoundingBox: 0 0 4 2\nshowpage\n"


def palette_alpha_tiff(
    width: int,
    height: int,
    pixels: list[tuple[int, int]],
    colors: dict[int, tuple],
    strips: list[tuple[int, int]] | None = None,
    cmap_long: bool = False,
) -> bytes:
    """Uncompressed palette + alpha TIFF laid out the way Illustrator writes EPS previews:
    photometric 3, two samples per pixel (index, alpha), BitsPerSample given once.
    `strips` overrides (offset, length) pairs relative to the pixel data; `cmap_long`
    stores the colormap as 32-bit values, as a broken writer might."""
    data = bytes(b for index, alpha in pixels for b in (index, alpha))
    cmap = [0] * 768
    for index, (r, g, b) in colors.items():
        cmap[index], cmap[256 + index], cmap[512 + index] = r * 257, g * 257, b * 257
    if cmap_long:
        cmap[0] = 0x1_0000  # out of range for a 16-bit colormap
    cmap_bytes = struct.pack(f"<768{'I' if cmap_long else 'H'}", *cmap)
    data_off = 8
    strips = [(data_off + o, c) for o, c in (strips or [(0, len(data))])]
    cmap_off = data_off + len(data)
    offsets_off = cmap_off + len(cmap_bytes)
    counts_off = offsets_off + 4 * len(strips)
    ifd_off = counts_off + 4 * len(strips)
    many = len(strips) > 1
    entries = [  # tag, type (3 SHORT, 4 LONG, 9 SLONG), count, value-or-offset
        (256, 3, 1, width),
        (257, 3, 1, height),
        (258, 3, 1, 8),
        (259, 3, 1, 1),
        (262, 3, 1, 3),
        (273, 9, len(strips), offsets_off if many else strips[0][0]),
        (277, 3, 1, 2),
        (278, 3, 1, height),
        (279, 9, len(strips), counts_off if many else strips[0][1]),
        (284, 3, 1, 1),
        (320, 4 if cmap_long else 3, 768, cmap_off),
        (338, 3, 1, 1),
    ]
    ifd = struct.pack("<H", len(entries))
    for tag, typ, count, value in entries:
        if typ == 3 and count == 1:
            packed = struct.pack("<HH", value, 0)
        else:
            packed = struct.pack("<i" if typ == 9 else "<I", value)
        ifd += struct.pack("<HHI", tag, typ, count) + packed
    ifd += struct.pack("<I", 0)
    arrays = struct.pack(f"<{len(strips)}i", *(o for o, _ in strips)) + struct.pack(
        f"<{len(strips)}i", *(c for _, c in strips)
    )
    return b"II*" + bytes([0]) + struct.pack("<I", ifd_off) + data + cmap_bytes + arrays + ifd


def dos_eps(tiff: bytes, postscript: bytes = POSTSCRIPT) -> bytes:
    header_size = 30
    tiff_off = header_size + len(postscript)
    header = DOS_EPS_MAGIC + struct.pack("<6IH", header_size, len(postscript), 0, 0, tiff_off, len(tiff), 0xFFFF)
    return header + postscript + tiff


# Two red opaque pixels, then a half-transparent blue and a fully transparent one.
PIXELS = [(1, 255), (1, 255), (2, 128), (2, 0)]
COLORS = {1: (255, 0, 0), 2: (0, 0, 255)}


def _decode(encoded: bytes) -> Image.Image:
    return open_image(io.BytesIO(encoded), (64, 64)).convert("RGBA")


def test_illustrator_palette_alpha_preview_is_decoded():
    encoded = eps.find_preview(dos_eps(palette_alpha_tiff(2, 2, PIXELS, COLORS)))

    image = _decode(encoded)
    assert image.size == (2, 2)
    assert image.getpixel((0, 0)) == (255, 0, 0, 255)
    assert image.getpixel((0, 1))[2:] == (255, 128)  # blue, half transparent
    assert image.getpixel((1, 1))[3] == 0


def test_ordinary_tiff_previews_pass_through():
    buf = io.BytesIO()
    Image.new("RGB", (5, 3), (0, 200, 0)).save(buf, format="TIFF")

    encoded = eps.find_preview(dos_eps(buf.getvalue()))

    assert encoded == buf.getvalue()
    assert _decode(encoded).getpixel((2, 1))[:3] == (0, 200, 0)


@pytest.mark.parametrize(
    "data",
    [
        POSTSCRIPT,  # plain EPS: no binary header, no preview we can read
        b"",
        DOS_EPS_MAGIC + bytes(10),  # header cut short
        dos_eps(b"II*" + bytes([0]) + b"garbage"),
        dos_eps(palette_alpha_tiff(2, 2, PIXELS, COLORS))[:-900],  # TIFF cut short
    ],
    ids=["plain-postscript", "empty", "short-header", "garbage-tiff", "truncated-tiff"],
)
def test_missing_or_broken_previews_give_none(data):
    assert eps.find_preview(data) is None


def test_preview_offsets_outside_the_file_give_none():
    data = bytearray(dos_eps(palette_alpha_tiff(2, 2, PIXELS, COLORS)))
    struct.pack_into("<I", data, 20, 10**9)  # tiff offset far past the end
    assert eps.find_preview(bytes(data)) is None


def test_previews_split_into_strips_are_joined():
    # 2x2 image as two strips of one row each.
    encoded = eps.find_preview(dos_eps(palette_alpha_tiff(2, 2, PIXELS, COLORS, strips=[(0, 4), (4, 4)])))
    assert _decode(encoded).getpixel((0, 1))[2:] == (255, 128)


@pytest.mark.parametrize(
    "strips",
    [
        [(0, 8)] * 2,  # same bytes twice: many of these used to multiply memory use
        [(-100, 8)],  # negative offset (strips are relative to the pixel data at byte 8)
        [(0, 2), (2, 2), (4, 2), (6, 2)],  # more strips than rows
    ],
    ids=["overlapping", "negative", "too-many"],
)
def test_bogus_strip_layouts_give_none(strips):
    assert eps.find_preview(dos_eps(palette_alpha_tiff(2, 2, PIXELS, COLORS, strips=strips))) is None


def test_millions_of_strips_are_refused_without_loading_them(monkeypatch):
    import time

    from PIL import TiffImagePlugin

    loads = []
    real_load = TiffImagePlugin.ImageFileDirectory_v2.load
    monkeypatch.setattr(TiffImagePlugin.ImageFileDirectory_v2, "load", lambda self, fp: loads.append(1) or real_load(self, fp))
    many = [(0, 1)] * (eps.MAX_STRIPS + 1)
    data = dos_eps(palette_alpha_tiff(2, 2, PIXELS, COLORS, strips=many))

    started = time.perf_counter()
    assert eps.find_preview(data) is None
    assert loads == [] and time.perf_counter() - started < 1


def test_out_of_range_colormap_gives_none():
    assert eps.find_preview(dos_eps(palette_alpha_tiff(2, 2, PIXELS, COLORS, cmap_long=True))) is None


def test_unexpected_errors_never_escape(monkeypatch):
    def boom(tiff):
        raise OverflowError("cannot convert float infinity to integer")

    monkeypatch.setattr(eps, "_palette_alpha", boom)
    assert eps.find_preview(dos_eps(palette_alpha_tiff(2, 2, PIXELS, COLORS))) is None


def test_absurd_dimensions_are_refused(monkeypatch):
    monkeypatch.setattr(eps, "MAX_PREVIEW_PIXELS", 3)
    assert eps.find_preview(dos_eps(palette_alpha_tiff(2, 2, PIXELS, COLORS))) is None


def test_loose_eps_files_get_thumbnails(settings, conn, asset_root: Path):
    from assetindex.scanner import scan_root
    from assetindex.web import create_app

    (asset_root / "logo.eps").write_bytes(dos_eps(palette_alpha_tiff(2, 2, PIXELS, COLORS)))
    (asset_root / "plain.eps").write_bytes(POSTSCRIPT)
    scan_root(conn, "art", asset_root)
    ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM files")}
    client = TestClient(create_app(settings))

    assert client.get(f"/api/files/{ids['logo.eps']}/thumb").status_code == 200
    assert client.get(f"/api/files/{ids['plain.eps']}/thumb").status_code == 415


def test_eps_cards_get_image_vectors(settings, conn, asset_root: Path):
    from assetindex.embed_stage import _has_image
    from assetindex.scanner import scan_root

    (asset_root / "logo.eps").write_bytes(dos_eps(palette_alpha_tiff(2, 2, PIXELS, COLORS)))
    scan_root(conn, "art", asset_root)
    row = conn.execute("SELECT f.*, NULL AS preview_state FROM files f WHERE name = 'logo.eps'").fetchone()
    assert _has_image(row)


def test_eps_inside_archives_feed_sheets():
    from assetindex.archive_previews import choose_candidates

    picked = choose_candidates([(1, "Vector/logo.eps", 1000), (2, "readme.txt", 10)], 9)
    assert [path for _, path in picked] == ["Vector/logo.eps"]
