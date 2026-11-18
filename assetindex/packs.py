"""Search by pack, for agents: every matching file is counted for the pack it belongs to
(a folder, or an archive with its previews, as the organizer splits a root), and a pack
is one result however many of its files match: an unpacked pack's hundreds of .uasset
files no longer push every other pack off the page.

Order: packs whose own name holds every word, then packs matched by files inside (most
hits first), then packs only the meaning matched (semantic search, in its order). Packs
set aside (_待删除, _Duplicates) are never offered.
"""

import sqlite3
import threading
from dataclasses import dataclass, field

from assetindex.archive_queries import entry_hits
from assetindex.categories import DUPLICATES, TO_DELETE
from assetindex.classify import under
from assetindex.kinds import LISTABLE_ARCHIVE_EXTS, THUMBNAIL_EXTS
from assetindex.search import _where_with_terms, parse_terms
from assetindex.tags import tags_of
from assetindex.units import Layout, stored_layout

MAX_HITS = 5  # files shown per pack: enough to see why it matched
MAX_PACKS = 50
MAX_CAPTION_IDS = 500
ASIDE = (TO_DELETE + "/", DUPLICATES + "/")
DIMS = (("01_2D/", "2D"), ("02_3D/", "3D"), ("03_Audio/", "audio"))
UNREAL_EXTS = ("uasset", "umap")
UNITY_EXTS = ("unitypackage", "unity", "prefab")
# What decides how a root splits into packs: its files, and the user's splits, joins and
# decided folder packs (see units.stored_layout).
LAYOUT_KEY = """
SELECT (SELECT COUNT(*) || ':' || IFNULL(MAX(id), 0) || ':' || IFNULL(SUM(mtime), 0) FROM files WHERE root = ?1),
       (SELECT COUNT(*) || ':' || IFNULL(MAX(rowid), 0) FROM unit_splits WHERE root = ?1),
       (SELECT COUNT(*) || ':' || IFNULL(MAX(rowid), 0) FROM unit_joins WHERE root = ?1),
       (SELECT COUNT(*) || ':' || IFNULL(MAX(updated), 0) FROM organize_units
        WHERE root = ?1 AND is_dir = 1 AND status IN ('accepted', 'rejected'))
"""

_layouts: dict[str, tuple[tuple, Layout]] = {}
_layouts_lock = threading.Lock()


@dataclass(frozen=True)
class PackResult:
    total: int
    packs: list[dict]


@dataclass
class _Found:
    root: str
    path: str
    is_dir: bool
    match: str  # "name" | "inside" | "meaning"; "all" when browsing with no words
    hits: list[str]


@dataclass
class _Request:
    """One search: each root's layout looked up once, each folder's files read once."""

    conn: sqlite3.Connection
    terms: list[str] = field(default_factory=list)
    layouts: dict[str, Layout] = field(default_factory=dict)
    folders: dict[tuple[str, str], list[sqlite3.Row]] = field(default_factory=dict)

    def layout(self, root: str) -> Layout:
        if root not in self.layouts:
            self.layouts[root] = _cached_layout(self.conn, root)
        return self.layouts[root]

    def in_folder(self, root: str, folder: str) -> list[sqlite3.Row]:
        """Files in `folder` and one level down (where a preview folder sits), not deeper:
        a category folder also holds unpacked packs of thousands of files."""
        if (root, folder) not in self.folders:
            low, high = under(folder) if folder else ("", "\U0010ffff")
            self.folders[(root, folder)] = self.conn.execute(
                "SELECT f.id, f.rel_path, f.ext, f.size, a.engine, a.preview_state FROM files f"
                " LEFT JOIN archives a ON a.file_id = f.id WHERE f.root = ? AND f.rel_path >= ? AND f.rel_path < ?"
                " AND length(substr(f.rel_path, ?)) - length(replace(substr(f.rel_path, ?), '/', '')) <= 1",
                (root, low, high, len(low) + 1, len(low) + 1),
            ).fetchall()
        return self.folders[(root, folder)]


def _cached_layout(conn: sqlite3.Connection, root: str) -> Layout:
    """The root's packs, recomputed only when what decides them changed."""
    key = tuple(conn.execute(LAYOUT_KEY, (root,)).fetchone())
    with _layouts_lock:
        cached = _layouts.get(root)
        if cached and cached[0] == key:
            return cached[1]
    shape = stored_layout(conn, root)[1]
    with _layouts_lock:
        _layouts[root] = (key, shape)
    return shape


def _in_category(path: str, category: str) -> bool:
    parent = path.rpartition("/")[0]
    return not category or parent == category or parent.startswith(category + "/")


def _own_name(path: str, is_dir: bool) -> str:
    name = path.rpartition("/")[2]
    return (name if is_dir else name.rpartition(".")[0] or name).lower()


def _group(req: _Request, rows, terms: list[str], category: str) -> dict[tuple[str, str], _Found]:
    found: dict[tuple[str, str], _Found] = {}
    for root, rel_path in rows:
        path, is_dir = req.layout(root).unit_of(rel_path)
        if path.startswith(ASIDE) or not _in_category(path, category):
            continue
        pack = found.get((root, path))
        if pack is None:
            own = bool(terms) and all(t.lower() in _own_name(path, is_dir) for t in terms)
            pack = found[(root, path)] = _Found(root, path, is_dir, "name" if own else "inside", [])
        pack.hits.append(rel_path[len(path) + 1 :] if is_dir else rel_path.rpartition("/")[2])
    return found


def _ranked(word: dict, meaning: dict) -> list[_Found]:
    """Named packs first (most hits first); then the rest by meaning, whether a word
    matched inside or not; last, packs only a word inside matched (most hits first). A
    pack with one file called "fighter" no longer outranks every warrior pack."""
    closeness = {key: n for n, key in enumerate(meaning)}  # in the order semantic search gave
    named = [p for p in word.values() if p.match == "name"]
    rest = [p for p in word.values() if p.match != "name"]
    for key, pack in meaning.items():
        if key not in word:
            pack.match = "meaning"
            rest.append(pack)
    unranked = len(closeness)

    def order(pack: _Found) -> tuple:
        return closeness.get((pack.root, pack.path), unranked), -len(pack.hits), pack.path

    return sorted(named, key=lambda p: (-len(p.hits), p.path)) + sorted(rest, key=order)


def _every_pack(req: _Request, category: str) -> list[_Found]:
    """No words: every pack, by path (category, then name), for browsing."""
    rows = req.conn.execute("SELECT root, rel_path FROM files").fetchall()
    found = _group(req, rows, [], category)
    for pack in found.values():
        pack.match, pack.hits = "all", []
    return sorted(found.values(), key=lambda p: (p.root, p.path))


def _folder_pack(req: _Request, pack: _Found) -> tuple[int, int, int | None, str | None, list[int]]:
    """(files, bytes, thumbnail id, engine, a few ids for a caption) of a folder pack."""
    conn, (low, high) = req.conn, under(pack.path)
    files, size = conn.execute(
        "SELECT COUNT(*), IFNULL(SUM(size), 0) FROM files WHERE root = ? AND rel_path >= ? AND rel_path < ?",
        (pack.root, low, high),
    ).fetchone()
    exts = sorted(THUMBNAIL_EXTS)
    thumb = conn.execute(
        f"SELECT id FROM files WHERE root = ? AND rel_path >= ? AND rel_path < ? AND ext IN ({','.join('?' * len(exts))})"
        " ORDER BY rel_path LIMIT 1",
        (pack.root, low, high, *exts),
    ).fetchone()
    engine = conn.execute(
        "SELECT a.engine FROM files f JOIN archives a ON a.file_id = f.id"
        " WHERE f.root = ? AND f.rel_path >= ? AND f.rel_path < ? AND a.engine IS NOT NULL LIMIT 1",
        (pack.root, low, high),
    ).fetchone()
    found_ext = [r[0] for r in conn.execute(
        f"SELECT DISTINCT ext FROM files WHERE root = ? AND rel_path >= ? AND rel_path < ?"
        f" AND ext IN ({','.join('?' * (len(UNREAL_EXTS) + len(UNITY_EXTS)))}) LIMIT 2",
        (pack.root, low, high, *UNREAL_EXTS, *UNITY_EXTS),
    )]
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM files WHERE root = ? AND rel_path >= ? AND rel_path < ? ORDER BY rel_path LIMIT ?",
        (pack.root, low, high, MAX_CAPTION_IDS),
    )]
    return files, size, thumb[0] if thumb else None, engine[0] if engine else _engine_of(found_ext), ids


def _engine_of(exts) -> str | None:
    exts = {(e or "").lower() for e in exts}
    if exts & set(UNREAL_EXTS):
        return "unreal"
    if exts & set(UNITY_EXTS):
        return "unity"
    return None


def _file_members(req: _Request, pack: _Found) -> list[sqlite3.Row]:
    """The file and its previews, which may sit beside it or in a preview folder."""
    folder = pack.path.rpartition("/")[0]
    shape = req.layout(pack.root)
    return [r for r in req.in_folder(pack.root, folder) if shape.unit_of(r["rel_path"]) == (pack.path, False)]


def _thumb(rows: list[sqlite3.Row], pack: _Found) -> int | None:
    for row in sorted(rows, key=lambda r: (r["rel_path"] != pack.path, r["rel_path"])):
        if row["ext"] in LISTABLE_ARCHIVE_EXTS and row["preview_state"] == 1:
            return row["id"]
        if row["ext"] in THUMBNAIL_EXTS:
            return row["id"]
    return None


def _caption(conn: sqlite3.Connection, ids: list[int]) -> str | None:
    ids = ids[:MAX_CAPTION_IDS]
    if not ids:
        return None
    row = conn.execute(
        f"SELECT text FROM captions WHERE text IS NOT NULL AND file_id IN ({','.join('?' * len(ids))}) LIMIT 1", ids
    ).fetchone()
    return " ".join(row[0].split()) if row else None


def describe(req: _Request, pack: _Found) -> dict:
    """A pack as an agent sees it."""
    links, own = {}, None
    if pack.is_dir:
        files, size, thumb, engine, ids = _folder_pack(req, pack)
        kind = "folder"
    else:
        rows = _file_members(req, pack)
        own = next((r for r in rows if r["rel_path"] == pack.path), None)
        files, size, thumb = len(rows), sum(r["size"] for r in rows), _thumb(rows, pack)
        engine = next((r["engine"] for r in rows if r["engine"]), None) or _engine_of(r["ext"] for r in rows)
        ids = [own["id"]] if own is not None else [r["id"] for r in rows]
        kind = "archive" if own is not None and own["ext"] in LISTABLE_ARCHIVE_EXTS else "file"
    if own is not None:
        links["detail"] = f"/api/agent/files/{own['id']}"
        links["download"] = f"/api/files/{own['id']}/download"
    if thumb is not None:
        links["thumb"] = f"/api/files/{thumb}/thumb"
    category, _, name = pack.path.rpartition("/")
    hits = sorted(pack.hits)
    if kind == "archive":  # the files inside that matched, not the archive's own name
        inside = entry_hits(req.conn, own["id"], own["rel_path"], req.terms) if req.terms else []
        hits = inside + [h for h in hits if h != name]
    return {
        "id": own["id"] if own is not None else None,  # the archive or file itself; None for a folder
        "path": f"{pack.root}:/{pack.path}",
        "name": name,
        "category": category,
        "kind": kind,
        "files": files,
        "size": size,
        "dim": next((dim for prefix, dim in DIMS if pack.path.startswith(prefix)), None),
        "engine": engine,
        "caption": _caption(req.conn, ids),
        "tags": tags_of(req.conn, pack.root, pack.path),  # view (a hint only), style, themes; None if not tagged
        "match": pack.match,
        "hit_count": len(hits),  # an archive: its matching files inside (entry_hits caps them)
        "hits": hits[:MAX_HITS],
        "links": links,
    }


def _meant(req: _Request, ids: list[int], category: str) -> dict[tuple[str, str], _Found]:
    """Packs of semantic search's files, in its order."""
    if not ids:
        return {}
    where = {r[0]: (r[1], r[2]) for r in req.conn.execute(
        f"SELECT id, root, rel_path FROM files WHERE id IN ({','.join('?' * len(ids))})", ids)}
    return _group(req, [where[i] for i in ids if i in where], [], category)


def search_packs(
    conn: sqlite3.Connection, query: str, category: str = "", limit: int = 20, offset: int = 0,
    meaning: list[int] | None = None,
) -> PackResult:
    """Packs for `query`; `meaning` is semantic search's file ids, best first, if any."""
    terms = parse_terms(query)
    req = _Request(conn, terms)
    if terms:
        where, params = _where_with_terms(terms, None)
        rows = conn.execute(f"SELECT f.root, f.rel_path FROM files f {where}", params).fetchall()
        ranked = _ranked(_group(req, rows, terms, category), _meant(req, meaning or [], category))
    else:
        ranked = _every_pack(req, category)
    start = max(0, offset)
    page = ranked[start : start + max(1, min(limit, MAX_PACKS))]
    return PackResult(total=len(ranked), packs=[describe(req, p) for p in page])
