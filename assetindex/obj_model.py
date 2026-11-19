"""Read a Wavefront .obj for a thumbnail: its points and faces, drawn by mesh_render."""

import logging

import numpy as np

from assetindex.mesh_render import MAX_FACES, MAX_VERTICES, NotAModel, render_mesh

log = logging.getLogger(__name__)

MAX_OBJ_BYTES = 16 * 1024**2


def render_obj(data: bytes) -> bytes | None:
    """PNG bytes of the model, or None if there is nothing to draw (or too much)."""
    try:
        return render_mesh(*_parse(data))
    except NotAModel as err:
        log.debug("cannot draw obj: %s", err)
        return None


def _parse(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    vertices: list[list[float]] = []
    triangles: list[tuple[int, int, int]] = []
    try:
        for line in data.decode("utf-8", "replace").splitlines():
            if line.startswith("v "):
                xyz = [float(x) for x in line.split()[1:4]]
                if len(xyz) != 3:
                    raise ValueError(f"vertex with {len(xyz)} numbers")
                # Checked while reading: a file of bare vertices would fill memory with lists.
                if len(vertices) >= MAX_VERTICES:
                    raise NotAModel(f"more than {MAX_VERTICES} vertices")
                vertices.append(xyz)
            elif line.startswith("f "):
                # "f 1/2/3 4//6 -1": the vertex is the first number; negative counts back.
                corners = [_index(token.split("/")[0], len(vertices)) for token in line.split()[1:]]
                if len(triangles) + len(corners) - 2 > MAX_FACES:
                    raise NotAModel(f"more than {MAX_FACES} triangles")
                triangles.extend((corners[0], corners[i], corners[i + 1]) for i in range(1, len(corners) - 1))
    except (ValueError, IndexError) as err:
        raise NotAModel(str(err)) from None
    return np.array(vertices, dtype=np.float64).reshape(-1, 3), np.array(triangles, dtype=np.int64).reshape(-1, 3)


def _index(token: str, count: int) -> int:
    number = int(token)
    return count + number if number < 0 else number - 1
