"""Semantic search: rank cards by how close their vectors are to the query's.

Text and image similarities live on different scales (text-to-text scores run higher),
so raw scores are not compared. Each source's scores are standardised (how many
standard deviations above that source's average) and a card scores the better of its
two: a picture that clearly shows the query wins even if its file name says nothing.
The spread has a floor so that a source where nothing stands out can't blow noise up
into a match.
"""

import sqlite3
import threading
import time
from collections import Counter
from dataclasses import dataclass

import numpy as np

from assetindex.embedding import EmbedClient
from assetindex.search import MAX_LIMIT, SearchResult, fetch_items, parse_terms

MAX_RESULTS = 200  # semantic ranking has no cut-off: show the best, not all of it
MIN_SPREAD = 0.05  # cosine similarity; below this a source's scores are mostly noise
RELOAD_SECONDS = 30


@dataclass(frozen=True)
class _Matrix:
    ids: np.ndarray  # file ids, one per row
    kinds: np.ndarray  # files.kind per row
    vectors: np.ndarray  # unit-length float32 rows


class NoVectors(Exception):
    """Nothing to search with yet: not embedded, or embedded with another model."""


class VectorIndex:
    """Stored vectors held in memory; reloaded when the embeddings table changes, but
    at most every RELOAD_SECONDS: while a scan is embedding, the table changes every few
    seconds, and reloading tens of thousands of vectors per search would be wasteful."""

    def __init__(self, clock=time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self._signature = None
        self._loaded_at = 0.0
        self._matrices: dict[str, _Matrix] = {}

    def get(self, conn: sqlite3.Connection, model: str) -> dict[str, _Matrix]:
        # INSERT OR REPLACE gives the new row the highest rowid, so any change moves this.
        signature = (model, *conn.execute("SELECT COUNT(*), MAX(rowid) FROM embeddings").fetchone())
        with self._lock:
            stale = signature != self._signature
            fresh_enough = (
                self._signature is not None
                and self._signature[0] == model
                and self._matrices
                and self._clock() - self._loaded_at < RELOAD_SECONDS
            )
            if stale and not fresh_enough:
                self._matrices = {}  # let the old arrays go before building new ones
                self._matrices = _load(conn, model)
                self._signature = signature
                self._loaded_at = self._clock()
            return self._matrices


def _load(conn: sqlite3.Connection, model: str) -> dict[str, _Matrix]:
    matrices = {}
    for (source,) in conn.execute("SELECT DISTINCT source FROM embeddings WHERE model = ?", (model,)).fetchall():
        rows = conn.execute(
            """
            SELECT e.file_id, f.kind, e.vector FROM embeddings e JOIN files f ON f.id = e.file_id
            WHERE e.model = ? AND e.source = ? AND e.vector IS NOT NULL
              AND (f.seq_frames IS NULL OR f.seq_frames > 0)
            """,
            (model, source),
        ).fetchall()
        if not rows:
            continue
        # One model should mean one dimension; if not, keep the majority.
        size = Counter(len(r["vector"]) for r in rows).most_common(1)[0][0]
        rows = [r for r in rows if len(r["vector"]) == size]
        vectors = np.empty((len(rows), size // 4), dtype=np.float32)  # filled in place: no extra copy
        for i, r in enumerate(rows):
            vectors[i] = np.frombuffer(r["vector"], dtype=np.float32)
        matrices[source] = _Matrix(
            ids=np.array([r["file_id"] for r in rows], dtype=np.int64),
            kinds=np.array([r["kind"] for r in rows]),
            vectors=vectors,
        )
    return matrices


def _unit(vector: list[float]) -> np.ndarray:
    arr = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm > 0 else arr


def rank(matrices: dict[str, _Matrix], query: np.ndarray, kind: str | None) -> list[int]:
    best: dict[int, float] = {}
    for matrix in matrices.values():
        if matrix.vectors.shape[1] != query.shape[0]:
            continue  # vectors from a model with another dimension
        sims = matrix.vectors @ query
        scores = (sims - sims.mean()) / max(float(sims.std()), MIN_SPREAD)
        keep = np.flatnonzero(matrix.kinds == kind) if kind else np.arange(len(scores))
        for row in keep[np.argsort(-scores[keep], kind="stable")[:MAX_RESULTS]]:
            file_id = int(matrix.ids[row])
            best[file_id] = max(best.get(file_id, -np.inf), float(scores[row]))
    return sorted(best, key=lambda i: -best[i])[:MAX_RESULTS]


def _visible(conn: sqlite3.Connection, ids: list[int]) -> list[int]:
    """Drop cards gone or hidden since the index was loaded (deleted files, frames that
    joined a sequence), keeping the order."""
    if not ids:
        return ids
    shown = {
        r[0]
        for r in conn.execute(
            f"SELECT id FROM files WHERE id IN ({','.join('?' * len(ids))})"
            " AND (seq_frames IS NULL OR seq_frames > 0)",
            ids,
        )
    }
    return [i for i in ids if i in shown]


def semantic_search(
    conn: sqlite3.Connection,
    index: VectorIndex,
    client: EmbedClient,
    model: str,
    query: str,
    kind: str | None = None,
    limit: int = 50,
    offset: int = 0,
    query_vector: list[float] | None = None,
) -> SearchResult:
    """Raises EmbedError if the query can't be embedded, NoVectors if there's nothing to
    compare it with."""
    matrices = index.get(conn, model)
    if not matrices:
        raise NoVectors(model)
    vector = _unit(query_vector if query_vector is not None else client.embed_texts([query])[0])
    if all(m.vectors.shape[1] != vector.shape[0] for m in matrices.values()):
        raise NoVectors(model)  # stored vectors came from a model with another dimension
    ranked = _visible(conn, rank(matrices, vector, kind))
    start = max(0, offset)
    page = ranked[start : start + max(1, min(limit, MAX_LIMIT))]
    return SearchResult(total=len(ranked), items=fetch_items(conn, page, parse_terms(query)))
