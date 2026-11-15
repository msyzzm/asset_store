from pathlib import Path

import pytest
from PIL import Image

from assetindex.config import Settings
from assetindex.db import connect


def _write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def asset_root(tmp_path: Path) -> Path:
    root = tmp_path / "assets"
    _write(root / "特效" / "刀剑特效.7z", b"7z-bytes")
    _write(root / "Audio" / "Battle_Theme.mp3", b"0123456789" * 100)
    _write(root / "3D" / "Knight" / "knight_sword.fbx")
    _write(root / "readme.txt")
    _write(root / "tool" / "node_modules" / "junk.js")
    img = root / "2D" / "Icons" / "sword_icon.png"
    img.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (64, 32), (255, 0, 0, 255)).save(img)
    return root


@pytest.fixture
def settings(tmp_path: Path, asset_root: Path) -> Settings:
    return Settings(roots={"art": asset_root}, data_dir=tmp_path / "data")


@pytest.fixture
def conn(settings: Settings):
    connection = connect(settings.db_path)
    yield connection
    connection.close()
