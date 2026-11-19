"""Turn an OpenEXR image into an ordinary picture for thumbnails and covers.

Read with the OpenEXR library (all its compressions, DWAA included), which only opens
files on disk: the bytes go through a temporary file. Texture maps (roughness, normals:
values within 0..1) are shown as they are; lighting (values above 1) is tone-mapped
like an .hdr.
"""

import io
import logging
import math
import os
import tempfile

import numpy as np
import OpenEXR
from PIL import Image

from assetindex.hdr import OUT_WIDTH, tone_map

log = logging.getLogger(__name__)

MAX_EXR_BYTES = 128 * 1024**2
MAX_EXR_PIXELS = 16_000_000  # read whole as floats: 4096 x 4096 RGBA is 256 MB
MAGIC = b"\x76\x2f\x31\x01"
# A texture map, not light: its brightest values (99.5th percentile) stay about 1, a
# little over from filtering (a normal map's blue reaches 1.02).
DATA_RANGE = 1.05
SIGNED_BELOW = -0.05  # lower than this: signed data (-1..1), not rounding below 0


class _NotExr(ValueError):
    pass


def render_exr(data: bytes) -> bytes | None:
    """PNG bytes of the picture, or None if it cannot be read (or is too big)."""
    if not data.startswith(MAGIC):
        return None
    handle, name = tempfile.mkstemp(suffix=".exr")
    try:
        with os.fdopen(handle, "wb") as tmp:
            tmp.write(data)
        values = _read(name)
    except (_NotExr, RuntimeError, ValueError, KeyError, TypeError, IndexError) as err:
        log.debug("cannot read exr: %r", err)
        return None
    finally:
        os.unlink(name)
    image = Image.fromarray(_display(values), "RGB")
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def _read(name: str) -> np.ndarray:
    """The first part's colour as (rows, cols, 3) floats, every few rows and columns."""
    low, high = OpenEXR.File(name, header_only=True).header()["dataWindow"]
    width, height = int(high[0] - low[0] + 1), int(high[1] - low[1] + 1)
    if width <= 0 or height <= 0 or width * height > MAX_EXR_PIXELS:
        raise _NotExr(f"size {width}x{height}")
    channels = OpenEXR.File(name, separate_channels=True).parts[0].channels
    step = max(1, math.ceil(width / OUT_WIDTH))
    picked = _colour_channels(channels)
    planes = [np.asarray(channels[c].pixels, dtype=np.float32)[::step, ::step] for c in picked]
    return np.nan_to_num(np.stack(planes if len(planes) == 3 else planes * 3, axis=-1), posinf=0, neginf=0)


def _colour_channels(channels: dict) -> list[str]:
    """R, G, B (also "layer.R"), else one channel shown grey (Y, Z, a mask...)."""
    names = sorted(channels)
    for prefix in ("", *sorted({n.rsplit(".", 1)[0] + "." for n in names if "." in n})):
        rgb = [prefix + c for c in "RGB"]
        if all(c in channels for c in rgb):
            return rgb
    if "Y" in channels:
        return ["Y"]
    if not names:
        raise _NotExr("no channels")
    return [names[0]]


def _display(values: np.ndarray) -> np.ndarray:
    if float(np.percentile(values, 99.5)) <= DATA_RANGE and float(values.min()) >= -DATA_RANGE:
        # A texture map: show its values as they are (normals may hold -1..1).
        shown = (values + 1) / 2 if values.min() < SIGNED_BELOW else values
        return np.clip(shown * 255 + 0.5, 0, 255).astype(np.uint8)
    return tone_map(np.maximum(values, 0))
