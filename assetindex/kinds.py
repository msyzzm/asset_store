"""Map file extensions to coarse asset kinds."""

_KIND_EXTS: dict[str, frozenset[str]] = {
    "image": frozenset({
        "png", "jpg", "jpeg", "gif", "webp", "bmp", "tga", "psd",
        "tif", "tiff", "exr", "hdr", "dds", "svg", "aseprite", "eps",
    }),
    "audio": frozenset({"mp3", "wav", "ogg", "flac", "m4a", "aac", "aif", "aiff"}),
    "video": frozenset({"mp4", "webm", "mov", "avi", "mkv"}),
    "model": frozenset({"fbx", "obj", "gltf", "glb", "blend", "max", "ma", "mb", "3ds", "dae", "stl"}),
    "archive": frozenset({"zip", "rar", "7z", "unitypackage", "tar", "gz", "tgz"}),
    "unreal": frozenset({"uasset", "umap", "uproject", "uplugin"}),
}

_EXT_TO_KIND: dict[str, str] = {
    ext: kind for kind, exts in _KIND_EXTS.items() for ext in exts
}

KINDS: tuple[str, ...] = (*_KIND_EXTS.keys(), "other")

# Formats Pillow can decode into a thumbnail.
THUMBNAIL_EXTS: frozenset[str] = frozenset({
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "tga", "psd", "tif", "tiff",
})


# Archives whose contents are listed and previewed.
LISTABLE_ARCHIVE_EXTS: frozenset[str] = frozenset({"zip", "rar", "7z", "unitypackage"})


def kind_of(ext: str) -> str:
    return _EXT_TO_KIND.get(ext.lower(), "other")
