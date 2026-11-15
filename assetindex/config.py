"""Runtime settings, read from environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA_DIR = "data"
DEFAULT_THUMB_CACHE_MB = 4096


@dataclass(frozen=True)
class Settings:
    roots: dict[str, Path]
    data_dir: Path
    # Cap for thumbnails rendered on demand; archive contact sheets are not counted.
    thumb_cache_bytes: int = DEFAULT_THUMB_CACHE_MB * 1024**2

    @property
    def db_path(self) -> Path:
        return self.data_dir / "index.db"

    @property
    def thumb_dir(self) -> Path:
        return self.data_dir / "thumbs"


def parse_roots(spec: str) -> dict[str, Path]:
    """Parse 'name=path;name2=path2' into {name: Path}."""
    entries = [e.strip() for e in spec.split(";") if e.strip()]
    if not entries:
        raise ValueError("ASSET_ROOTS is empty; expected 'name=path;name2=path2'")
    roots: dict[str, Path] = {}
    for entry in entries:
        name, sep, path = entry.partition("=")
        if not sep or not name.strip() or not path.strip():
            raise ValueError(f"Invalid ASSET_ROOTS entry {entry!r}; expected 'name=path'")
        roots[name.strip()] = Path(path.strip())
    return roots


def load_settings() -> Settings:
    spec = os.environ.get("ASSET_ROOTS")
    if spec is None:
        raise ValueError("ASSET_ROOTS env var is required, e.g. 'local=E:\\ArtAssets;nas=/assets'")
    data_dir = Path(os.environ.get("ASSET_DATA_DIR", DEFAULT_DATA_DIR))
    cache_mb = os.environ.get("ASSET_THUMB_CACHE_MB", str(DEFAULT_THUMB_CACHE_MB))
    if not cache_mb.isdigit():
        raise ValueError(f"ASSET_THUMB_CACHE_MB must be a whole number of megabytes, got {cache_mb!r}")
    return Settings(roots=parse_roots(spec), data_dir=data_dir, thumb_cache_bytes=int(cache_mb) * 1024**2)
