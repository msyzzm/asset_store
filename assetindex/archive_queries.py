"""Read-side queries over indexed archive contents. No archive parsers imported here,
so the web server never loads py7zr/rarfile."""

import sqlite3

MAX_HITS = 3  # matching inner paths shown on a search result card


def _within_archive(terms: list[str]) -> tuple[str, list[object]]:
    # Scoped to one archive via idx_entries_file: a per-archive scan (<= MAX_ENTRIES rows)
    # beats materialising every global FTS match. LIKE is case-insensitive like the FTS path.
    return "".join(" AND e.path LIKE ?" for _ in terms), [f"%{t}%" for t in terms]


def _paths(conn: sqlite3.Connection, file_id: int, where: str, params: list[object]) -> list[str]:
    rows = conn.execute(
        f"SELECT e.path FROM archive_entries e WHERE e.file_id = ?{where} ORDER BY e.path, e.id LIMIT ?",
        (file_id, *params, MAX_HITS),
    )
    return [r["path"] for r in rows]


def inner_terms(rel_path: str, terms: list[str]) -> list[str]:
    """Terms the archive's own path doesn't match, so they must have matched its contents."""
    lowered = rel_path.lower()
    return [t for t in terms if t.lower() not in lowered]


def entry_hits(conn: sqlite3.Connection, file_id: int, rel_path: str, terms: list[str]) -> list[str]:
    """Inner paths explaining why an archive matched: paths matching every inner term,
    else (terms spread over several files) paths matching any of them."""
    inner = inner_terms(rel_path, terms)
    if not inner:
        return []
    hits = _paths(conn, file_id, *_within_archive(inner))
    if hits or len(inner) == 1:
        return hits
    any_term = " AND (" + " OR ".join("e.path LIKE ?" for _ in inner) + ")"
    return _paths(conn, file_id, any_term, [f"%{t}%" for t in inner])


def list_entries(
    conn: sqlite3.Connection, file_id: int, terms: list[str], limit: int, offset: int
) -> tuple[int, list[dict]]:
    where, params = _within_archive(terms)
    total = conn.execute(
        f"SELECT COUNT(*) FROM archive_entries e WHERE e.file_id = ?{where}", (file_id, *params)
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT e.id, e.path, e.ext, e.size, e.has_preview FROM archive_entries e WHERE e.file_id = ?{where}"
        " ORDER BY e.path, e.id LIMIT ? OFFSET ?",
        (file_id, *params, limit, offset),
    )
    return total, [dict(r) for r in rows]
