"""Split a root into units ("packs"): the things the organizer proposes to move.

Invariant: a unit is one directory, or one file together with its companions (the other
volumes of a split archive, a same-name preview image or readme next to an archive, or in
a subfolder that holds nothing but such previews, like PM895/JPEG/<pack>.jpg).
Units never nest, and every indexed file belongs to exactly one unit. Moving a unit is
then a rename or two, and a pack (a downloaded asset pack, a game's art dump) always
stays together.

A folder is "opened" (each file and folder in it is a unit of its own) when:
- it is a top-level folder named like a category or a store ("2D", "Audio", "itch", "UE");
- it is a folder inside one of those named like a collection ("合集", "系列", "Collection");
- it holds MIN_PACKS or more differently named archives, directly or in its subfolders
  (a split one counts once). Libraries sorted by hand look like that:
  UE/道具/<pack>.rar + <pack>.png;
- it holds an opened folder (then it cannot be one pack itself);
- the user split it.
The user can also join a folder back into one pack, which overrides the rules above.
"""

import os
import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from assetindex.categories import SCHEME_FOLDERS

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
ARCHIVE_EXTS = frozenset({"zip", "rar", "7z", "unitypackage"})
# Volumes of a split archive: name.part2.rar, name.r00, name.z01, name.7z.001, name.zip.001
_VOLUME = re.compile(r"^(?P<stem>.+?)(\.part\d+\.rar|\.r\d\d|\.z\d\d|\.(7z|zip)\.\d{3})$", re.IGNORECASE)
_COPY_SUFFIX = re.compile(r"\s*[(（]\d+[)）]$")  # "Nature (1).png" previews "Nature.rar"
_SEPARATORS = re.compile(r"[\s_\-.,()\[\]（）【】]+")
_VERSION = re.compile(r"v?\d+(?:[._]\d+)*", re.IGNORECASE)
_README = re.compile(r"^(read ?me|说明|解压|密码)", re.IGNORECASE)
# What may go with an archive whose name it only starts like: previews and notes, not assets.
NOTE_EXTS = frozenset({"png", "jpg", "jpeg", "webp", "gif", "bmp", "txt", "pdf", "url", "html", "htm", "md"})
MIN_PACKS = 3  # two archives are often one pack (two engine versions, two parts)
MIN_SHARED = 6  # names that all start alike are variants of one pack: Cabin_Unity, Cabin_Unreal
MIN_PREFIX = 4  # "Megapack.jpg" previews "Megapack 4.26.rar"; "A.png" previews nothing by prefix


@dataclass(frozen=True)
class Unit:
    path: str  # relative to the root, "/"-separated
    is_dir: bool
    files: int
    size: int
    collection_hint: bool = False  # looks like several packs: worth splitting


@dataclass(frozen=True)
class Layout:
    """How a root splits into units."""

    opened: frozenset[str] = frozenset()  # folders whose children are units
    companions: Mapping[str, str] = field(default_factory=dict)  # file -> the file it goes with

    def unit_of(self, rel_path: str) -> tuple[str, bool]:
        """The unit a file belongs to: (unit path, is_dir)."""
        parts = rel_path.split("/")
        depth = 0
        while depth < len(parts) - 1 and "/".join(parts[: depth + 1]) in self.opened:
            depth += 1
        main = self.companions.get(rel_path)
        if main is not None:
            return main, False
        if depth == len(parts) - 1:  # a file directly in an opened folder (or the root)
            return rel_path, False
        return "/".join(parts[: depth + 1]), True


def _is_container(name: str) -> bool:
    return bool(_CONTAINER.match(name.strip()))


def is_container_name(name: str) -> bool:
    """A top-level folder named like a category or a store ("UE", "2D", "itch")."""
    return _is_container(name)


def _is_collection(name: str) -> bool:
    return bool(_COLLECTION.search(name))


def archive_stem(name: str) -> str | None:
    """The pack name of an archive or archive volume, else None."""
    volume = _VOLUME.match(name)
    if volume:
        return volume["stem"]
    stem, dot, ext = name.rpartition(".")
    return stem if dot and stem and ext.lower() in ARCHIVE_EXTS else None


_VOLUME_FORMAT = {"p": "rar", "r": "rar", "z": "zip", "7": "7z"}  # by the volume suffix's first letter


def _archive_key(name: str) -> tuple[str, str] | None:
    """(pack name, format) of an archive or archive volume, else None: "X.part2.rar" and
    "X.r00" are volumes of a rar, "X.z01" and "X.zip.001" of a zip, "X.7z.001" of a 7z."""
    volume = _VOLUME.match(name)
    if volume:
        return volume["stem"], _VOLUME_FORMAT[name[len(volume["stem"]) + 1].lower()]
    stem, dot, ext = name.rpartition(".")
    return (stem, ext.lower()) if dot and stem and ext.lower() in ARCHIVE_EXTS else None


def pair_companions(names: Iterable[str]) -> dict[str, str]:
    """Within one folder: each file that goes with an archive -> that archive's name. The
    other volumes go with the first one; a non-archive goes with the archive of the same
    name, else the shortest one whose name starts like it (separators and case ignored)."""
    names = list(names)
    # Volumes follow the first one of their own format; "X.rar" and "X.zip" are two packs (often a copy).
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for name in names:
        key = _archive_key(name)
        if key is not None:
            groups[key].append(name)
    lead = {key: min(group, key=lambda n: (bool(_VOLUME.match(n)), n)) for key, group in groups.items()}
    pairs = {name: lead[key] for key, group in groups.items() for name in group if name != lead[key]}
    main: dict[str, str] = {}  # pack name -> the archive its previews go with (7z, then rar, then zip...)
    for (stem, _), first in sorted(lead.items()):
        main.setdefault(stem, first)
    exact: dict[str, str] = {}
    for stem in sorted(main):
        exact.setdefault(_squash(stem), stem)
    by_length = sorted(exact.items(), key=lambda s: (-len(s[0]), s[0]))  # longest first
    for name in names:
        if archive_stem(name) is not None:
            continue
        base, dot, ext = name.rpartition(".")
        key = _squash(_COPY_SUFFIX.sub("", base if dot else name))
        if not key:
            continue
        stem = exact.get(key)
        if stem is None and dot and ext.lower() in NOTE_EXTS and len(key) >= MIN_PREFIX:
            # "Megapack.jpg" for "Megapack 4.26.rar", "Warroom Environment.png" for "Warroom.rar"
            stem = next(
                (s for low, s in by_length if len(low) >= MIN_PREFIX and (low.startswith(key) or key.startswith(low))),
                None,
            )
        if stem is not None:
            pairs[name] = main[stem]
    return pairs


def folder_companions(here: list[str], below: Mapping[str, list[str]]) -> dict[str, str]:
    """One opened folder's companions, paths relative to it: files here that go with an
    archive here, and every file of a preview folder. `below` maps the folder's direct
    subfolders that have no subfolders of their own to their file names; one whose files
    are all previews or notes of archives here ("JPEG/(1)_x.jpg" for "(1)_x.zip") is a
    preview folder."""
    pairs = pair_companions(here)
    main = {}  # squashed stem -> the archive a preview of it goes with
    for name in sorted(here):
        stem = archive_stem(name)
        if stem is not None:
            main.setdefault(_squash(stem), pairs.get(name, name))
    if not main:
        return pairs
    for sub, names in below.items():
        # Exact names only: "Tiles/tile_001.png" next to "Tile.zip" is a folder of tiles.
        keys = {name: _squash(_COPY_SUFFIX.sub("", name.rpartition(".")[0])) for name in names}
        if names and all(name.rpartition(".")[2].lower() in NOTE_EXTS and keys[name] in main for name in names):
            pairs.update({f"{sub}/{name}": main[keys[name]] for name in names})
    return pairs


def preview_key(stem: str) -> str:
    """What previews are matched to a pack by: "X (2).png" and "x-.png" both go with "X.zip"."""
    return _squash(_COPY_SUFFIX.sub("", stem))


def _squash(name: str) -> str:
    """Lower case without separators: "Hong_Kong Street" and "hong kong-street" match."""
    return _SEPARATORS.sub("", name).lower()


def _pack_name(stem: str) -> str | None:
    """An archive's name with versions dropped, None for a readme or password note."""
    if _README.match(stem.strip()):
        return None
    return _squash(_VERSION.sub("", stem)) or None


def _tree(paths: list[str]) -> tuple[dict[str, list[str]], dict[str, set[str]]]:
    """Direct file names and direct subfolders of every folder ("" = the root)."""
    files: dict[str, list[str]] = defaultdict(list)
    subdirs: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        folder, _, name = path.rpartition("/")
        files[folder].append(name)
        while folder:
            parent = folder.rpartition("/")[0]
            if folder in subdirs[parent]:
                break
            subdirs[parent].add(folder)
            folder = parent
    return files, subdirs


def _archive_names(folder: str, files: dict[str, list[str]]) -> set[str]:
    stems = (archive_stem(name) for name in files.get(folder, ()))
    return {name for stem in stems if stem is not None and (name := _pack_name(stem))}


def _holds_packs(folder: str, files: dict[str, list[str]], subdirs: dict[str, set[str]]) -> bool:
    """MIN_PACKS or more differently named archives, directly or one folder down. Names
    that all start alike are one pack's variants (engine versions in subfolders included)."""
    names = _archive_names(folder, files)
    for sub in subdirs.get(folder, ()):
        names |= _archive_names(sub, files)
    if len(names) < MIN_PACKS:
        return False
    return len(os.path.commonprefix(list(names))) < MIN_SHARED


def layout(
    paths: Iterable[str], splits: Iterable[str] = (), joins: Iterable[str] = (), always_open: Iterable[str] = ()
) -> Layout:
    """The units of one root, from its indexed paths, by the rules above plus the user's
    splits (opened) and joins (kept as one pack). `always_open` are folders that hold
    packs by definition: the organizer's category folders, which a sorted pack lands in."""
    paths = list(paths)
    files, subdirs = _tree(paths)
    folders = {d for ds in subdirs.values() for d in ds}
    joins = frozenset(joins)

    def joined(folder: str) -> bool:
        parts = folder.split("/")
        return any("/".join(parts[:i]) in joins for i in range(1, len(parts) + 1))

    top = {d for d in folders if "/" not in d and _is_container(d)}
    collections = {d for d in folders if d.count("/") == 1 and d.split("/")[0] in top and _is_collection(d.split("/")[1])}
    packed = {d for d in folders if _holds_packs(d, files, subdirs)}
    # A joined folder's parent stays open, or the join would make the parent the pack.
    parents = {j.rpartition("/")[0] for j in joins if "/" in j}
    opened = {d for d in top | collections | packed if not joined(d)} | parents | set(splits) | set(always_open)
    for folder in list(opened):  # a folder holding an opened folder is not one pack
        while "/" in folder:
            folder = folder.rpartition("/")[0]
            opened.add(folder)
    companions = {}
    for folder in opened | {""}:
        prefix = folder + "/" if folder else ""
        below = {  # a folder kept as one pack stays one
            sub.rpartition("/")[2]: files.get(sub, [])
            for sub in subdirs.get(folder, ()) if sub not in opened and not subdirs.get(sub) and not joined(sub)
        }
        for name, main in folder_companions(files.get(folder, []), below).items():
            companions[prefix + name] = prefix + main
    return Layout(frozenset(opened), companions)



def stored_splits(conn: sqlite3.Connection, root: str) -> list[str]:
    return [r[0] for r in conn.execute("SELECT path FROM unit_splits WHERE root = ?", (root,))]


def stored_joins(conn: sqlite3.Connection, root: str) -> list[str]:
    """Folders kept as one pack: joined by the user, or a folder pack already decided on
    (a rule change must not take a confirmed pack apart)."""
    return [
        r[0] for r in conn.execute(
            "SELECT path FROM unit_joins WHERE root = ?"
            " UNION SELECT path FROM organize_units WHERE root = ? AND is_dir = 1 AND status IN ('accepted', 'rejected')",
            (root, root),
        )
    ]


def stored_layout(conn: sqlite3.Connection, root: str) -> tuple[list[str], Layout]:
    """A root's indexed paths and how they split into units, with the user's splits and joins."""
    paths = [r[0] for r in conn.execute("SELECT rel_path FROM files WHERE root = ?", (root,))]
    return paths, layout(paths, stored_splits(conn, root), stored_joins(conn, root), SCHEME_FOLDERS)


def companions_by_unit(paths: Iterable[str], shape: Layout) -> dict[str, list[str]]:
    """File unit -> its companions, sorted."""
    found: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        unit, is_dir = shape.unit_of(path)
        if not is_dir and unit != path:
            found[unit].append(path)
    return {unit: sorted(members) for unit, members in found.items()}


def compute_units(files: Iterable[tuple[str, int]], splits: Iterable[str] = (), joins: Iterable[str] = ()) -> list[Unit]:
    """Units of one root from its indexed (rel_path, size) pairs, sorted by path."""
    files = list(files)
    shape = layout((p for p, _ in files), splits, joins)
    counts: dict[tuple[str, bool], list[int]] = {}
    for path, size in files:
        acc = counts.setdefault(shape.unit_of(path), [0, 0])
        acc[0] += 1
        acc[1] += size
    return [
        Unit(path, is_dir, n, size, collection_hint=is_dir and _is_collection(path.rsplit("/", 1)[-1]))
        for (path, is_dir), (n, size) in sorted(counts.items())
    ]
