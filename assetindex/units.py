"""Split a root into units ("packs"): the things the organizer proposes to move.

Invariant: a unit is exactly one file or one directory, units never nest, and every
indexed file belongs to exactly one unit. Moving a unit is then a single rename, and a
pack (a downloaded asset pack, a game's art dump) always stays together.

The default rule, from how asset libraries tend to look:
- a file directly in the root is a unit;
- a top-level folder named like a category or a store ("2D", "Audio", "itch") is a
  container: each file and folder in it is a unit; any other top-level folder is a unit;
- inside a container, a folder named like a collection ("合集", "系列", "Collection",
  "OpenGameArt") is opened one more level.
The user can split any unit into its children ("split" overrides) when the guess is wrong.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

# Top-level folder names that group packs rather than being one.
_CONTAINER = re.compile(
    r"^(\d+[_ .-]*)?(2d|3d|2dpixel|pixel|pixel ?art|sprites?|textures?|materials?|models?|audio|music|sounds?|"
    r"sfx|bgm|vfx|effects?|fx|ui|gui|icons?|fonts?|characters?|environments?|props?|animations?|"
    r"itch(\.io)?|unity|unreal|ue[45]?|asset ?store|marketplace|downloads?|packs?|assets?|"
    r"素材|资源|资产|美术|角色|场景|特效|音效|音乐|模型|图标|字体|动画)$",
    re.IGNORECASE,
)
# Folder names that mark a collection of packs.
_COLLECTION = re.compile(r"合集|合辑|系列|大全|打包|collection|bundle|series|humble|opengameart|\bpacks\b", re.IGNORECASE)


@dataclass(frozen=True)
class Unit:
    path: str  # relative to the root, "/"-separated
    is_dir: bool
    files: int
    size: int
    collection_hint: bool = False  # looks like several packs: worth splitting


def _is_container(name: str) -> bool:
    return bool(_CONTAINER.match(name.strip()))


def _is_collection(name: str) -> bool:
    return bool(_COLLECTION.search(name))


def unit_of(rel_path: str, opened: frozenset[str]) -> tuple[str, bool]:
    """The unit a file belongs to: (unit path, is_dir). `opened` holds the folders whose
    children are units (containers, collections, user splits)."""
    parts = rel_path.split("/")
    depth = 0
    while depth < len(parts) - 1 and "/".join(parts[: depth + 1]) in opened:
        depth += 1
    if depth == len(parts) - 1:
        return rel_path, False  # a file directly in an opened folder
    return "/".join(parts[: depth + 1]), True


def opened_folders(paths: Iterable[str], splits: Iterable[str] = ()) -> frozenset[str]:
    """Folders whose children are units: by the default rule, plus the user's splits."""
    paths = list(paths)
    top = {p.split("/", 1)[0] for p in paths if "/" in p}
    opened = {t for t in top if _is_container(t)}
    second = {"/".join(p.split("/")[:2]) for p in paths if p.count("/") >= 2 and p.split("/", 1)[0] in opened}
    opened |= {s for s in second if _is_collection(s.rsplit("/", 1)[1])}
    return frozenset(opened | set(splits))


def compute_units(files: Iterable[tuple[str, int]], splits: Iterable[str] = ()) -> list[Unit]:
    """Units of one root from its indexed (rel_path, size) pairs, sorted by path."""
    files = list(files)
    opened = opened_folders((p for p, _ in files), splits)
    counts: dict[tuple[str, bool], list[int]] = {}
    for path, size in files:
        key = unit_of(path, opened)
        acc = counts.setdefault(key, [0, 0])
        acc[0] += 1
        acc[1] += size
    return [
        Unit(path, is_dir, n, size, collection_hint=is_dir and _is_collection(path.rsplit("/", 1)[-1]))
        for (path, is_dir), (n, size) in sorted(counts.items())
    ]
