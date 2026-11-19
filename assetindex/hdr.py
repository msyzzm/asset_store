"""Turn a Radiance .hdr (RGBE) into an ordinary picture for thumbnails and covers.

Pillow cannot read .hdr. Only the rows and columns a thumbnail needs are kept while the
scanlines are unpacked, then the light is mapped to screen brightness: exposure from
the picture's log-average luminance, Reinhard to bring the sun and sky into range.
"""

import io
import logging
import math
import re

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

MAX_HDR_BYTES = 128 * 1024**2
MAX_SIDE = 32_768
OUT_WIDTH = 640  # kept columns: twice a thumbnail, shrunk afterwards
HEADER_LIMIT = 64 * 1024
KEY = 0.18  # middle grey the average light is mapped to
_RESOLUTION = re.compile(rb"([-+])Y (\d+) \+X (\d+)")


class _NotHdr(ValueError):
    pass


def render_hdr(data: bytes) -> bytes | None:
    """PNG bytes of the picture, or None if it is not a readable .hdr."""
    try:
        rgbe = _decode(data)
    except _NotHdr as err:
        log.debug("cannot read hdr: %s", err)
        return None
    image = Image.fromarray(tone_map(_light(rgbe)), "RGB")
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def _decode(data: bytes) -> np.ndarray:
    """Kept pixels as (rows, cols, 4) RGBE bytes."""
    if not data.startswith((b"#?RADIANCE", b"#?RGBE")):
        raise _NotHdr("not a Radiance file")
    end = data.find(b"\n\n", 0, HEADER_LIMIT)
    if end < 0:
        raise _NotHdr("header never ends")
    header = data[:end]
    if b"FORMAT=" in header and b"FORMAT=32-bit_rle_rgbe" not in header:
        raise _NotHdr("not RGBE (XYZE?)")
    line_end = data.find(b"\n", end + 2, end + 2 + 64)
    match = _RESOLUTION.fullmatch(data[end + 2:line_end].strip()) if line_end > 0 else None
    if match is None:
        raise _NotHdr("resolution other than [-+]Y h +X w")
    flipped, height, width = match.group(1) == b"+", int(match.group(2)), int(match.group(3))
    if not (0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE):
        raise _NotHdr(f"size {width}x{height}")
    step = max(1, math.ceil(width / OUT_WIDTH))
    rows = _Scanlines(data, line_end + 1, width).read(height, step)
    picture = np.stack(rows)[:, ::step]
    return picture[::-1] if flipped else picture


class _Scanlines:
    def __init__(self, data: bytes, pos: int, width: int):
        self.data, self.pos, self.width = data, pos, width

    def read(self, height: int, step: int) -> list[np.ndarray]:
        """Every `step`-th scanline; the ones between are only walked past."""
        return [line for y in range(height) if (line := self._line(keep=y % step == 0)) is not None]

    def _line(self, keep: bool) -> np.ndarray | None:
        data, pos, width = self.data, self.pos, self.width
        head = data[pos:pos + 4]
        if len(head) < 4:
            raise _NotHdr("file ends early")
        if 8 <= width < 0x8000 and head[0] == 2 and head[1] == 2 and (head[2] << 8 | head[3]) == width:
            self.pos = pos + 4
            channels = [self._channel(keep) for _ in range(4)]
            return np.stack(channels, axis=1) if keep else None
        # Flat RGBE (old files): four bytes a pixel.
        size = width * 4
        if pos + size > len(data):
            raise _NotHdr("file ends early")
        self.pos = pos + size
        return np.frombuffer(data, np.uint8, size, pos).reshape(width, 4) if keep else None

    def _channel(self, keep: bool) -> np.ndarray | None:
        """One channel of a run-length scanline: a count above 128 repeats the next byte."""
        data, pos, width = self.data, self.pos, self.width
        out = bytearray(width) if keep else None
        x = 0
        while x < width:
            if pos >= len(data):
                raise _NotHdr("file ends early")
            count = data[pos]
            pos += 1
            if count > 128:
                count -= 128
                if x + count > width or pos >= len(data):
                    raise _NotHdr("run past the scanline")
                if keep:
                    out[x:x + count] = data[pos:pos + 1] * count
                pos += 1
            else:
                if count == 0 or x + count > width or pos + count > len(data):
                    raise _NotHdr("bad run")
                if keep:
                    out[x:x + count] = data[pos:pos + count]
                pos += count
            x += count
        self.pos = pos
        return np.frombuffer(out, np.uint8) if keep else None


def _light(rgbe: np.ndarray) -> np.ndarray:
    exponent = rgbe[..., 3].astype(np.int32)
    return np.where(exponent[..., None] > 0,
                    np.ldexp(rgbe[..., :3].astype(np.float32) + 0.5, exponent[..., None] - 136), 0.0)


def tone_map(light: np.ndarray) -> np.ndarray:
    """Linear light (rows, cols, 3) as 8-bit screen colours."""
    luminance = light @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    average = float(np.exp(np.mean(np.log(luminance + 1e-6))))
    scaled = light * (KEY / max(average, 1e-6))
    mapped = scaled / (1 + scaled)
    return np.clip(np.power(mapped, 1 / 2.2) * 255 + 0.5, 0, 255).astype(np.uint8)
