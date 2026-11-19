"""A contact sheet for a folder pack with no picture but files carrying one (an unpacked
UE project: .uasset editor thumbnails). Built on first view from the files' own cached
thumbnails, and cached itself under thumbs/grids (evictable: it can be built again).
"""

import hashlib
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

from assetindex.classify import under
from assetindex.thumbs import source_limit

# Files carrying a picture of their own (an .uasset's editor thumbnail).
CARRIED_EXTS = ("uasset", "eps")
# Names vary (BumpingPub calls its meshes M_CupBoard_01 and keeps a BasicMaterial among
# them): an SM_ name first, then the Meshes folder less anything named a material.
MESHES_FIRST = (
    "(CASE WHEN name LIKE 'SM\\_%' ESCAPE '\\' THEN 0"
    " WHEN rel_path LIKE '%/Meshes/%' AND name NOT LIKE '%material%' THEN 1 ELSE 2 END)"
)
GRID_CELLS = 9
CANDIDATES = GRID_CELLS + 6  # spares for files whose thumbnail cannot be read
SCANNED = 500  # rows looked at for different-looking candidates
_NUMBERED = re.compile(r"[_\-\s]*\d+$")


def _in_folder(exts: tuple[str, ...]) -> str:
    return f"root = ? AND rel_path >= ? AND rel_path < ? AND ext IN ({','.join('?' * len(exts))})"


def summary(conn: sqlite3.Connection, root: str, folder: str) -> tuple[int, int | None, str]:
    """(how many carry a picture, the one to show if only one, version for the URL: it
    changes when a file is added, removed or replaced in place)."""
    low, high = under(folder)
    count, newest, changed, size = conn.execute(
        f"SELECT COUNT(*), MAX(id), MAX(mtime), SUM(size) FROM files WHERE {_in_folder(CARRIED_EXTS)}",
        (root, low, high, *CARRIED_EXTS),
    ).fetchone()
    first = conn.execute(
        f"SELECT id FROM files WHERE {_in_folder(CARRIED_EXTS)} ORDER BY {MESHES_FIRST}, rel_path LIMIT 1",
        (root, low, high, *CARRIED_EXTS),
    ).fetchone()
    return count, first[0] if first else None, f"{count}.{newest or 0}.{int(changed or 0)}.{size or 0}"


def members(conn: sqlite3.Connection, root: str, folder: str) -> list[sqlite3.Row]:
    """Files for the grid, meshes first, one of each kind: Bag_01..Bag_04 give one bag."""
    low, high = under(folder)
    rows = conn.execute(
        f"SELECT id, rel_path, name, ext, size, mtime FROM files WHERE {_in_folder(CARRIED_EXTS)}"
        f" ORDER BY {MESHES_FIRST}, rel_path LIMIT ?",
        (root, low, high, *CARRIED_EXTS, SCANNED),
    ).fetchall()
    picked, kinds = [], set()
    for row in rows:
        kind = _NUMBERED.sub("", row["name"].rsplit(".", 1)[0]).lower()
        if kind in kinds or row["size"] > (source_limit(row["ext"]) or 0):
            continue
        kinds.add(kind)
        picked.append(row)
        if len(picked) >= CANDIDATES:
            break
    return picked


def url(root: str, folder: str, version: str) -> str:
    """The version changes with the folder's files: browsers cache thumbnails for a week."""
    return f"/api/packs/grid?root={quote(root, safe='')}&path={quote(folder, safe='')}&v={version}"


def grid_path(thumb_dir: Path, root: str, folder: str, version: str) -> Path:
    key = hashlib.sha256(f"{root}\0{folder}\0{version}".encode()).hexdigest()
    return thumb_dir / "grids" / key[:2] / f"{key}.webp"
