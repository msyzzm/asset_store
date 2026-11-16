"""Find duplicates: whole units (packs) that are identical, and files repeated across units.

Only files whose size another file shares can be duplicates, and only those of at least
HASH_MIN_BYTES are worth reading: that is a small part of a library (1.5 GB of 76 GB in
the sample). Hashes are cached per file id, size and mtime.

Identical units get a proposal to move all but one copy aside. Files repeated inside
different packs are only reported: moving one copy would break its pack.
"""

import hashlib
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from assetindex.config import Settings
from assetindex.units import unit_of

HASH_MIN_BYTES = 256 * 1024
HASH_BATCH = 32  # files hashed between two short DB writes
READ_CHUNK = 1024 * 1024
TOP_FILE_GROUPS = 50
# Names that say "I'm the second copy": the other one is kept.
_COPY_MARK = re.compile(r"\(\d+\)|（\d+）|副本|复制|\bcopy\b|\bdup\b", re.IGNORECASE)


@dataclass(frozen=True)
class HashStats:
    hashed: int = 0
    failed: int = 0
    bytes: int = 0
    stopped: bool = False


@dataclass(frozen=True)
class UnitFile:
    rel: str  # path inside the unit ("" for a file unit)
    size: int
    mtime: float
    hash: str | None


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


def file_hash(path: Path) -> str:
    digest = hashlib.blake2b(digest_size=16)
    with path.open("rb") as f:
        while chunk := f.read(READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def hash_candidates(conn: sqlite3.Connection, root: str) -> list[sqlite3.Row]:
    """Files of `root` that may have a twin (same size somewhere) and have no current hash."""
    min_size = HASH_MIN_BYTES
    return conn.execute(
        """
        SELECT f.id, f.rel_path, f.size, f.mtime FROM files f
        LEFT JOIN file_hashes h ON h.file_id = f.id AND h.size = f.size AND h.mtime = f.mtime
        WHERE f.root = ? AND f.size >= ? AND h.file_id IS NULL
          AND f.size IN (SELECT size FROM files WHERE size >= ? GROUP BY size HAVING COUNT(*) > 1)
        ORDER BY f.size DESC, f.id
        """,
        (root, min_size, min_size),
    ).fetchall()


def hash_pending(
    conn: sqlite3.Connection,
    settings: Settings,
    root: str,
    should_stop: Callable[[], bool] = lambda: False,
    report: Callable[[int, int], None] = lambda done, total: None,
) -> HashStats:
    """Hash the candidates of one root. No DB transaction is open while the NAS is read."""
    rows = hash_candidates(conn, root)
    base = settings.roots[root].resolve()
    hashed = failed = size = 0
    for start in range(0, len(rows), HASH_BATCH):
        results = []
        for row in rows[start : start + HASH_BATCH]:
            if should_stop():
                _save_hashes(conn, results)
                return HashStats(hashed, failed, size, stopped=True)
            path = (base / row["rel_path"]).resolve()
            try:
                if not path.is_relative_to(base):
                    raise OSError("path leaves its root")
                results.append((row["id"], row["size"], row["mtime"], file_hash(path)))
                hashed += 1
                size += row["size"]
            except OSError:  # gone or unreadable since the scan: no hash, no duplicate claim
                failed += 1
            report(hashed + failed, len(rows))
        _save_hashes(conn, results)
    return HashStats(hashed, failed, size)


def _save_hashes(conn: sqlite3.Connection, results: list[tuple]) -> None:
    if results:
        with conn:
            conn.executemany("INSERT OR REPLACE INTO file_hashes (file_id, size, mtime, hash) VALUES (?, ?, ?, ?)", results)


def unit_files(conn: sqlite3.Connection, root: str, opened: frozenset[str]) -> dict[tuple[str, bool], list[UnitFile]]:
    """Every file of a root, grouped by its unit, with its hash if it has a current one."""
    rows = conn.execute(
        """
        SELECT f.rel_path, f.size, f.mtime, h.hash FROM files f
        LEFT JOIN file_hashes h ON h.file_id = f.id AND h.size = f.size AND h.mtime = f.mtime
        WHERE f.root = ?
        """,
        (root,),
    )
    units: dict[tuple[str, bool], list[UnitFile]] = defaultdict(list)
    for r in rows:
        unit, is_dir = unit_of(r["rel_path"], opened)
        rel = r["rel_path"][len(unit) + 1 :] if is_dir else ""
        units[(unit, is_dir)].append(UnitFile(rel, r["size"], r["mtime"], r["hash"]))
    return units


def signature(files: list[UnitFile]) -> str:
    """Changes when a file in the unit is added, removed or modified."""
    digest = hashlib.sha1(usedforsecurity=False)
    for f in sorted(files, key=lambda f: f.rel):
        digest.update(f"{f.rel}\0{f.size}\0{f.mtime}\n".encode())
    return digest.hexdigest()


def content_key(is_dir: bool, files: list[UnitFile]) -> tuple[str, bool] | None:
    """What two identical units share: (key, compared by content throughout). A single file
    is only comparable by its hash; a folder by its tree, files with a hash by content."""
    if not is_dir:
        f = files[0]
        return (f"f:{f.size}:{f.hash}", True) if f.hash else None
    digest = hashlib.sha1(usedforsecurity=False)
    for f in sorted(files, key=lambda f: f.rel):
        digest.update(f"{f.rel}\0{f.size}\0{f.hash or ''}\n".encode())
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
        _, size, count, exact = members[0]
        result.append(DupGroup(paths[0], tuple(paths[1:]), size, count, exact))
    return sorted(result, key=lambda g: (-g.wasted, g.keep))


def file_duplicates(units: dict[tuple[str, bool], list[UnitFile]], skip: set[str]) -> dict:
    """Files with the same content in different units (outside `skip`, the units already
    found identical as a whole). Report only: the waste, and the worst groups."""
    by_hash: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for (unit, _), files in units.items():
        if unit in skip:
            continue
        for f in files:
            if f.hash:
                by_hash[f.hash].append((unit, f"{unit}/{f.rel}" if f.rel else unit, f.size))
    groups, inner_groups, inner_wasted = [], 0, 0
    for members in by_hash.values():
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
