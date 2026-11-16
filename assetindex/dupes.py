"""Find duplicates: whole units (packs) that are identical, and files repeated across units.

Reading files over NFS is the slow part, so as little is read as possible:
1. Only files whose size another file of the root shares can be duplicates, and only those
   of at least HASH_MIN_BYTES are worth it (1.5 GB of 76 GB in the sample).
2. Those get a sample: their size plus three SAMPLE_CHUNK slices (start, middle, end).
   Different files almost always differ there.
3. Only files whose sample matches another's, inside a unit that could be identical to
   another (same kind, same number of files, same total size), are read whole.
Samples and hashes are cached per file id, size and mtime.

Identical units get a proposal to move all but one copy aside. Files repeated inside
different packs are only reported, matched by sample: moving one copy would break its pack.
"""

import hashlib
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from assetindex.config import Settings
from assetindex.units import opened_folders, unit_of

HASH_MIN_BYTES = 256 * 1024
SAMPLE_CHUNK = 16 * 1024  # x3 per file: one round trip each, even over NFS
HASH_BATCH = 32  # files read between two short DB writes
READ_CHUNK = 1024 * 1024
TOP_FILE_GROUPS = 50
# Names that say "I'm the second copy": the other one is kept.
_COPY_MARK = re.compile(r"\(\d+\)|（\d+）|副本|复制|\bcopy\b|\bdup\b", re.IGNORECASE)


@dataclass(frozen=True)
class HashStats:
    sampled: int = 0
    hashed: int = 0  # read whole
    failed: int = 0
    bytes: int = 0  # read from disk, samples and whole files
    stopped: bool = False


@dataclass(frozen=True)
class UnitFile:
    rel: str  # path inside the unit ("" for a file unit)
    size: int
    mtime: float
    hash: str | None  # of the whole content, only for files that needed it
    sample: str | None = None

    @property
    def content(self) -> str | None:
        """Best known stand-in for the content: the full hash, else the sample. Two
        units only end up equal on samples if reading whole was skipped or failed."""
        return self.hash or (f"s:{self.sample}" if self.sample else None)


@dataclass(frozen=True)
class DupGroup:
    keep: str
    others: tuple[str, ...]
    size: int  # of one copy
    files: int
    exact: bool  # every file compared by content (else small files by name and size)

    @property
    def wasted(self) -> int:
        return self.size * len(self.others)


def file_hash(path: Path) -> tuple[str, int]:
    """Whole-content hash, and the bytes read."""
    digest = hashlib.blake2b(digest_size=16)
    read = 0
    with path.open("rb") as f:
        while chunk := f.read(READ_CHUNK):
            digest.update(chunk)
            read += len(chunk)
    return digest.hexdigest(), read


def file_sample(path: Path) -> tuple[str, int]:
    """Hash of the size and three slices (all of it if small), and the bytes read."""
    digest = hashlib.blake2b(digest_size=16)
    with path.open("rb") as f:
        size = f.seek(0, 2)
        digest.update(f"{size}\n".encode())
        if size <= 3 * SAMPLE_CHUNK:
            offsets = [0]
            length = size
        else:
            offsets = [0, size // 2 - SAMPLE_CHUNK // 2, size - SAMPLE_CHUNK]
            length = SAMPLE_CHUNK
        read = 0
        for offset in offsets:
            f.seek(offset)
            data = f.read(length)
            digest.update(data)
            read += len(data)
    return digest.hexdigest(), read


def sample_candidates(conn: sqlite3.Connection, root: str) -> list[sqlite3.Row]:
    """Files of `root` that may have a twin there (same size) and have no current sample."""
    min_size = HASH_MIN_BYTES
    return conn.execute(
        """
        SELECT f.id, f.rel_path, f.size, f.mtime FROM files f
        LEFT JOIN file_samples s ON s.file_id = f.id AND s.size = f.size AND s.mtime = f.mtime
        WHERE f.root = ? AND f.size >= ? AND s.file_id IS NULL
          AND f.size IN (SELECT size FROM files WHERE root = ? AND size >= ? GROUP BY size HAVING COUNT(*) > 1)
        ORDER BY f.size DESC, f.id
        """,
        (root, min_size, root, min_size),
    ).fetchall()


def _twin_shaped_units(conn: sqlite3.Connection, root: str, opened: frozenset[str]) -> set[str]:
    """Units another unit could be identical to: same kind, number of files and total size."""
    shapes: dict[tuple[str, bool], list[int]] = defaultdict(lambda: [0, 0])
    for rel_path, size in conn.execute("SELECT rel_path, size FROM files WHERE root = ?", (root,)):
        shape = shapes[unit_of(rel_path, opened)]
        shape[0] += 1
        shape[1] += size
    counts = Counter((is_dir, n, total) for (_, is_dir), (n, total) in shapes.items())
    return {unit for (unit, is_dir), (n, total) in shapes.items() if counts[(is_dir, n, total)] > 1}


def hash_candidates(conn: sqlite3.Connection, root: str, opened: frozenset[str]) -> list[sqlite3.Row]:
    """Files to read whole: their sample matches another file's, they belong to a unit
    that could have a twin, and they have no current hash."""
    rows = conn.execute(
        """
        WITH cur AS (
            SELECT f.id, f.rel_path, f.size, f.mtime, s.sample FROM files f
            JOIN file_samples s ON s.file_id = f.id AND s.size = f.size AND s.mtime = f.mtime
            WHERE f.root = ?
        )
        SELECT c.id, c.rel_path, c.size, c.mtime FROM cur c
        LEFT JOIN file_hashes h ON h.file_id = c.id AND h.size = c.size AND h.mtime = c.mtime
        WHERE h.file_id IS NULL AND c.sample IN (SELECT sample FROM cur GROUP BY sample HAVING COUNT(*) > 1)
        ORDER BY c.size DESC, c.id
        """,
        (root,),
    ).fetchall()
    if not rows:
        return []
    twins = _twin_shaped_units(conn, root, opened)
    return [r for r in rows if unit_of(r["rel_path"], opened)[0] in twins]


def _read_files(
    conn: sqlite3.Connection,
    base: Path,
    rows: list[sqlite3.Row],
    measure: Callable[[Path], tuple[str, int]],
    table: str,
    should_stop: Callable[[], bool],
    report: Callable[[int, int], None],
) -> tuple[int, int, int, bool]:
    """Measure each file and store the result in `table`; (done, failed, bytes, stopped).
    No DB transaction is open while the NAS is read."""
    done = failed = read = 0
    for start in range(0, len(rows), HASH_BATCH):
        results = []
        for row in rows[start : start + HASH_BATCH]:
            if should_stop():
                _save(conn, table, results)
                return done, failed, read, True
            path = (base / row["rel_path"]).resolve()
            try:
                if not path.is_relative_to(base):
                    raise OSError("path leaves its root")
                value, nbytes = measure(path)
                results.append((row["id"], row["size"], row["mtime"], value))
                done += 1
                read += nbytes
            except OSError:  # gone or unreadable since the scan: no claim about it
                failed += 1
            report(done + failed, len(rows))
        _save(conn, table, results)
    return done, failed, read, False


_SAVE_SQL = {
    "file_samples": "INSERT OR REPLACE INTO file_samples (file_id, size, mtime, sample) VALUES (?, ?, ?, ?)",
    "file_hashes": "INSERT OR REPLACE INTO file_hashes (file_id, size, mtime, hash) VALUES (?, ?, ?, ?)",
}


def _save(conn: sqlite3.Connection, table: str, results: list[tuple]) -> None:
    if results:
        with conn:
            conn.executemany(_SAVE_SQL[table], results)


def hash_pending(
    conn: sqlite3.Connection,
    settings: Settings,
    root: str,
    opened: frozenset[str] | None = None,
    should_stop: Callable[[], bool] = lambda: False,
    report: Callable[[str, int, int], None] = lambda stage, done, total: None,
) -> HashStats:
    """Sample the candidates of one root, then read whole the ones that could be part of
    identical packs. `report` gets the stage ("sampling" or "hashing") and the progress."""
    if opened is None:
        opened = opened_folders([r[0] for r in conn.execute("SELECT rel_path FROM files WHERE root = ?", (root,))])
    base = settings.roots[root].resolve()
    sampled, failed, read, stopped = _read_files(
        conn, base, sample_candidates(conn, root), file_sample, "file_samples", should_stop,
        lambda done, total: report("sampling", done, total),
    )
    if stopped:
        return HashStats(sampled, 0, failed, read, stopped=True)
    hashed, failed_whole, read_whole, stopped = _read_files(
        conn, base, hash_candidates(conn, root, opened), file_hash, "file_hashes", should_stop,
        lambda done, total: report("hashing", done, total),
    )
    return HashStats(sampled, hashed, failed + failed_whole, read + read_whole, stopped)


def unit_files(conn: sqlite3.Connection, root: str, opened: frozenset[str]) -> dict[tuple[str, bool], list[UnitFile]]:
    """Every file of a root, grouped by its unit, with its current hash and sample if any."""
    rows = conn.execute(
        """
        SELECT f.rel_path, f.size, f.mtime, h.hash, s.sample FROM files f
        LEFT JOIN file_hashes h ON h.file_id = f.id AND h.size = f.size AND h.mtime = f.mtime
        LEFT JOIN file_samples s ON s.file_id = f.id AND s.size = f.size AND s.mtime = f.mtime
        WHERE f.root = ?
        """,
        (root,),
    )
    units: dict[tuple[str, bool], list[UnitFile]] = defaultdict(list)
    for r in rows:
        unit, is_dir = unit_of(r["rel_path"], opened)
        rel = r["rel_path"][len(unit) + 1 :] if is_dir else ""
        units[(unit, is_dir)].append(UnitFile(rel, r["size"], r["mtime"], r["hash"], r["sample"]))
    return units


def signature(files: list[UnitFile]) -> str:
    """Changes when a file in the unit is added, removed or modified."""
    digest = hashlib.sha1(usedforsecurity=False)
    for f in sorted(files, key=lambda f: f.rel):
        digest.update(f"{f.rel}\0{f.size}\0{f.mtime}\n".encode())
    return digest.hexdigest()


def content_key(is_dir: bool, files: list[UnitFile]) -> tuple[str, bool] | None:
    """What two identical units share: (key, compared by full content throughout). A single
    file is only comparable by its content; a folder by its tree, and by content where known."""
    if not is_dir:
        f = files[0]  # a lone file has no name or tree to back up a sample: full hash only
        return (f"f:{f.size}:{f.hash}", True) if f.hash else None
    digest = hashlib.sha1(usedforsecurity=False)
    for f in sorted(files, key=lambda f: f.rel):
        digest.update(f"{f.rel}\0{f.size}\0{f.content or ''}\n".encode())
    # Files below the hashing threshold are compared by name and size only.
    exact = all(f.hash for f in files)
    return f"d:{digest.hexdigest()}", exact


def _keep_rank(path: str, preferred: frozenset[str] = frozenset()) -> tuple:
    name = path.rsplit("/", 1)[-1]
    return (path not in preferred, bool(_COPY_MARK.search(name)), path.count("/"), len(path), path)


def duplicate_units(
    units: dict[tuple[str, bool], list[UnitFile]], preferred: Iterable[str] = ()
) -> list[DupGroup]:
    """Groups of identical units, biggest waste first. The kept copy: one the user chose
    (`preferred`), else one without a "copy" mark in its name, then the shallowest, then
    the shortest path."""
    preferred = frozenset(preferred)
    groups: dict[str, list[tuple[str, int, int, bool]]] = defaultdict(list)
    for (path, is_dir), files in units.items():
        key = content_key(is_dir, files)
        if key:
            groups[key[0]].append((path, sum(f.size for f in files), len(files), key[1]))
    result = []
    for members in groups.values():
        if len(members) < 2:
            continue
        paths = sorted((m[0] for m in members), key=lambda p: _keep_rank(p, preferred))
        _, size, count, _ = members[0]
        exact = all(m[3] for m in members)
        result.append(DupGroup(paths[0], tuple(paths[1:]), size, count, exact))
    return sorted(result, key=lambda g: (-g.wasted, g.keep))


def file_duplicates(units: dict[tuple[str, bool], list[UnitFile]], skip: set[str]) -> dict:
    """Files with the same content in different units (outside `skip`, the units already
    found identical as a whole). Report only, matched by sample (most were never read
    whole): the waste, and the worst groups."""
    by_sample: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for (unit, _), files in units.items():
        if unit in skip:
            continue
        for f in files:
            if f.sample:
                by_sample[f.sample].append((unit, f"{unit}/{f.rel}" if f.rel else unit, f.size))
    groups, inner_groups, inner_wasted = [], 0, 0
    for members in by_sample.values():
        if len(members) < 2:
            continue
        size = members[0][2]
        wasted = size * (len(members) - 1)
        if len({unit for unit, _, _ in members}) > 1:
            groups.append({"size": size, "wasted": wasted, "paths": sorted(p for _, p, _ in members)})
        else:  # inside one pack: mostly on purpose (an .fbm next to textures/, per-engine copies)
            inner_groups += 1
            inner_wasted += wasted
    groups.sort(key=lambda g: -g["wasted"])
    return {
        "groups": len(groups),
        "wasted": sum(g["wasted"] for g in groups),
        "top": groups[:TOP_FILE_GROUPS],
        "inner_groups": inner_groups,
        "inner_wasted": inner_wasted,
    }
