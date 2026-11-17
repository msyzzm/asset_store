"""Substring search over indexed paths."""

import sqlite3
from dataclasses import dataclass

from assetindex.archive_queries import entry_hits

MAX_LIMIT = 200
MAX_TERMS = 8  # each term is a separate LIKE subquery; cap the cost per request
MAX_QUERY_LENGTH = 200
TRIGRAM = 3  # the index's token length: shorter terms cannot use it
FILE_COLUMNS = (
    "f.id, f.root, f.rel_path, f.name, f.ext, f.kind, f.size, f.mtime, f.seq_frames AS frames,"
    " a.engine, a.engine_version, a.entry_count, a.preview_state AS sheet, c.text AS caption"
)
# Hidden sequence members (seq_frames = 0) are represented by their head row.
VISIBLE = "(f.seq_frames IS NULL OR f.seq_frames > 0)"


@dataclass(frozen=True)
class SearchResult:
    total: int
    items: list[dict]


def parse_terms(query: str) -> list[str]:
    # '%' is dropped so it can't act as a wildcard. '_' is kept: it is common in
    # filenames, and as a LIKE wildcard it only adds rare false positives.
    return [t for t in (raw.replace("%", "") for raw in query.split()) if t][:MAX_TERMS]


def _match(
    alias: str, terms: list[str], kind: str | None, with_entries: bool
) -> tuple[str, list[object]]:
    """A term matches a file's own path or picture description, or (with_entries) any
    path inside it if it is an archive. Terms shorter than a trigram read the tables
    themselves: SQLite 3.40 (the server's) finds no rows for them through the index."""
    clauses: list[str] = []
    params: list[object] = []
    for term in terms:
        if len(term) >= TRIGRAM:
            own = (
                f"{alias}.id IN (SELECT rowid FROM files_fts WHERE rel_path LIKE ?)"
                f" OR {alias}.id IN (SELECT rowid FROM captions_fts WHERE text LIKE ?)"
            )
            inner = (
                f"{alias}.id IN (SELECT e.file_id FROM archive_entries e"
                " WHERE e.id IN (SELECT rowid FROM entries_fts WHERE path LIKE ?))"
            )
        else:
            own = f"{alias}.rel_path LIKE ? OR {alias}.id IN (SELECT file_id FROM captions WHERE text LIKE ?)"
            inner = f"{alias}.id IN (SELECT file_id FROM archive_entries WHERE path LIKE ?)"
        clauses.append(f"({own} OR {inner})" if with_entries else f"({own})")
        params += [f"%{term}%"] * (3 if with_entries else 2)
    if kind:
        clauses.append(f"{alias}.kind = ?")
        params.append(kind)
    return " AND ".join(clauses), params


def _where_with_terms(terms: list[str], kind: str | None) -> tuple[str, list[object]]:
    """Visible matches, plus the head of any sequence whose hidden frame matched
    (so searching "walk_03" finds the walk sequence)."""
    visible, visible_params = _match("m", terms, kind, with_entries=True)
    # Sequence frames are images, never archives: no need to search archive contents here.
    hidden, hidden_params = _match("m", terms, kind, with_entries=False)
    where = f"""WHERE f.id IN (
        SELECT m.id FROM files m WHERE {visible} AND (m.seq_frames IS NULL OR m.seq_frames > 0)
        UNION
        SELECT h.id FROM files h
        WHERE h.seq_frames > 0 AND (h.root, h.seq_key) IN (
            SELECT m.root, m.seq_key FROM files m WHERE {hidden} AND m.seq_frames = 0
        )
    )"""
    return where, [*visible_params, *hidden_params]


def search(
    conn: sqlite3.Connection,
    query: str,
    kind: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> SearchResult:
    terms = parse_terms(query)
    if terms:
        where, params = _where_with_terms(terms, kind)
    else:
        where = f"WHERE {VISIBLE}" + (" AND f.kind = ?" if kind else "")
        params = [kind] if kind else []
    # Evaluate the match once: ordered ids give both the total and the page.
    ids = [r[0] for r in conn.execute(f"SELECT f.id FROM files f {where} ORDER BY f.rel_path, f.id", params)]
    start = max(0, offset)
    page = ids[start : start + max(1, min(limit, MAX_LIMIT))]
    return SearchResult(total=len(ids), items=fetch_items(conn, page, terms))


def fetch_items(conn: sqlite3.Connection, ids: list[int], terms: list[str]) -> list[dict]:
    """Result cards for these file ids, in the given order."""
    if not ids:
        return []
    rows = conn.execute(
        f"SELECT {FILE_COLUMNS} FROM files f LEFT JOIN archives a ON a.file_id = f.id"
        " LEFT JOIN captions c ON c.file_id = f.id"
        f" WHERE f.id IN ({','.join('?' * len(ids))})",
        ids,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [
        {**dict(r), "hits": entry_hits(conn, r["id"], r["rel_path"], terms) if r["entry_count"] else []}
        for r in (by_id[i] for i in ids if i in by_id)
    ]
