import struct
import zlib

import numpy as np

from assetindex import fbx_model
from assetindex.fbx_model import render_fbx

CUBE_POINTS = [0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0, 0, 0, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1]
# Quads; the last corner of each is stored as ~index (negative).
CUBE_POLYGONS = [0, 1, 2, ~3, 4, 5, 6, ~7, 0, 1, 5, ~4, 1, 2, 6, ~5, 2, 3, 7, ~6, 3, 0, 4, ~7]


def _prop(value) -> bytes:
    if isinstance(value, str):
        raw = value.encode()
        return b"S" + struct.pack("<I", len(raw)) + raw
    if isinstance(value, float):
        return b"D" + struct.pack("<d", value)
    if isinstance(value, int):
        return b"L" + struct.pack("<q", value)
    kind, values, packed = value  # ("d" | "i", [...], compress?)
    raw = np.array(values, dtype="<f8" if kind == "d" else "<i4").tobytes()
    blob = zlib.compress(raw) if packed else raw
    return kind.encode() + struct.pack("<III", len(values), int(packed), len(blob)) + blob


def _node(name: str, props=(), children=(), wide=False, at=0, ints=False) -> bytes:
    """One node record at offset `at`; `ints` stores ints as I (FBX 6 style indices)."""
    head = struct.Struct("<QQQB" if wide else "<IIIB")
    body = b"".join(b"I" + struct.pack("<i", p) if ints and isinstance(p, int) else _prop(p) for p in props)
    start = at + head.size + len(name) + len(body)
    kids = b""
    for child in children:
        kids += child(start + len(kids))
    if children:
        kids += b"\0" * head.size  # null record closing the list
    end = start + len(kids)
    return head.pack(end, len(props), len(body), len(name)) + name.encode() + body + kids


def _fbx(version: int, nodes) -> bytes:
    wide = version >= 7500
    data = b"Kaydara FBX Binary  \x00\x1a\x00" + struct.pack("<I", version)
    for make in nodes:
        data += make(len(data), wide)
    return data + b"\0" * (25 if wide else 13)


def n(name, props=(), children=(), ints=False):
    """A node to place later, once its offset and the header width are known."""
    return lambda at, wide=False: _node(name, props, [lambda a, c=c: c(a, wide) for c in children], wide, at, ints)


def p70(**vectors):
    return n("Properties70", children=[n("P", (k.replace("_", " "), k, "", "A", *map(float, v))) for k, v in vectors.items()])


def cube_scene(version=7400, model_settings=None, up_axis=1, packed=True):
    return _fbx(version, [
        n("GlobalSettings", children=[n("Properties70", children=[n("P", ("UpAxis", "int", "Integer", "", up_axis))])]),
        n("Objects", children=[
            n("Geometry", (100, "Cube\x00\x01Geometry", "Mesh"), [
                n("Vertices", [("d", CUBE_POINTS, packed)]),
                n("PolygonVertexIndex", [("i", CUBE_POLYGONS, packed)]),
            ]),
            n("Model", (200, "Cube\x00\x01Model", "Mesh"), [model_settings or p70()]),
        ]),
        n("Connections", children=[n("C", ("OO", 100, 200)), n("C", ("OO", 200, 0))]),
    ])


def test_a_mesh_is_drawn_from_versions_7400_and_7500():
    assert render_fbx(cube_scene(7400)) is not None
    assert render_fbx(cube_scene(7500, packed=False)) is not None


def test_models_place_their_meshes():
    moved = fbx_model._scene_mesh(fbx_model._read(cube_scene(model_settings=p70(Lcl_Translation=(10, 0, 0)))))

    points, faces = moved
    assert points[:, 0].min() == 10 and len(faces) == 12  # six quads, two triangles each


def test_a_z_up_scene_is_stood_up():
    points, _ = fbx_model._scene_mesh(fbx_model._read(cube_scene(
        model_settings=p70(Lcl_Scaling=(1, 1, 5)), up_axis=2)))

    assert np.ptp(points[:, 1]) == 5  # the tall z became y


def test_fbx6_keeps_the_mesh_in_the_model_one_number_at_a_time():
    data = _fbx(6100, [n("Objects", children=[
        n("Model", ("Cube\x00\x01Model", "Mesh"), [
            n("Vertices", [float(v) for v in CUBE_POINTS]),
            n("PolygonVertexIndex", CUBE_POLYGONS, ints=True),
        ]),
    ])])

    assert render_fbx(data) is not None


def test_files_without_a_mesh_or_broken_have_no_picture(monkeypatch):
    animation = _fbx(7400, [n("Objects", children=[n("Model", (1, "Hips\x00\x01Model", "LimbNode"))])])
    assert render_fbx(animation) is None
    assert render_fbx(b"; FBX 7.4.0 project file\n") is None  # ASCII
    assert render_fbx(cube_scene()[:200]) is None  # cut short
    monkeypatch.setattr(fbx_model, "MAX_ARRAY_BYTES", 64)
    assert render_fbx(cube_scene()) is None  # an array bigger than allowed is never inflated


def test_an_array_claiming_less_than_it_holds_is_refused():
    data = cube_scene(packed=True)
    at = data.index(b"Vertices") + len(b"Vertices") + 1  # the array's element count
    lying = data[:at] + struct.pack("<I", 3) + data[at + 4:]

    assert render_fbx(lying) is None


def test_a_model_turns_in_its_own_rotation_order():
    """Rokoko's skeletons: rotation (90, 0, 180) in order ZXY stands upright; read as XYZ it hangs upside down."""
    settings = n("Properties70", children=[
        n("P", ("RotationOrder", "enum", "", "", 4)),
        n("P", ("Lcl Rotation", "Lcl Rotation", "", "A", 90.0, 0.0, 180.0)),
        n("P", ("Lcl Scaling", "Lcl Scaling", "", "A", 1.0, 1.0, 5.0)),
    ])
    points, _ = fbx_model._scene_mesh(fbx_model._read(cube_scene(model_settings=settings)))

    # z (the tall side) goes to -y by Z then X, then turning about Y keeps it there.
    assert points[:, 1].max() <= 1e-9 and points[:, 1].min() == -5
    turned_xyz = fbx_model._rotation([90, 0, 180]) @ np.array([0, 0, 5])
    assert turned_xyz[1] > 0  # the default order would have stood it the other way up


def test_an_empty_array_is_not_inflated_without_a_limit():
    bomb = zlib.compress(b"\0" * 10_000_000)
    array = fbx_model._Array(np.dtype("<f8"), 0, 1, bomb)

    assert len(array.values()) == 0


def test_an_array_longer_than_its_node_is_refused():
    data = cube_scene(packed=False)
    at = data.index(b"Vertices") + len(b"Vertices") + 1 + 8  # the array's byte size
    assert render_fbx(data[:at] + struct.pack("<I", 0xFFFFFF) + data[at + 4:]) is None
