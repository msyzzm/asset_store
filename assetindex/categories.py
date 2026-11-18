"""The library layout the organizer sorts packs into (after the Unity Asset Store's
categories). Engine is not a folder: a Unity character pack is a character pack; the
engine shows as a badge."""

CATEGORIES = (
    ("01_2D/Characters", "2D 角色、怪物、立绘、头像、角色序列帧"),
    ("01_2D/Environments", "2D 场景、背景、地图、Tileset 图块"),
    ("01_2D/Props", "2D 道具、物品、武器、装备"),
    ("01_2D/UI", "界面、按钮、窗口、图标、字体"),
    ("01_2D/VFX", "2D 特效：序列帧、特效图片（不是引擎里的粒子系统）"),
    ("01_2D/Textures", "贴图、材质、纹理、无缝图、HDR 天空和环境光图（.hdr/.exr）"),
    ("02_3D/Characters", "3D 角色、怪物、人物模型"),
    ("02_3D/Environments", "3D 场景、建筑、地形"),
    ("02_3D/Props", "3D 道具、物品、武器"),
    ("02_3D/Vehicles", "3D 载具、交通工具"),
    ("02_3D/Vegetation", "3D 植物、树木"),
    ("02_3D/VFX", "3D 特效：UE 的 Niagara/Cascade 粒子、Unity 粒子特效 prefab、特效蓝图和材质"),
    ("02_3D/Animations", "动作、动画、动捕数据"),
    ("03_Audio/Music", "音乐、BGM"),
    ("03_Audio/SFX", "音效"),
    ("03_Audio/Ambient", "环境音"),
    ("03_Audio/Voice", "语音、配音"),
    ("04_Tools/Shaders", "Shader、材质效果、后处理"),
    ("04_Tools/Plugins", "引擎插件、脚本、工具"),
    ("04_Tools/Templates", "项目模板、完整工程"),
    ("05_Reference", "参考资料、设定集、原画、照片、教程"),
)
UNSORTED = "_Unsorted"  # the model couldn't tell, or answered outside the list
DUPLICATES = "_Duplicates"  # extra copies of identical units
TO_DELETE = "_待删除"  # set aside by hand, to be deleted in the NAS's own file manager (its recycle bin works there)
CATEGORY_PATHS = frozenset(path for path, _ in CATEGORIES) | {UNSORTED}
DOWNLOADS = "_下载"  # where the fetcher puts finished downloads, one pack each, to be sorted like any other
PARTIAL = ".partial"  # under DOWNLOADS: downloads still running, never scanned


def _with_parents(paths) -> frozenset[str]:
    out = set()
    for path in paths:
        parts = path.split("/")
        out.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    return frozenset(out)


# The scheme's folders hold packs by definition: a sorted pack there is never mistaken
# for part of a bigger one, however few packs a category has.
SCHEME_FOLDERS = _with_parents([*CATEGORY_PATHS, DUPLICATES, TO_DELETE, DOWNLOADS])
