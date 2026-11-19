"""Read a .glb (or a .gltf with its data inside) for a thumbnail: each mesh placed by the
scene's nodes, drawn by mesh_render. Buffers in other files, Draco-compressed meshes and
primitives other than triangles are left out.
"""

import base64
import json
import logging
import struct

import numpy as np

from assetindex.mesh_render import MAX_FACES, MAX_VERTICES, NotAModel, render_mesh

log = logging.getLogger(__name__)

MAX_GLTF_BYTES = 64 * 1024**2
MAX_NODE_DEPTH = 64
GLB_MAGIC = b"glTF"
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN = 0x004E4942
TRIANGLES = 4
_COMPONENTS = {5120: "i1", 5121: "u1", 5122: "<i2", 5123: "<u2", 5125: "<u4", 5126: "<f4"}
_WIDTH = {"SCALAR": 1, "VEC3": 3}


def render_gltf(data: bytes) -> bytes | None:
    """PNG bytes of the model, or None if there is nothing to draw (or too much)."""
    try:
        document, binary = _split(data)
        return render_mesh(*_scene(document, _buffers(document, binary)))
    except NotAModel as err:
        log.debug("cannot draw gltf: %s", err)
        return None
    except (struct.error, KeyError, IndexError, TypeError, ValueError, AttributeError) as err:  # malformed
        log.debug("cannot read gltf: %r", err)
        return None


def _split(data: bytes) -> tuple[dict, bytes | None]:
    """The JSON document, and a .glb's binary chunk."""
    if not data.startswith(GLB_MAGIC):
        return json.loads(data.decode("utf-8-sig")), None
    pos, document, binary = 12, None, None
    while pos + 8 <= len(data):
        size, kind = struct.unpack_from("<II", data, pos)
        chunk = data[pos + 8:pos + 8 + size]
        if kind == CHUNK_JSON and document is None:
            document = json.loads(chunk.decode("utf-8"))
        elif kind == CHUNK_BIN and binary is None:
            binary = chunk
        pos += 8 + size
    if not isinstance(document, dict):
        raise NotAModel("glb without its JSON")
    return document, binary


def _buffers(document: dict, binary: bytes | None) -> list[bytes | None]:
    """Each buffer's bytes; None where they sit in another file."""
    found: list[bytes | None] = []
    for buffer in document.get("buffers", []):
        uri = buffer.get("uri")
        if uri is None:
            found.append(binary)  # a .glb's own binary chunk
        elif uri.startswith("data:") and ";base64," in uri:
            found.append(base64.b64decode(uri.split(";base64,", 1)[1]))
        else:
            found.append(None)
    return found


def _accessor(document: dict, buffers: list, index: int, kind: str) -> np.ndarray:
    accessor = document["accessors"][index]
    if accessor.get("type") != kind or "bufferView" not in accessor or accessor.get("sparse"):
        raise NotAModel("an accessor this reader skips")
    view = document["bufferViews"][accessor["bufferView"]]
    data = buffers[view["buffer"]]
    if data is None:
        raise NotAModel("data in another file")
    dtype = np.dtype(_COMPONENTS[accessor["componentType"]])
    width, count = _WIDTH[kind], int(accessor["count"])
    if count > 3 * MAX_VERTICES:
        raise NotAModel("accessor too long")
    start = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
    stride = int(view.get("byteStride") or dtype.itemsize * width)
    end = start + stride * (count - 1) + dtype.itemsize * width if count else start
    if start < 0 or end > len(data) or stride < dtype.itemsize * width:
        raise NotAModel("accessor runs past its buffer")
    rows = np.lib.stride_tricks.as_strided(
        np.frombuffer(data, np.uint8, end - start, start), (count, dtype.itemsize * width), (stride, 1))
    return np.frombuffer(rows.copy().tobytes(), dtype).reshape(count, width)


def _local(node: dict) -> np.ndarray:
    if "matrix" in node:
        return np.array(node["matrix"], dtype=np.float64).reshape(4, 4).T  # stored column by column
    x, y, z, w = node.get("rotation", [0, 0, 0, 1])
    rotation = np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])
    matrix = np.eye(4)
    matrix[:3, :3] = rotation @ np.diag(node.get("scale", [1, 1, 1]))
    matrix[:3, 3] = node.get("translation", [0, 0, 0])
    return matrix


def _scene(document: dict, buffers: list) -> tuple[np.ndarray, np.ndarray]:
    nodes = document.get("nodes", [])
    scenes = document.get("scenes", [])
    if scenes:
        roots = scenes[document.get("scene", 0)].get("nodes", [])
    else:  # no scene: every node nobody holds
        held = {c for n in nodes for c in n.get("children", [])}
        roots = [i for i in range(len(nodes)) if i not in held]
    points, faces, offset = [], [], 0
    stack = [(i, np.eye(4), 0) for i in roots]
    while stack:
        index, parent, depth = stack.pop()
        if depth > MAX_NODE_DEPTH:
            raise NotAModel("nodes nested too deep")
        node = nodes[index]
        world = parent @ _local(node)
        stack.extend((c, world, depth + 1) for c in node.get("children", []))
        if "mesh" not in node:
            continue
        for primitive in document["meshes"][node["mesh"]].get("primitives", []):
            piece = _primitive(document, buffers, primitive)
            if piece is None:
                continue
            local, triangles = piece
            if offset + len(local) > MAX_VERTICES or sum(map(len, faces)) + len(triangles) > MAX_FACES:
                raise NotAModel("too big a scene")
            points.append(local @ world[:3, :3].T + world[:3, 3])
            faces.append(triangles + offset)
            offset += len(local)
    if not points:
        raise NotAModel("no meshes")
    return np.concatenate(points), np.concatenate(faces)


def _primitive(document: dict, buffers: list, primitive: dict) -> tuple[np.ndarray, np.ndarray] | None:
    """A triangle primitive's points and faces; None for one this reader skips."""
    if primitive.get("mode", TRIANGLES) != TRIANGLES or "POSITION" not in primitive.get("attributes", {}):
        return None
    try:
        points = _accessor(document, buffers, primitive["attributes"]["POSITION"], "VEC3").astype(np.float64)
        if "indices" in primitive:
            corners = _accessor(document, buffers, primitive["indices"], "SCALAR").astype(np.int64).ravel()
        else:
            corners = np.arange(len(points))
    except NotAModel as err:  # e.g. Draco: other primitives may still draw
        log.debug("skipped a primitive: %s", err)
        return None
    if len(corners) % 3:
        raise NotAModel("triangles not in threes")
    return points, corners.reshape(-1, 3)
