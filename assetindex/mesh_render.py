"""Draw a triangle mesh as a grey, lit model for thumbnails and archive covers.

A software render with numpy and Pillow (no GPU in the container): seen from the front
right and a little above, orthographic, two-sided light, painted far to near. Materials
and textures are ignored: the shape is what tells a wall from a barrel. The model files
(obj_model, fbx_model, gltf_model) only read points and triangles; this draws them.
"""

import io
import math

import numpy as np
from PIL import Image, ImageDraw

# Triangles: about 2 s to paint random small ones (measured 2026-11-18); painting time
# follows the area filled more than the count, so a worst case may take longer.
MAX_FACES = 100_000
MAX_VERTICES = 200_000
MAX_COORD = 1e12  # beyond this, squares overflow; no real model is that big
SIZE = 320
SUPERSAMPLE = 2  # drawn at twice the size, then shrunk: smooth edges
MARGIN = 0.06  # of the picture, on each side
YAW = math.radians(-35)  # turn the model to show its front and right side
PITCH = math.radians(25)  # and tip it to show its top
LIGHT = np.array([-0.4, 0.6, 0.7]) / np.linalg.norm([-0.4, 0.6, 0.7])
BASE_COLOR = np.array([206, 209, 215])
AMBIENT = 0.3


class NotAModel(ValueError):
    """Nothing to draw, or too much: the file gets no picture."""


def render_mesh(points: np.ndarray, faces: np.ndarray) -> bytes:
    """PNG bytes of the mesh: `points` (n, 3), y up; `faces` (m, 3) indices into them.
    Raises NotAModel if there is nothing sensible to draw."""
    points = np.asarray(points, dtype=np.float64)
    faces = np.asarray(faces, dtype=np.int64)
    if points.ndim != 2 or points.shape[1] != 3 or not len(points):
        raise NotAModel("no vertices")
    if faces.ndim != 2 or faces.shape[1] != 3 or not len(faces):
        raise NotAModel("no faces")
    if len(faces) > MAX_FACES:
        raise NotAModel(f"more than {MAX_FACES} triangles")
    if faces.min() < 0 or faces.max() >= len(points):
        raise NotAModel("face points at a missing vertex")
    if not np.isfinite(points).all() or np.abs(points).max() > MAX_COORD:
        raise NotAModel("vertex too far out")
    image = _draw(points, faces)
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


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
        raise NotAModel("every face is flat")
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
