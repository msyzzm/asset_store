import io

import pytest
from PIL import Image

from assetindex import thumbs
from assetindex.thumbs import open_image


def _encoded(fmt: str, size=(64, 64), **save) -> io.BytesIO:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, fmt, **save)
    buf.seek(0)
    return buf


@pytest.fixture
def tiny_limit(monkeypatch):
    # 64x64 = 4096 pixels: over this limit at full size, under it at 1/8 scale (8x8).
    monkeypatch.setattr(thumbs, "MAX_THUMB_SOURCE_PIXELS", 100)


def test_huge_jpeg_is_decoded_at_reduced_scale(tiny_limit):
    image = open_image(_encoded("JPEG"), (8, 8))
    assert image.size == (8, 8) and image.getpixel((4, 4))[0] > 150


def test_other_huge_images_are_still_refused(tiny_limit):
    with pytest.raises(ValueError, match="too large"):
        open_image(_encoded("PNG"), (8, 8))


def test_progressive_jpeg_gets_no_exception(tiny_limit):
    # libjpeg keeps a progressive file's full-size coefficients while decoding, whatever the scale.
    with pytest.raises(ValueError, match="too large"):
        open_image(_encoded("JPEG", progressive=True), (8, 8))


def test_reduced_scale_still_has_a_ceiling(monkeypatch):
    monkeypatch.setattr(thumbs, "MAX_THUMB_SOURCE_PIXELS", 10)  # even 8x8 = 64 is too many
    with pytest.raises(ValueError, match="too large"):
        open_image(_encoded("JPEG"), (8, 8))


def test_pillow_own_bomb_check_does_not_stop_big_jpegs_first():
    assert Image.MAX_IMAGE_PIXELS * 2 >= thumbs.MAX_JPEG_SOURCE_PIXELS
