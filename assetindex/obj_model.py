"""Draw a Wavefront .obj as a grey, lit model for thumbnails and archive covers.

A software render with numpy and Pillow (no GPU in the container): seen from the front
right and a little above, orthographic, two-sided light, painted far to near. Materials
and textures are ignored: the shape is what tells a wall from a barrel.
"""

import io
import logging
import math

import numpy as np
from PIL import Image, ImageDraw

log = logging.getLogger(__name__)

MAX_OBJ_BYTES = 16 * 1024**2
MAX_FACES = 100_000  # triangles: about 2 s to paint (measured 2026-11-18); more takes too long
MAX_VERTICES = 200_000  # a file of bare vertices would otherwise fill memory with lists
MAX_COORD = 1e12  # beyond this, squares overflow; no real model is that big
SIZE = 320
SUPERSAMPLE = 2  # drawn at twice the size, then shrunk: smooth edges
MARGIN = 0.06  # of the picture, on each side
YAW = math.radians(-35)  # turn the model to show its front and right side
PITCH = math.radians(25)  # and tip it to show its top
LIGHT = np.array([-0.4, 0.6, 0.7]) / np.linalg.norm([-0.4, 0.6, 0.7])
BASE_COLOR = np.array([206, 209, 215])
AMBIENT = 0.3


class _NotAModel(ValueError):
    pass


def render_obj(data: bytes) -> bytes | None:
    """PNG bytes of the model, or None if there is nothing to draw (or too much)."""
    try:
        vertices, triangles = _parse(data)
        image = _draw(vertices, triangles)
    except _NotAModel as err:
        log.debug("cannot draw obj: %s", err)
        return None
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def _parse(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    vertices: list[list[float]] = []
    triangles: list[tuple[int, int, int]] = []
    try:
        for line in data.decode("utf-8", "replace").splitlines():
            if line.startswith("v "):
                xyz = [float(x) for x in line.split()[1:4]]
                if len(xyz) != 3:
                    raise ValueError(f"vertex with {len(xyz)} numbers")
                if len(vertices) >= MAX_VERTICES:
                    raise _NotAModel(f"more than {MAX_VERTICES} vertices")
                vertices.append(xyz)
            elif line.startswith("f "):
                # "f 1/2/3 4//6 -1": the vertex is the first number; negative counts back.
                corners = [_index(token.split("/")[0], len(vertices)) for token in line.split()[1:]]
                if len(triangles) + len(corners) - 2 > MAX_FACES:
                    raise _NotAModel(f"more than {MAX_FACES} triangles")
                triangles.extend((corners[0], corners[i], corners[i + 1]) for i in range(1, len(corners) - 1))
    except (ValueError, IndexError) as err:
        raise _NotAModel(str(err)) from None
    if not triangles:
        raise _NotAModel("no faces")
    points = np.array(vertices, dtype=np.float64)
    faces = np.array(triangles, dtype=np.int64)
    if not points.size or faces.min() < 0 or faces.max() >= len(points):
        raise _NotAModel("face points at a missing vertex")
    if not np.isfinite(points).all() or np.abs(points).max() > MAX_COORD:
        raise _NotAModel("vertex too far out")
    return points, faces


def _index(token: str, count: int) -> int:
    number = int(token)
    return count + number if number < 0 else number - 1


def _view(points: np.ndarray) -> np.ndarray:
    """Points turned to the viewing angle: x right, y up, z toward the viewer."""
    cy, sy, cp, sp = math.cos(YAW), math.sin(YAW), math.cos(PITCH), math.sin(PITCH)
    yaw = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    pitch = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]])
    centred = points - (points.min(axis=0) + points.max(axis=0)) / 2
    return centred @ (pitch @ yaw).T


def _draw(points: np.ndarray, faces: np.ndarray) -> Image.Image:
    view = _view(points)
    corners = view[faces]  # (faces, 3 corners, xyz)
    normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    solid = lengths > 1e-12 * max(1.0, float(np.abs(view).max()) ** 2)
    if not solid.any():
        raise _NotAModel("every face is flat")
    corners, normals = corners[solid], normals[solid] / lengths[solid, None]

    canvas = SIZE * SUPERSAMPLE
    flat = corners[:, :, :2].reshape(-1, 2)
    low, high = flat.min(axis=0), flat.max(axis=0)
    scale = canvas * (1 - 2 * MARGIN) / max(float((high - low).max()), 1e-12)
    centre = (low + high) / 2
    xy = (corners[:, :, :2] - centre) * scale
    xy[:, :, 1] *= -1  # image y grows downward
    xy += canvas / 2

    light = AMBIENT + (1 - AMBIENT) * np.abs(normals @ LIGHT)  # two-sided: windings vary
    colours = np.clip(BASE_COLOR * light[:, None], 0, 255).astype(np.uint8)
    order = np.argsort(corners[:, :, 2].mean(axis=1))  # far first

    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for i in order:
        colour = (*(int(c) for c in colours[i]), 255)
        draw.polygon([tuple(p) for p in xy[i]], fill=colour, outline=colour)  # outline closes seams
    return image.resize((SIZE, SIZE), Image.Resampling.LANCZOS)
