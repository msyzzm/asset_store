"""`python -m assetindex check`: verify a deployment can actually do its job.

Meant to be run once inside the container after installing or upgrading, since the
things that break there (missing bsdtar, wrong locale for non-ASCII names, NAS not
mounted, unwritable volume) all fail quietly at scan time.
"""

import mimetypes
import os
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from assetindex.config import Settings
from assetindex.db import connect
from assetindex.embedding import load_config, probe
from assetindex.extract import ExtractError, find_bsdtar, read_member

STATIC_DIR = Path(__file__).parent / "static"
VIEWER_FILES = ("model-viewer.js", "vendor/three/build/three.module.min.js")
# Non-ASCII plus glob characters: both have broken bsdtar member lookups before.
PROBE_MEMBER = "素材/ドラゴン [1].txt"
PROBE_DATA = b"ok"


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def _data_dir(settings: Settings) -> CheckResult:
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=settings.data_dir):
            pass
    except OSError as err:
        return CheckResult("data dir writable", False, f"{settings.data_dir}: {err}")
    return CheckResult("data dir writable", True, str(settings.data_dir))


def _database(settings: Settings) -> CheckResult:
    try:
        conn = connect(settings.db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        finally:
            conn.close()
    except Exception as err:  # sqlite3.Error, OSError: report, don't crash the check
        return CheckResult("database", False, f"{settings.db_path}: {err!r}")
    return CheckResult("database", True, f"{count} files indexed")


def _root(name: str, path: Path) -> CheckResult:
    label = f"root {name} readable"
    try:
        with os.scandir(path) as entries:
            has_entries = next(entries, None) is not None
    except OSError as err:
        return CheckResult(label, False, f"{path}: {err}")
    if not has_entries:
        return CheckResult(label, False, f"{path} is empty (share not mounted?)")
    return CheckResult(label, True, str(path))


def _bsdtar() -> list[CheckResult]:
    tool = find_bsdtar()
    if tool is None:
        return [CheckResult("bsdtar", False, "not found: zip/rar/7z previews disabled (install libarchive-tools)")]
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.zip"
        with zipfile.ZipFile(probe, "w") as z:
            z.writestr(PROBE_MEMBER, PROBE_DATA)
        try:
            data = read_member(probe, PROBE_MEMBER, 1024, tool)
        except ExtractError as err:
            data = str(err).encode()
    ok = data == PROBE_DATA
    detail = "ok" if ok else f"got {data[:80]!r}; check the locale (LC_ALL=C.UTF-8)"
    return [CheckResult("bsdtar", True, tool), CheckResult("bsdtar non-ASCII names", ok, detail)]


def _viewer() -> CheckResult:
    missing = [f for f in VIEWER_FILES if not (STATIC_DIR / f).is_file()]
    if missing:
        return CheckResult("3D viewer files", False, f"missing {', '.join(missing)}")
    js_type = mimetypes.guess_type("x.js")[0] or ""
    if "javascript" not in js_type:
        return CheckResult("3D viewer files", False, f".js served as {js_type!r}; browsers will refuse it")
    return CheckResult("3D viewer files", True, str(STATIC_DIR / "vendor"))


def _embedding(settings: Settings) -> list[CheckResult]:
    """Only once semantic search is switched on in the settings page."""
    config = load_config(settings.data_dir)
    if not config.active:
        return []
    result = probe(config)
    checks = [("embedding API", result["text"])]
    if result["image"] is not None:
        checks.append(("embedding API (images)", result["image"]))
    return [
        CheckResult(name, r["ok"], f"{config.base_url} {config.model}: " + (f"{r['dim']} dims" if r["ok"] else r["error"]))
        for name, r in checks
    ]


def run_checks(settings: Settings) -> list[CheckResult]:
    return [
        _data_dir(settings),
        _database(settings),
        *(_root(name, path) for name, path in settings.roots.items()),
        *_bsdtar(),
        _viewer(),
        *_embedding(settings),
    ]
