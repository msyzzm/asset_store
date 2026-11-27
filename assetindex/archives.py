"""List the files inside archives (zip, rar, 7z, unitypackage) without extracting them.

Archive contents are untrusted: names are cleaned, capped and filtered the same way
the filesystem scanner filters real paths. Nested archives are listed as plain entries,
never opened.
"""

import gzip
import re
import tarfile
import time
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import py7zr
import rarfile

from assetindex.kinds import LISTABLE_ARCHIVE_EXTS, kind_of
from assetindex.scanner import IGNORED_DIRS, is_utf8

ARCHIVE_EXTS = LISTABLE_ARCHIVE_EXTS
MAX_ENTRIES = 50_000  # per archive; beyond this the listing is marked truncated
MAX_PATH_LENGTH = 1024
MAX_PATHNAME_BYTES = 4096  # a unitypackage `pathname` member is one short line
ZIP_UTF8_FLAG = 0x800
ZIP_ENCRYPTED_FLAG = 0x1
# A unitypackage must be fully decompressed to list it; stop gzip bombs.
MAX_UNITYPACKAGE_BYTES = 32 * 1024**3
MAX_UNITYPACKAGE_SECONDS = 15 * 60  # wall clock per streaming pass
# Control characters and bidi overrides can disguise names in the UI ("gnp\u202eexe.png").
_UNSAFE_CHARS = re.compile(r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]")

# Compressed RAR comments need the external unrar tool; never spawn it while listing.
rarfile.NEED_COMMENTS = 0
UNREAL_EXTS = frozenset({"uasset", "umap"})
# DAZ Studio's own formats: scenes and presets, data, scripts, the install manifest.
DAZ_EXTS = frozenset({"duf", "dsf", "dsa", "dse", "dsx"})
# A folder named just like an engine version: "5.1", "UE5.3", "UE_4.27".
_UE_VERSION = re.compile(r"^(?:UE[ _-]?)?([45]\.\d{1,2})$", re.IGNORECASE)


class ArchiveError(Exception):
    """Archive is corrupt, encrypted or unsupported. Retrying won't help until it changes."""


@dataclass(frozen=True)
class ArchiveEntry:
    path: str
    size: int
    guid: str | None = None  # unitypackage asset folder, used to fetch its preview later
    has_preview: bool = False
    encrypted: bool = False  # its name lists, its content needs a password


@dataclass(frozen=True)
class ArchiveListing:
    entries: tuple[ArchiveEntry, ...]
    truncated: bool
    engine: str | None
    engine_version: str | None
    encrypted: int = 0  # entries whose content needs a password


def _clean(path: str) -> str | None:
    path = path.replace("\\", "/").lstrip("/")
    while path.startswith("./"):
        path = path[2:]
    if not path or len(path) > MAX_PATH_LENGTH or not is_utf8(path) or _UNSAFE_CHARS.search(path):
        return None
    parts = path.split("/")
    if any(part in IGNORED_DIRS or part == "__MACOSX" for part in parts):
        return None
    if parts[-1].startswith("._"):  # macOS resource forks, not real files
        return None
    return path


def _zip_name(info: zipfile.ZipInfo) -> str:
    """Python decodes names without the UTF-8 flag as cp437; most such names are really GBK."""
    name = info.filename
    if info.flag_bits & ZIP_UTF8_FLAG or name.isascii():
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return name


def _zip_entries(path: Path) -> Iterator[ArchiveEntry]:
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if not info.is_dir():
                yield ArchiveEntry(_zip_name(info), info.file_size, encrypted=bool(info.flag_bits & ZIP_ENCRYPTED_FLAG))


def _rar_entries(path: Path) -> Iterator[ArchiveEntry]:
    with rarfile.RarFile(path) as r:
        for info in r.infolist():
            if not info.is_dir():
                yield ArchiveEntry(info.filename, info.file_size, encrypted=bool(info.needs_password()))


def _7z_entries(path: Path) -> Iterator[ArchiveEntry]:
    with py7zr.SevenZipFile(path) as z:  # locked names: PasswordRequired, recorded as a bad archive
        locked = bool(z.needs_password())
        for info in z.list():
            if not info.is_directory:
                yield ArchiveEntry(info.filename, info.uncompressed or 0, encrypted=locked)


class _CappedReader:
    """File wrapper that raises ArchiveError past `limit` bytes or `seconds` of reading."""

    def __init__(self, raw, limit: int, seconds: float = MAX_UNITYPACKAGE_SECONDS) -> None:
        self._raw = raw
        self._left = limit
        self._deadline = time.monotonic() + seconds

    def read(self, size: int = -1) -> bytes:
        data = self._raw.read(size)
        self._left -= len(data)
        if self._left < 0:
            raise ArchiveError("unitypackage decompresses beyond the size limit")
        if time.monotonic() > self._deadline:
            raise ArchiveError("unitypackage took too long to read")
        return data


def _unitypackage_entries(path: Path) -> Iterator[ArchiveEntry]:
    """A unitypackage is a tar.gz of <guid>/{asset, pathname, preview.png} folders.
    Streamed ("r|gz") so a multi-GB package is never held in memory."""
    pathnames: dict[str, str] = {}
    sizes: dict[str, int] = {}
    previews: set[str] = set()
    # gzip.open + "r|" rather than "r|gz": tarfile's own stream decoder rejects some
    # gzip headers Unity writes ("invalid compressed data").
    with gzip.open(path) as gz, tarfile.open(
        fileobj=_CappedReader(gz, MAX_UNITYPACKAGE_BYTES), mode="r|"
    ) as tar:
        for member in tar:
            guid, _, part = member.name.removeprefix("./").partition("/")
            if part == "pathname" and member.isfile():
                raw = tar.extractfile(member).read(MAX_PATHNAME_BYTES)
                lines = raw.decode("utf-8", errors="replace").splitlines()
                if lines:
                    pathnames[guid] = lines[0]
            elif part == "asset" and member.isfile():
                sizes[guid] = member.size
            elif part == "preview.png":
                previews.add(guid)
    # Folders have a pathname but no asset; drop them like directory entries elsewhere.
    for guid in sorted(pathnames, key=pathnames.get):
        if guid in sizes:
            yield ArchiveEntry(pathnames[guid], sizes[guid], guid=guid, has_preview=guid in previews)


def read_unity_previews(path: Path, guids: set[str], max_bytes_each: int) -> dict[str, bytes]:
    """Bytes of `<guid>/preview.png` for the wanted guids, in one streaming pass."""
    found: dict[str, bytes] = {}
    if not guids:
        return found
    try:
        with gzip.open(path) as gz, tarfile.open(
            fileobj=_CappedReader(gz, MAX_UNITYPACKAGE_BYTES), mode="r|"
        ) as tar:
            for member in tar:
                guid, _, part = member.name.removeprefix("./").partition("/")
                if part != "preview.png" or guid not in guids or not member.isfile():
                    continue
                if member.size <= max_bytes_each:
                    found[guid] = tar.extractfile(member).read(max_bytes_each)
                if len(found) == len(guids):
                    break
    except (ArchiveError, MemoryError):
        raise
    except OSError as err:
        if isinstance(err, gzip.BadGzipFile):
            raise ArchiveError(str(err)) from err
        raise
    except Exception as err:  # tarfile/zlib raise many types for malformed input
        raise ArchiveError(f"{type(err).__name__}: {err}") from err
    return found


# The first bytes of each format: an archive is read by what it is, not by its name
# ("十三机兵.zip" can be a RAR someone renamed).
_SIGNATURES = (
    (b"PK\x03\x04", "zip"), (b"PK\x05\x06", "zip"),
    (b"Rar!\x1a\x07", "rar"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
)
READ_AS = "(按内容读作 "  # starts the error of an archive read by its content, not its name


def sniff_format(path: Path) -> str | None:
    """zip, rar or 7z by the file's first bytes, else None. OSError if it can't be read."""
    with path.open("rb") as f:
        head = f.read(8)
    return next((fmt for signature, fmt in _SIGNATURES if head.startswith(signature)), None)


_LISTERS = {
    "zip": _zip_entries,
    "rar": _rar_entries,
    "7z": _7z_entries,
    "unitypackage": _unitypackage_entries,
}


def _collect(raw: Iterable[ArchiveEntry]) -> tuple[list[ArchiveEntry], bool]:
    entries: list[ArchiveEntry] = []
    for entry in raw:
        path = _clean(entry.path)
        if path is None:
            continue
        if len(entries) >= MAX_ENTRIES:
            return entries, True
        entries.append(ArchiveEntry(path, max(0, entry.size), entry.guid, entry.has_preview, entry.encrypted))
    return entries, False


def detect_engine(ext: str, paths: Iterable[str]) -> tuple[str | None, str | None]:
    """Guess which engine an archive is for. The UE version is inferred from folder names."""
    if ext == "unitypackage":
        return "unity", None
    paths = list(paths)
    suffixes = {p.rsplit(".", 1)[-1].lower() for p in paths if "." in p.rsplit("/", 1)[-1]}
    if suffixes & UNREAL_EXTS:
        for p in paths:
            for folder in p.split("/")[:-1]:
                match = _UE_VERSION.match(folder)
                if match:
                    return "unreal", match.group(1)
        return "unreal", None
    has_unity_assets = "meta" in suffixes and any("Assets" in p.split("/")[:-1] for p in paths)
    # A .unitypackage beside plain models is a bonus (Kenney packs): the pack is for any engine.
    unity_only = "unitypackage" in suffixes and not any(kind_of(s) == "model" for s in suffixes)
    if unity_only or has_unity_assets:
        return "unity", None
    if suffixes & DAZ_EXTS:
        return "daz", None
    return None, None


def list_archive(path: Path, ext: str) -> ArchiveListing:
    """Raise OSError if the file can't be read (retry later), ArchiveError if it's bad."""
    fmt = ext if ext == "unitypackage" else (_sniff_or_none(path) or ext)
    lister = _LISTERS.get(fmt)
    if lister is None:
        raise ArchiveError(f"unsupported archive type {ext!r}")
    try:
        entries, truncated = _read(lister, path)
    except ArchiveError as err:
        if fmt != ext:
            raise ArchiveError(f"{READ_AS}{fmt}) {err}") from err
        raise
    engine, version = detect_engine(ext, (e.path for e in entries))
    return ArchiveListing(tuple(entries), truncated, engine, version, sum(e.encrypted for e in entries))


def encrypted_count(path: Path, ext: str) -> int:
    """How many files in an archive need a password, read from its directory only (an
    archive listed before this was counted keeps its entries and cover). zip, rar and 7z
    mark it; a unitypackage or tarball cannot be locked. Raises like list_archive."""
    fmt = ext if ext == "unitypackage" else (_sniff_or_none(path) or ext)
    if fmt not in ("zip", "rar", "7z"):
        return 0
    entries, _ = _read(_LISTERS[fmt], path)
    return sum(e.encrypted for e in entries)


def _sniff_or_none(path: Path) -> str | None:
    try:
        return sniff_format(path)
    except OSError:  # the lister meets the same error and reports it
        return None


def _read(lister, path: Path) -> tuple[list[ArchiveEntry], bool]:
    try:
        return _collect(lister(path))
    except (ArchiveError, MemoryError):
        raise  # MemoryError is transient: the caller retries on the next scan
    except OSError as err:
        # gzip reports corrupt data as an OSError subclass; only real I/O errors are transient.
        if isinstance(err, gzip.BadGzipFile):
            raise ArchiveError(str(err)) from err
        raise
    except Exception as err:  # third-party parsers raise many types for malformed input
        raise ArchiveError(f"{type(err).__name__}: {err}") from err
