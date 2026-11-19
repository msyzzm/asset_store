import io

from PIL import Image

from assetindex.svg import render_svg

WHITE_ICON = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><path fill="#fff" d="M64 64h384v384H64z"/></svg>'
DARK_ICON = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><path fill="#202020" d="M64 64h384v384H64z"/></svg>'


def _open(data):
    return Image.open(io.BytesIO(data)).convert("RGBA")


def test_an_svg_becomes_a_png():
    image = _open(render_svg(DARK_ICON))

    assert image.width == 320
    assert image.getpixel((160, 160))[:3] == (32, 32, 32)
    assert image.getpixel((2, 2))[3] == 0  # dark art keeps its transparent background


def test_white_art_on_nothing_gets_a_dark_backdrop():
    """game-icons.net draws white on transparent: on a light page it would be invisible."""
    image = _open(render_svg(WHITE_ICON))

    assert image.getpixel((160, 160))[:3] == (255, 255, 255)
    corner = image.getpixel((2, 2))
    assert corner[3] == 255 and max(corner[:3]) < 100


def test_a_wide_svg_keeps_its_shape():
    wide = b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="50"><rect width="200" height="50"/></svg>'

    image = _open(render_svg(wide))

    assert image.size == (320, 80)


def test_broken_svg_has_no_picture():
    assert render_svg(b"<svg") is None
    assert render_svg(b"\xff\xfe not xml") is None


def test_a_byte_order_mark_is_fine_and_an_empty_drawing_is_no_picture():
    assert render_svg(b"\xef\xbb\xbf" + DARK_ICON) is not None
    assert render_svg(b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"/>') is None
