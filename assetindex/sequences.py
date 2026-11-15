"""Detect numbered image sequences (sprite frames, tile sets) and collapse each into one item.

Files named `<prefix><digits>.<ext>` in the same folder form a group. Groups with at
least SEQUENCE_MIN_FRAMES members get one visible "head" row (lowest frame number)
carrying the frame count; the other members are hidden from search but stay
reachable through `frames_of`.

`seq_frames` per row: NULL = normal file, N > 0 = head of N frames, 0 = hidden member.
"""

import re
import sqlite3

SEQUENCE_MIN_FRAMES = 5  # fewer numbered files are usually variants (tree1, tree2), not frames
MAX_INDEX_DIGITS = 9  # longer digit runs are ids/timestamps; also keeps seq_index in int64
MAX_FRAMES = 2000  # largest real group seen is ~800 tiles

# Lazy prefix + greedy digits: the digit group is the whole trailing run.
_NUMBERED = re.compile(r"^(.*?)(\d+)\.([^.]+)$")

FRAME_COLUMNS = "id, root, rel_path, name, ext, kind, size, mtime"

_GROUP_SQL = """
WITH ranked AS (
    SELECT id,
        COUNT(*) OVER (PARTITION BY seq_key) AS n,
        ROW_NUMBER() OVER (PARTITION BY seq_key ORDER BY seq_index, rel_path) AS rn
    FROM files WHERE root = ? AND seq_key IS NOT NULL
), target AS (
    SELECT id, CASE WHEN n < ? THEN NULL WHEN rn = 1 THEN n ELSE 0 END AS frames
    FROM ranked
)
UPDATE files SET seq_frames = target.frames
FROM target
WHERE files.id = target.id AND files.seq_frames IS NOT target.frames
"""


def sequence_of(rel_path: str, kind: str) -> tuple[str, int] | None:
    """Return (group key, frame number) for a numbered image, else None."""
    if kind != "image":
        return None
    name = rel_path.rsplit("/", 1)[-1]
    match = _NUMBERED.match(name)
    if match is None or len(match.group(2)) > MAX_INDEX_DIGITS:
        return None
    folder = rel_path[: len(rel_path) - len(name)]
    prefix, digits, ext = match.groups()
    return f"{folder}{prefix}#{ext.lower()}", int(digits)


def group_sequences(conn: sqlite3.Connection, root_name: str) -> None:
    """Recompute heads/hidden members for one root. Only rows whose state changes are written."""
    conn.execute(_GROUP_SQL, (root_name, SEQUENCE_MIN_FRAMES))


def frames_of(conn: sqlite3.Connection, file_id: int) -> list[dict] | None:
    """All frames of the sequence containing file_id, in frame order; None if not indexed."""
    row = conn.execute(
        "SELECT root, seq_key, seq_frames FROM files WHERE id = ?", (file_id,)
    ).fetchone()
    if row is None:
        return None
    if row["seq_frames"] is None:
        rows = conn.execute(f"SELECT {FRAME_COLUMNS} FROM files WHERE id = ?", (file_id,))
    else:
        rows = conn.execute(
            f"SELECT {FRAME_COLUMNS} FROM files WHERE root = ? AND seq_key = ?"
            " ORDER BY seq_index, rel_path LIMIT ?",
            (row["root"], row["seq_key"], MAX_FRAMES),
        )
    return [dict(r) for r in rows]
