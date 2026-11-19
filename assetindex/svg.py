"""Draw an .svg as a PNG for thumbnails and archive covers (resvg: no browser needed)."""

import io
import logging

import resvg_py
from PIL import Image

log = logging.getLogger(__name__)

MAX_SVG_BYTES = 5 * 1024**2
# Fit inside this box, keeping the shape: a 1x100000 drawing must not become 32M pixels.
RENDER_BOX = 320
# Icon sets such as game-icons.net draw white on transparent: on the light page they
# would vanish, so art that is nearly all light gets this backdrop.
BACKDROP = (48, 50, 54, 255)
LIGHT_LEVEL = 200  # average brightness (0-255) of the drawn pixels above which art counts as light
DRAWN_ALPHA = 128


def render_svg(data: bytes) -> bytes | None:
    """PNG bytes of the drawing, or None if it is not a drawable SVG."""
    try:
        text = data.decode("utf-8-sig")
        # System fonts skipped: loading them for every icon is slow, and icon sets are paths.
        png = resvg_py.svg_to_bytes(svg_string=text, width=RENDER_BOX, height=RENDER_BOX, skip_system_fonts=True)
    except (UnicodeDecodeError, ValueError) as err:
        log.debug("cannot draw svg: %r", err)
        return None
    except BaseException as err:  # a Rust panic arrives as pyo3's PanicException, not an Exception
        if type(err).__name__ != "PanicException":
            raise
        log.warning("resvg failed on an svg: %r", err)
        return None
    with Image.open(io.BytesIO(png)) as im:
        image = im.convert("RGBA")
    if image.getchannel("A").getbbox() is None:
        return None  # draws nothing visible: no cover rather than a blank one
    if not _is_light_on_nothing(image):
        return png
    backed = Image.new("RGBA", image.size, BACKDROP)
    backed.alpha_composite(image)
    out = io.BytesIO()
    backed.save(out, "PNG")
    return out.getvalue()


def _is_light_on_nothing(image: Image.Image) -> bool:
    drawn = image.getchannel("A").point(lambda a: 255 if a >= DRAWN_ALPHA else 0)
    if drawn.getbbox() is None:
        return False
    histogram = image.convert("L").histogram(mask=drawn)
    return sum(level * n for level, n in enumerate(histogram)) / sum(histogram) > LIGHT_LEVEL
