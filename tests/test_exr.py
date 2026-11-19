import io
from pathlib import Path

import numpy as np
import OpenEXR
from PIL import Image

from assetindex import exr
from assetindex.exr import render_exr


def make_exr(tmp_path: Path, channels: dict, name="t.exr") -> bytes:
    """An EXR written by the OpenEXR library itself, as bytes."""
    header = {"compression": OpenEXR.ZIP_COMPRESSION, "type": OpenEXR.scanlineimage}
    path = tmp_path / name
    with OpenEXR.File(header, {k: np.ascontiguousarray(v, dtype=np.float32) for k, v in channels.items()}) as f:
        f.write(str(path))
    return path.read_bytes()


def _open(data):
    return Image.open(io.BytesIO(data)).convert("RGB")


def test_a_texture_map_is_shown_as_it_is(tmp_path):
    normal = np.zeros((8, 16, 3))
    normal[...] = (0.5, 0.5, 1.02)  # a little over 1 from filtering, like Poly Haven normal maps

    assert _open(render_exr(make_exr(tmp_path, {"RGB": normal}))).getpixel((4, 4)) == (128, 128, 255)


def test_light_brighter_than_white_is_tone_mapped(tmp_path):
    sky = np.zeros((8, 16, 3))
    sky[:4] = (20.0, 30.0, 50.0)  # sky far brighter than white
    sky[4:] = (0.5, 0.4, 0.3)

    image = _open(render_exr(make_exr(tmp_path, {"RGB": sky})))

    top, bottom = image.getpixel((8, 1)), image.getpixel((8, 6))
    assert max(top) < 255 and sum(top) > sum(bottom)  # the sky is bright but not blown out


def test_one_channel_is_grey_and_layered_names_are_found(tmp_path):
    rough = make_exr(tmp_path, {"Y": np.full((8, 8), 0.25)})
    layered = make_exr(tmp_path, {f"diffuse.{c}": np.full((8, 8), v) for c, v in zip("RGB", (1.0, 0.0, 0.0))}, "l.exr")

    assert _open(render_exr(rough)).getpixel((2, 2)) == (64, 64, 64)
    assert _open(render_exr(layered)).getpixel((2, 2)) == (255, 0, 0)


def test_too_big_or_broken_files_have_no_picture(tmp_path, monkeypatch):
    data = make_exr(tmp_path, {"Y": np.full((8, 8), 0.5)})
    assert render_exr(data[:60]) is None  # cut short
    assert render_exr(b"not an exr") is None
    monkeypatch.setattr(exr, "MAX_EXR_PIXELS", 10)
    assert render_exr(data) is None
