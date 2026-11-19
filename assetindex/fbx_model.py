"""Read a binary .fbx for a thumbnail: its meshes placed as the scene places them, drawn
by mesh_render. ASCII FBX is not read (none in the samples: 3728 files, all binary,
versions 6100, 7400, 7500 and 7700).

Only the nodes a picture needs are parsed (GlobalSettings, Objects' Geometry and Model,
Connections); animation curves, textures and the rest are skipped by their end offset,
and arrays are only inflated when read.
"""

import logging
import math
import struct
import zlib
from dataclasses import dataclass, field

import numpy as np

from assetindex.mesh_render import MAX_FACES, MAX_VERTICES, NotAModel, render_mesh

log = logging.getLogger(__name__)

MAX_FBX_BYTES = 64 * 1024**2  # characters with embedded textures are big; the mesh is a fraction
MAX_ARRAY_BYTES = 64 * 1024**2  # one inflated array
MAX_DEPTH = 32
# FBX 6 lists a mesh's numbers one property each: three per vertex.
MAX_PROPS = 3 * MAX_VERTICES
MAGIC = b"Kaydara FBX Binary  \x00"
HEADER_BYTES = 27
WANTED = frozenset({
    "GlobalSettings", "Objects", "Connections", "Geometry", "Model", "Vertices", "PolygonVertexIndex",
    "Properties70", "Properties60", "P", "Property", "C", "Connect",
})
_SCALARS = {b"Y": "<h", b"I": "<i", b"F": "<f", b"D": "<d", b"L": "<q"}
_ARRAYS = {b"f": np.dtype("<f4"), b"d": np.dtype("<f8"), b"l": np.dtype("<i8"), b"i": np.dtype("<i4"), b"b": np.dtype("u1")}


@dataclass
class _Array:
    dtype: np.dtype
    count: int
    encoding: int
    blob: bytes

    def values(self) -> np.ndarray:
        size = self.count * self.dtype.itemsize
        if size > MAX_ARRAY_BYTES:
            raise NotAModel(f"array of {size} bytes")
        if self.encoding == 0:
            raw = self.blob
        elif self.encoding == 1:
            try:
                raw = zlib.decompressobj().decompress(self.blob, size)  # never more than it claims
            except zlib.error as err:
                raise NotAModel(f"bad array: {err}") from None
        else:
            raise NotAModel(f"array encoding {self.encoding}")
        if len(raw) != size:
            raise NotAModel("array shorter than it claims")
        return np.frombuffer(raw, self.dtype)


@dataclass
class _Node:
    name: str
    props: list
    children: list["_Node"] = field(default_factory=list)

    def child(self, name: str) -> "_Node | None":
        return next((c for c in self.children if c.name == name), None)


def render_fbx(data: bytes) -> bytes | None:
    """PNG bytes of the model, or None if there is nothing to draw (or too much)."""
    try:
        return render_mesh(*_scene_mesh(_read(data)))
    except NotAModel as err:
        log.debug("cannot draw fbx: %s", err)
        return None
    except (struct.error, IndexError, ValueError, TypeError, OverflowError) as err:  # a malformed file
        log.debug("cannot read fbx: %r", err)
        return None


def _read(data: bytes) -> list[_Node]:
    if not data.startswith(MAGIC) or len(data) < HEADER_BYTES:
        raise NotAModel("not a binary FBX")
    version = struct.unpack_from("<I", data, 23)[0]
    return _Reader(data, wide=version >= 7500).nodes(HEADER_BYTES, len(data), 0)


class _Reader:
    def __init__(self, data: bytes, wide: bool):
        self.data = data
        self.header = struct.Struct("<QQQB" if wide else "<IIIB")

    def nodes(self, pos: int, end: int, depth: int) -> list[_Node]:
        if depth > MAX_DEPTH:
            raise NotAModel("nodes nested too deep")
        found: list[_Node] = []
        while pos + self.header.size <= end:
            node_end, prop_count, prop_bytes, name_len = self.header.unpack_from(self.data, pos)
            if node_end == 0:
                break  # the null record closing a list
            if not pos < node_end <= end:
                raise NotAModel("node runs past its parent")
            name_at = pos + self.header.size
            name = self.data[name_at:name_at + name_len].decode("ascii", "replace")
            if name in WANTED:
                if prop_count > MAX_PROPS:
                    raise NotAModel(f"node {name} with {prop_count} properties")
                props_at = name_at + name_len
                props = self._props(props_at, prop_count, node_end)
                children_at = props_at + prop_bytes
                found.append(_Node(name, props, self.nodes(children_at, node_end, depth + 1) if children_at < node_end else []))
            pos = node_end
        return found

    def _props(self, pos: int, count: int, end: int) -> list:
        data, props = self.data, []
        for _ in range(count):
            if pos >= end:
                raise NotAModel("properties run past their node")
            code = data[pos:pos + 1]
            pos += 1
            if code in _SCALARS:
                fmt = _SCALARS[code]
                props.append(struct.unpack_from(fmt, data, pos)[0])
                pos += struct.calcsize(fmt)
            elif code == b"C":
                props.append(bool(data[pos]))
                pos += 1
            elif code in _ARRAYS:
                count_, encoding, size = struct.unpack_from("<III", data, pos)
                pos += 12
                props.append(_Array(_ARRAYS[code], count_, encoding, data[pos:pos + size]))
                pos += size
            elif code in (b"S", b"R"):
                size = struct.unpack_from("<I", data, pos)[0]
                pos += 4
                raw = data[pos:pos + size]
                props.append(raw.decode("utf-8", "replace") if code == b"S" else raw)
                pos += size
            else:
                raise NotAModel(f"unknown property type {code!r}")
        return props


def _settings(node: _Node) -> dict[str, list[float]]:
    """A node's Properties70 (7.x) or Properties60 (6.x): name -> its numbers."""
    block = node.child("Properties70") or node.child("Properties60")
    found: dict[str, list[float]] = {}
    for p in block.children if block else []:
        if p.props and isinstance(p.props[0], str):
            found[p.props[0]] = [v for v in p.props[1:] if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return found


def _vector(settings: dict, name: str, default: float) -> list[float]:
    values = settings.get(name, [])
    return values[-3:] if len(values) >= 3 else [default] * 3


# RotationOrder 0..5: the axes in the order they are applied (0, XYZ, is the default).
ROTATION_ORDERS = ("xyz", "xzy", "yzx", "yxz", "zxy", "zyx")


def _rotation(degrees: list[float], order: str = "xyz") -> np.ndarray:
    """Euler angles in degrees, the first axis of `order` applied first."""
    x, y, z = (math.radians(d) for d in degrees)
    turns = {
        "x": np.array([[1, 0, 0], [0, math.cos(x), -math.sin(x)], [0, math.sin(x), math.cos(x)]]),
        "y": np.array([[math.cos(y), 0, math.sin(y)], [0, 1, 0], [-math.sin(y), 0, math.cos(y)]]),
        "z": np.array([[math.cos(z), -math.sin(z), 0], [math.sin(z), math.cos(z), 0], [0, 0, 1]]),
    }
    matrix = np.eye(3)
    for axis in order:
        matrix = turns[axis] @ matrix
    return matrix


def _trs(translation: list[float], rotation: np.ndarray, scale: list[float]) -> np.ndarray:
    matrix = np.eye(4)
    matrix[:3, :3] = rotation @ np.diag(scale)
    matrix[:3, 3] = translation
    return matrix


def _local(settings: dict) -> np.ndarray:
    """Translation, pre-rotation, rotation, scale; pivots and offsets are left out."""
    order = int((settings.get("RotationOrder") or [0])[-1])
    order_name = ROTATION_ORDERS[order] if 0 <= order < len(ROTATION_ORDERS) else "xyz"
    rotation = _rotation(_vector(settings, "PreRotation", 0)) @ _rotation(_vector(settings, "Lcl Rotation", 0), order_name)
    return _trs(_vector(settings, "Lcl Translation", 0), rotation, _vector(settings, "Lcl Scaling", 1))


def _geometric(settings: dict) -> np.ndarray:
    """The offset 3ds Max puts between a model and its mesh."""
    return _trs(_vector(settings, "GeometricTranslation", 0), _rotation(_vector(settings, "GeometricRotation", 0)),
                _vector(settings, "GeometricScaling", 1))


def _triangles(polygon_vertex_index: np.ndarray) -> np.ndarray:
    """Fans of each polygon; a negative index (bitwise not) closes a polygon."""
    ends = np.flatnonzero(polygon_vertex_index < 0)
    corners = np.where(polygon_vertex_index < 0, ~polygon_vertex_index, polygon_vertex_index).astype(np.int64)
    starts = np.concatenate(([0], ends[:-1] + 1))
    fans = np.maximum(ends - starts - 1, 0)  # a polygon of n corners makes n - 2 triangles
    total = int(fans.sum())
    if total > MAX_FACES:
        raise NotAModel(f"more than {MAX_FACES} triangles")
    first = np.repeat(starts, fans)
    step = np.arange(total) - np.repeat(np.cumsum(fans) - fans, fans) + 1
    return np.stack([corners[first], corners[first + step], corners[first + step + 1]], axis=1)


def _scene_mesh(top: list[_Node]) -> tuple[np.ndarray, np.ndarray]:
    by_name = {n.name: n for n in top}
    objects = by_name.get("Objects")
    if objects is None:
        raise NotAModel("no objects")
    models = {m.props[0]: m for m in objects.children if m.name == "Model" and m.props}
    parents: dict = {}
    for c in by_name["Connections"].children if "Connections" in by_name else []:
        if len(c.props) >= 3 and c.props[0] == "OO":
            parents.setdefault(c.props[1], []).append(c.props[2])
    worlds: dict = {}

    def world(key, seen=()) -> np.ndarray:
        if key in worlds:
            return worlds[key]
        model = models.get(key)
        if model is None or key in seen:
            return np.eye(4)
        up = next((p for p in parents.get(key, []) if p in models), None)
        matrix = (world(up, (*seen, key)) if up is not None else np.eye(4)) @ _local(_settings(model))
        worlds[key] = matrix
        return matrix

    pieces: list[tuple[np.ndarray, np.ndarray]] = []
    # 7.x: meshes are Geometry nodes, connected to the models that place them;
    # 6.x: a model holds its mesh itself.
    for node in objects.children:
        if node.child("Vertices") is None or node.child("PolygonVertexIndex") is None or not node.props:
            continue
        if node.name == "Geometry":
            placed = [p for p in parents.get(node.props[0], []) if p in models] or [None]
            transforms = [world(p) @ _geometric(_settings(models[p])) if p is not None else np.eye(4) for p in placed]
        else:
            transforms = [world(node.props[0]) @ _geometric(_settings(node))]
        pieces.extend((_points(node), transform) for transform in transforms)
    # 7.x keeps GlobalSettings at the top, 6.x inside Objects.
    return _merge(pieces, _up_axis(by_name.get("GlobalSettings") or objects.child("GlobalSettings")))


def _numbers(props: list) -> np.ndarray:
    """7.x: one array property; 6.x: a property per number."""
    if len(props) == 1 and isinstance(props[0], _Array):
        return props[0].values()
    if not props or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in props):
        raise NotAModel("mesh without numbers")
    return np.array(props)


def _points(node: _Node) -> tuple[np.ndarray, np.ndarray]:
    points = _numbers(node.child("Vertices").props).astype(np.float64)
    if len(points) % 3:
        raise NotAModel("vertices not in threes")
    return points.reshape(-1, 3), _triangles(_numbers(node.child("PolygonVertexIndex").props).astype(np.int64))


def _merge(pieces, up: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if not pieces:
        raise NotAModel("no meshes")
    points, faces, offset = [], [], 0
    for (local, triangles), transform in pieces:
        if offset + len(local) > MAX_VERTICES or sum(len(f) for f in faces) + len(triangles) > MAX_FACES:
            raise NotAModel("too big a scene")
        placed = local @ transform[:3, :3].T + transform[:3, 3]
        points.append(placed @ up.T)
        faces.append(triangles + offset)
        offset += len(local)
    return np.concatenate(points), np.concatenate(faces)


def _up_axis(settings_node: _Node | None) -> np.ndarray:
    """Turns the scene so its up axis is y, as the renderer expects."""
    settings = _settings(settings_node) if settings_node else {}
    axis = int((settings.get("UpAxis") or [1])[-1])
    sign = 1 if (settings.get("UpAxisSign") or [1])[-1] >= 0 else -1
    if axis == 2:  # z up: z becomes y, y becomes -z
        return np.array([[1, 0, 0], [0, 0, sign], [0, -sign, 0]], dtype=np.float64)
    if axis == 0:
        return np.array([[0, -sign, 0], [sign, 0, 0], [0, 0, 1]], dtype=np.float64)
    return np.diag([1.0, sign, sign])
