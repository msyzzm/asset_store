import io

from PIL import Image

from assetindex import obj_model
from assetindex.obj_model import render_obj

# A unit cube: quads, a comment, texture/normal indices and a negative (relative) index.
CUBE = b"""# cube
mtllib cube.mtl
v 0 0 0
v 1 0 0
v 1 1 0
v 0 1 0
v 0 0 1
v 1 0 1
v 1 1 1
v 0 1 1
vt 0 0
vn 0 0 1
f 1/1/1 2/1/1 3/1/1 4/1/1
f 5 6 7 8
f 1 2 6 5
f 2 3 7 6
f 3 4 8 7
f -4 -8 -5 -1
"""


def _open(data):
    return Image.open(io.BytesIO(data)).convert("RGBA")


def test_a_model_is_drawn_shaded_on_a_clear_background():
    image = _open(render_obj(CUBE))

    assert image.size == (320, 320)
    assert image.getpixel((1, 1))[3] == 0
    box = image.getchannel("A").getbbox()
    assert box is not None and box[2] - box[0] > 200  # fills most of the picture
    shades = {image.getpixel((x, y))[:3] for x in range(0, 320, 8) for y in range(0, 320, 8) if image.getpixel((x, y))[3] == 255}
    assert len(shades) >= 2  # faces turned differently are lit differently


def test_nothing_to_draw_has_no_picture():
    assert render_obj(b"") is None
    assert render_obj(b"v 0 0 0\nv 1 1 1\nl 1 2\n") is None  # lines only
    assert render_obj(b"v 0 0 0\nv 1 0 0\nv 2 0 0\nf 1 2 3\n") is None  # flat to a line
    assert render_obj(b"v 0 0 0\nf 1 2 9\n") is None  # indices past the vertices
    assert render_obj(b"v a b c\nf x y z\n") is None


def test_huge_models_are_not_drawn(monkeypatch):
    monkeypatch.setattr(obj_model, "MAX_FACES", 5)

    assert render_obj(CUBE) is None  # 6 quads = 12 triangles


def test_far_out_or_endless_input_has_no_picture(monkeypatch):
    far = b"v 0 0 0\nv 1e200 0 0\nv 0 1e200 0\nv 0 0 1e200\nf 1 2 3\nf 1 2 4\n"
    assert render_obj(far) is None
    monkeypatch.setattr(obj_model, "MAX_VERTICES", 4)
    assert render_obj(CUBE) is None  # 8 vertices
    monkeypatch.setattr(obj_model, "MAX_VERTICES", 100)
    monkeypatch.setattr(obj_model, "MAX_FACES", 3)
    assert render_obj(b"v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\nv 1 1 1\nv 2 2 0\nf 1 2 3 4 5 6\n") is None  # one face, 4 triangles
