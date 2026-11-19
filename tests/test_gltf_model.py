import base64
import json
import struct

import numpy as np

from assetindex import gltf_model
from assetindex.gltf_model import render_gltf

POINTS = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype="<f4")
INDICES = np.array([0, 1, 2, 0, 1, 3, 0, 2, 3, 1, 2, 3], dtype="<u2")


def _document(buffer: dict, node: dict | None = None, extra_primitive: dict | None = None) -> dict:
    primitives = [{"attributes": {"POSITION": 0}, "indices": 1}]
    if extra_primitive:
        primitives.insert(0, extra_primitive)
    return {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [node or {"mesh": 0}],
        "meshes": [{"primitives": primitives}],
        "buffers": [buffer],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": POINTS.nbytes},
            {"buffer": 0, "byteOffset": POINTS.nbytes, "byteLength": INDICES.nbytes},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": 4, "type": "VEC3"},
            {"bufferView": 1, "componentType": 5123, "count": 12, "type": "SCALAR"},
        ],
    }


def _glb(document: dict, binary: bytes) -> bytes:
    text = json.dumps(document).encode()
    text += b" " * (-len(text) % 4)
    binary += b"\0" * (-len(binary) % 4)
    body = struct.pack("<II", len(text), 0x4E4F534A) + text + struct.pack("<II", len(binary), 0x004E4942) + binary
    return b"glTF" + struct.pack("<II", 2, 12 + len(body)) + body


BINARY = POINTS.tobytes() + INDICES.tobytes()


def test_a_glb_is_drawn():
    assert render_gltf(_glb(_document({"byteLength": len(BINARY)}), BINARY)) is not None


def test_a_gltf_with_its_data_inside_is_drawn():
    uri = "data:application/octet-stream;base64," + base64.b64encode(BINARY).decode()

    assert render_gltf(json.dumps(_document({"uri": uri, "byteLength": len(BINARY)})).encode()) is not None


def test_nodes_place_their_meshes():
    document = _document({"byteLength": len(BINARY)}, {"children": [1]})
    document["nodes"].append({"mesh": 0, "translation": [5, 0, 0], "scale": [2, 2, 2]})

    points, faces = gltf_model._scene(document, [BINARY])

    assert points[:, 0].min() == 5 and points[:, 0].max() == 7 and len(faces) == 4


def test_primitives_it_cannot_read_are_skipped():
    draco = {"attributes": {"POSITION": 2}, "extensions": {"KHR_draco_mesh_compression": {}}}
    document = _document({"byteLength": len(BINARY)}, extra_primitive=draco)
    document["accessors"].append({"componentType": 5126, "count": 4, "type": "VEC3"})  # no bufferView: in Draco

    assert render_gltf(_glb(document, BINARY)) is not None


def test_missing_or_broken_data_has_no_picture():
    assert render_gltf(json.dumps(_document({"uri": "model.bin", "byteLength": len(BINARY)})).encode()) is None
    assert render_gltf(_glb(_document({"byteLength": len(BINARY)}), BINARY[:20])) is None  # cut short
    assert render_gltf(b"glTF\x02\0\0\0") is None
    assert render_gltf(b"not json") is None


def test_shared_children_cannot_blow_up_the_walk():
    """Each node lists the next one twice: 2**40 paths through 41 nodes."""
    document = _document({"byteLength": len(BINARY)})
    document["nodes"] = [{"children": [i + 1, i + 1]} for i in range(40)] + [{"mesh": 0}]

    assert render_gltf(_glb(document, BINARY)) is None


def test_negative_counts_and_deep_nesting_have_no_picture():
    document = _document({"byteLength": len(BINARY)})
    document["accessors"][1]["count"] = -1

    assert render_gltf(_glb(document, BINARY)) is None
    assert render_gltf(b"[" * 100_000) is None  # nesting deeper than Python recurses
