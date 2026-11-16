"""Carry out confirmed proposals: the move plan (read-only) and its checks.

A plan lists, for every accepted pack whose target differs from where it is, the renames
that put it there: a folder pack is one rename; a file pack is its file plus companions
(previews, notes, other volumes), each renamed after the new name so they still pair.

Before anything moves, the plan is checked against the index:
- no target may exist already, or be wanted by two packs (compared without case: NAS
  file systems may ignore it);
- splitting the library into packs again after the moves must give exactly the planned
  packs at their new places, and leave every other pack as it was. Otherwise the next
  analysis would see something else than what was confirmed.
A pack that fails a check is left out of the plan and reported with the reason.
"""

import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from assetindex.classify import file_suffix
from assetindex.units import _VOLUME, SCHEME_FOLDERS, Layout, companions_by_unit, layout, stored_joins, stored_splits

MAX_CHECK_ROUNDS = 5  # dropping a move can change how its neighbours parse


@dataclass(frozen=True)
class Rename:
    src: str  # relative to the root
    dst: str
    is_dir: bool


@dataclass(frozen=True)
class UnitMove:
    path: str
    target: str
    is_dir: bool
    renames: tuple[Rename, ...]

    @property
    def new_path(self) -> str:
        """Where the unit is after the move (a file unit: its main file)."""
        return self.renames[0].dst


@dataclass(frozen=True)
class Problem:
    path: str
    reason: str


@dataclass(frozen=True)
class MovePlan:
    moves: tuple[UnitMove, ...]
    problems: tuple[Problem, ...]


def _companion_suffix(name: str) -> str:
    """What a renamed file keeps of its name: a volume's ".part2.rar", else its extension."""
    volume = _VOLUME.match(name)
    return name[len(volume["stem"]) :] if volume else file_suffix(name)


def _file_renames(target: str, main: str, companions: list[str]) -> list[Rename]:
    """The main file goes to `target` (a first volume stays "<name>.part1.rar");
    companions go next to it, named after it."""
    folder, _, target_name = target.rpartition("/")
    suffix = file_suffix(target_name)
    stem = target_name[: len(target_name) - len(suffix)] if suffix else target_name
    new_main = f"{folder}/{stem}{_companion_suffix(main.rpartition('/')[2])}"
    renames = [Rename(main, new_main, False)]
    used = {new_main.lower()}
    for companion in companions:
        own = _companion_suffix(companion.rpartition("/")[2])
        dst, n = f"{folder}/{stem}{own}", 2
        while dst.lower() in used:  # two previews: "x.png", "x (2).png"
            dst = f"{folder}/{stem} ({n}){own}"
            n += 1
        used.add(dst.lower())
        renames.append(Rename(companion, dst, False))
    return renames


def build_plan(conn: sqlite3.Connection, root: str) -> MovePlan:
    paths = [r[0] for r in conn.execute("SELECT rel_path FROM files WHERE root = ? ORDER BY rel_path", (root,))]
    splits, joins = stored_splits(conn, root), stored_joins(conn, root)
    shape = layout(paths, splits, joins, SCHEME_FOLDERS)
    companions = companions_by_unit(paths, shape)
    rows = conn.execute(
        "SELECT path, target, is_dir FROM organize_units WHERE root = ? AND status = 'accepted'"
        " AND target IS NOT NULL AND target != path ORDER BY path",
        (root,),
    ).fetchall()
    candidates = []
    problems: list[Problem] = []
    for row in rows:
        if not _current(row["path"], bool(row["is_dir"]), paths, shape):
            problems.append(Problem(row["path"], "这个包的文件变了，重新分析后再确认"))
            continue
        if row["is_dir"]:
            renames = [Rename(row["path"], row["target"], True)]
        else:
            renames = _file_renames(row["target"], row["path"], companions.get(row["path"], []))
        candidates.append(UnitMove(row["path"], row["target"], bool(row["is_dir"]), tuple(renames)))
    moves = _without_clashes(candidates, paths, problems)
    for _ in range(MAX_CHECK_ROUNDS):
        bad = _misparsed(moves, paths, shape, splits, joins)
        if not bad:
            break
        problems.extend(Problem(path, reason) for path, reason in bad.items())
        moves = [m for m in moves if m.path not in bad]
    else:  # still not settled: move nothing rather than something unchecked
        problems.extend(Problem(m.path, "检查没能收敛，这次先不移动") for m in moves)
        moves = []
    return MovePlan(tuple(moves), tuple(sorted(problems, key=lambda p: p.path)))


def _current(path: str, is_dir: bool, paths: list[str], shape: Layout) -> bool:
    """The organize row still describes a unit of today's layout."""
    if not is_dir:
        return path in paths and shape.unit_of(path) == (path, False)
    inside = next((p for p in paths if p.startswith(path + "/")), None)
    return inside is not None and shape.unit_of(inside) == (path, True)


def _occupied(paths: list[str]) -> set[str]:
    """Every indexed file and folder, lower-cased."""
    taken = set()
    for path in paths:
        parts = path.lower().split("/")
        taken.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    return taken


def _without_clashes(candidates: list[UnitMove], paths: list[str], problems: list[Problem]) -> list[UnitMove]:
    """Drop moves whose targets exist or are wanted twice. A target that is itself moving
    away counts as taken too: no chains, so the renames can run in any order."""
    taken = _occupied(paths)
    wanted: dict[str, set[str]] = defaultdict(set)
    for move in candidates:
        for rename in move.renames:
            wanted[rename.dst.lower()].add(move.path)
    moving_dirs = [(r.src.lower() + "/", m.path) for m in candidates for r in m.renames if r.is_dir]
    all_dsts = [(r.dst.lower(), m.path) for m in candidates for r in m.renames]
    keep = []
    for move in candidates:
        reason = None
        for rename in move.renames:
            dst = rename.dst.lower()
            if len(wanted[dst]) > 1:
                reason = f"「{rename.dst}」也是别的包的目标"
            elif dst in taken:
                reason = f"「{rename.dst}」已经存在"
            elif any(dst.startswith(src) for src, owner in moving_dirs if owner != move.path):
                reason = f"「{rename.dst}」在另一个要移动的文件夹里"
            elif any(other != move.path and (o.startswith(dst + "/") or dst.startswith(o + "/")) for o, other in all_dsts):
                reason = f"「{rename.dst}」和另一个包的目标是包含关系"
            if reason:
                break
        if reason:
            problems.append(Problem(move.path, reason))
        else:
            keep.append(move)
    return keep


def left_folders(renames: list[Rename]) -> list[str]:
    """Folders packs move out of, from each pack's first rename (its main file or
    folder: a preview folder it takes companions from is not one). They stay opened
    (recorded as splits): the packs left there must not merge into one because their
    neighbours went elsewhere."""
    return sorted({r.src.rpartition("/")[0] for r in renames} - {""})


def _relocate(path: str, files: dict[str, str], dirs: dict[str, str]) -> str:
    if path in files:
        return files[path]
    for src, dst in dirs.items():
        if path.startswith(src + "/"):
            return dst + path[len(src) :]
    return path


def _misparsed(
    moves: list[UnitMove], paths: list[str], before: Layout, splits: list[str], joins: list[str]
) -> dict[str, str]:
    """Moves after which some file would not be in the pack it should: unit path -> why."""
    files = {r.src: r.dst for m in moves for r in m.renames if not r.is_dir}
    dirs = {r.src: r.dst for m in moves for r in m.renames if r.is_dir}
    moving = {m.path: m for m in moves}
    by_new = {m.new_path: m for m in moves}
    # A moved folder pack stays one pack, as the decision on it keeps it now; a folder
    # packs left stays a folder of packs, as the executor records (see left_folders).
    after_joins = [_relocate(j, {}, dirs) for j in joins] + list(dirs.values())
    after_splits = [_relocate(s, {}, dirs) for s in splits] + left_folders([m.renames[0] for m in moves])
    after = layout([_relocate(p, files, dirs) for p in paths], after_splits, after_joins, SCHEME_FOLDERS)
    bad: dict[str, str] = {}
    for path in paths:
        unit, is_dir = before.unit_of(path)
        move = moving.get(unit)
        expect = (move.new_path, is_dir) if move else (unit, is_dir)
        got = after.unit_of(_relocate(path, files, dirs))
        if got == expect:
            continue
        if move is not None:
            bad.setdefault(move.path, "移动后这个包会和别的文件混在一起，或者被拆开")
            continue
        captor = by_new.get(got[0])
        if captor is not None:
            bad.setdefault(captor.path, f"移动后会把「{path}」也并进这个包")
            continue
        # An unmoved pack parses differently: blame the moves into or out of its folder,
        # and if none, all of them (a batch that changes what it does not touch waits).
        folder = path.rpartition("/")[0]
        near = [m for m in moves if folder in (m.new_path.rpartition("/")[0], m.path.rpartition("/")[0])]
        for m in near or moves:
            bad.setdefault(m.path, f"移动后「{unit}」会被重新分包")
    return bad
