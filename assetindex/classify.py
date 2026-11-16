"""Ask the vision model where a unit (pack) belongs and what to call it.

The model sees what the index knows about the pack (path, file types, names inside,
picture descriptions) and one picture: the archive's contact sheet, or a sheet made from
a few of the folder's images. Its answer only becomes a proposal after validation: the
category must be one of CATEGORIES, and the name is cleaned into a safe folder name.
"""

import io
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath

from PIL import Image

from assetindex.archive_previews import choose_candidates
from assetindex.caption import CaptionClient, clean_caption
from assetindex.caption_job import save_caption
from assetindex.config import Settings
from assetindex.embed_stage import BACKGROUND, JPEG_QUALITY, _has_image, card_jpeg
from assetindex.embedding import EmbedError
from assetindex.kinds import LISTABLE_ARCHIVE_EXTS, THUMBNAIL_EXTS
from assetindex.thumbs import file_thumb_path, open_image, sheet_image, sheet_path
from assetindex.units import pair_companions

# The library layout (after the Unity Asset Store's categories). Engine is not a folder:
# a Unity character pack is a character pack; the engine shows as a badge.
CATEGORIES = (
    ("01_2D/Characters", "2D 角色、怪物、立绘、头像、角色序列帧"),
    ("01_2D/Environments", "2D 场景、背景、地图、Tileset 图块"),
    ("01_2D/Props", "2D 道具、物品、武器、装备"),
    ("01_2D/UI", "界面、按钮、窗口、图标、字体"),
    ("01_2D/VFX", "2D 特效、技能特效序列帧"),
    ("01_2D/Textures", "贴图、材质、纹理、无缝图"),
    ("02_3D/Characters", "3D 角色、怪物、人物模型"),
    ("02_3D/Environments", "3D 场景、建筑、地形"),
    ("02_3D/Props", "3D 道具、物品、武器"),
    ("02_3D/Vehicles", "3D 载具、交通工具"),
    ("02_3D/Vegetation", "3D 植物、树木"),
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
CATEGORY_PATHS = frozenset(path for path, _ in CATEGORIES) | {UNSORTED}

MAX_NAME_CHARS = 60
SAMPLE_NAMES = 40
SHEET_CELLS = 9
CELL_BOX = (160, 160)
PICTURE_BOX = (512, 512)
ANSWER_TOKENS = 300
CAPTION_TOKENS = 300  # extra room when the answer also describes the picture (Chinese: ~1.5 tokens a character)
ITEM_PROBLEM = 422  # an answer we can't use counts against this unit, like a refused picture
_BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_DIGITS = re.compile(r"\d+")
_NUMBERED = re.compile(r"^[\W\d_]+$")  # "127327921", "(12)": says nothing about the content


@dataclass(frozen=True)
class Proposal:
    category: str
    name: str
    reason: str
    caption: str = ""  # the picture described, when asked for (one-file packs without one)


def under(path: str) -> tuple[str, str]:
    """[low, high) bounds of every path inside folder `path`, for an indexed range query
    ("/" sorts right before "0")."""
    return path + "/", path + "0"


def unit_members(conn: sqlite3.Connection, root: str, path: str) -> list[str]:
    """A file unit's files: the file, then its companions (other volumes, a same-name
    preview), paired as assetindex.units pairs them."""
    folder, _, name = path.rpartition("/")
    if folder:
        low, high = under(folder)
        rows = conn.execute(
            "SELECT rel_path FROM files WHERE root = ? AND rel_path >= ? AND rel_path < ?"
            " AND instr(substr(rel_path, ?), '/') = 0",
            (root, low, high, len(low) + 1),
        )
    else:
        rows = conn.execute("SELECT rel_path FROM files WHERE root = ? AND instr(rel_path, '/') = 0", (root,))
    prefix = folder + "/" if folder else ""
    names = [r[0][len(prefix) :] for r in rows]
    return [path, *sorted(prefix + n for n, main in pair_companions(names).items() if main == name)]


def _unit_rows(conn: sqlite3.Connection, root: str, path: str, is_dir: bool) -> list[sqlite3.Row]:
    """The unit's files; for a file unit the file itself first."""
    if is_dir:
        where, params = "f.rel_path >= ? AND f.rel_path < ?", under(path)
    else:
        params = tuple(unit_members(conn, root, path))
        where = f"f.rel_path IN ({','.join('?' * len(params))})"
    return conn.execute(
        f"""
        SELECT f.id, f.root, f.rel_path, f.ext, f.size, f.mtime, f.seq_frames, a.preview_state,
               a.engine, a.engine_version
        FROM files f LEFT JOIN archives a ON a.file_id = f.id
        WHERE f.root = ? AND {where} ORDER BY f.rel_path != ?, f.rel_path
        """,
        (root, *params, path),
    ).fetchall()


def _readable_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return str(size)


def _sample_names(paths: list[str]) -> list[str]:
    """Distinct names with numbers collapsed: walk_01..walk_40 says "walk_#" once."""
    seen: dict[str, None] = {}
    for p in paths:
        pure = PurePosixPath(p.replace("\\", "/"))
        parent = pure.parent.name
        key = _DIGITS.sub("#", (f"{parent}/" if parent else "") + pure.stem)
        seen.setdefault(key)
        if len(seen) >= SAMPLE_NAMES:
            break
    return list(seen)


def digest(conn: sqlite3.Connection, root: str, path: str, is_dir: bool) -> str:
    """What the index knows about a unit, as text for the model."""
    rows = _unit_rows(conn, root, path, is_dir)
    if is_dir:
        inner = [r["rel_path"][len(path) + 1 :] for r in rows]
        exts = Counter(r["ext"] or "(无)" for r in rows)
    else:
        entries = conn.execute(
            "SELECT path, ext FROM archive_entries WHERE file_id = ? ORDER BY id LIMIT 2000", (rows[0]["id"],)
        ).fetchall() if rows else []
        inner = [e["path"] for e in entries]
        exts = Counter(e["ext"] or "(无)" for e in entries) or Counter(r["ext"] for r in rows)
    engines = {f"{r['engine']} {r['engine_version'] or ''}".strip() for r in rows if r["engine"]}
    if any(r["ext"] in ("uasset", "umap") for r in rows):
        engines.add("unreal")
    ids = [r["id"] for r in rows][:5000]
    captions = []
    if ids:
        captions = [
            c[0] for c in conn.execute(
                f"SELECT text FROM captions WHERE text IS NOT NULL AND file_id IN ({','.join('?' * len(ids))}) LIMIT 3", ids
            )
        ]
    lines = [
        f"路径：{path}",
        f"类型：{_kind_text(is_dir, len(rows))}，共 {_readable_size(sum(r['size'] for r in rows))}",
        "文件类型：" + "、".join(f"{ext} {n}" for ext, n in exts.most_common(8)),
    ]
    if engines:
        lines.append("引擎：" + "、".join(sorted(engines)))
    if inner:
        lines.append("里面的文件（数字已合并为 #）：" + "；".join(_sample_names(inner)))
    for caption in captions:
        lines.append("一张图的描述：" + " ".join(caption.split()))
    return "\n".join(lines)


def _kind_text(is_dir: bool, files: int) -> str:
    if is_dir:
        return f"文件夹，{files} 个文件"
    return "单个文件" if files == 1 else f"单个文件，带 {files - 1} 个同名的预览图或分卷"


def _jpeg(image: Image.Image) -> bytes:
    if image.mode == "RGBA":
        flat = Image.new("RGB", image.size, BACKGROUND)
        flat.paste(image, mask=image.getchannel("A"))
        image = flat
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def picture(conn: sqlite3.Connection, settings: Settings, root: str, path: str, is_dir: bool) -> bytes | None:
    """One picture of the unit, or None if it has none we can show."""
    rows = _unit_rows(conn, root, path, is_dir)
    if not is_dir:
        return _file_picture(settings, rows)[0]
    by_id = {r["id"]: r for r in rows}
    visible = [r for r in rows if r["ext"] in THUMBNAIL_EXTS and (r["seq_frames"] is None or r["seq_frames"] > 0)]
    picks = choose_candidates(((r["id"], r["rel_path"], r["size"]) for r in visible), SHEET_CELLS + 3)
    base = settings.roots[root].resolve()
    images = []
    for file_id, rel_path in picks:
        row = by_id[file_id]
        cached = file_thumb_path(settings.thumb_dir, file_id, row["mtime"])
        try:
            images.append(open_image(cached if cached.exists() else base / rel_path, CELL_BOX))
        except Exception:  # Pillow raises many types for malformed images
            continue
        if len(images) >= SHEET_CELLS:
            break
    if not images:  # no loose pictures: an archive's sheet inside the folder will do
        for r in rows:
            sheet = sheet_path(settings.thumb_dir, r["id"], r["mtime"])
            if r["ext"] in LISTABLE_ARCHIVE_EXTS and r["preview_state"] == 1 and sheet.exists():
                try:
                    return _jpeg(open_image(sheet, PICTURE_BOX))
                except Exception:
                    return None
        return None
    return _jpeg(sheet_image(images))


def _file_picture(settings: Settings, rows: list[sqlite3.Row]) -> tuple[bytes | None, sqlite3.Row | None]:
    """A file unit's picture and the card it comes from: the file's own (an archive's
    sheet, an image), else a preview next to it (an archive of .uasset files has no sheet)."""
    for row in rows:
        if not _has_image(row):
            continue
        try:
            return card_jpeg(settings, row, PICTURE_BOX), row
        except Exception:  # unreadable or undecodable: try the next, else text alone
            continue
    return None, None


def build_prompt(facts: str, with_caption: bool = False) -> str:
    menu = "\n".join(f"{path}  {desc}" for path, desc in CATEGORIES)
    fields = '{"category": "分类路径", "name": "名字", "reason": "一句话理由"}'
    caption = ""
    if with_caption:  # the same answer then also fills the card's 看图描述
        fields = fields[:-1] + ', "caption": "画面描述", "keywords": "关键词"}'
        caption = (
            "画面描述：用一句中文描述附图，包括是什么东西、角色或场景，画风，主要颜色。"
            "关键词：5 到 10 个，用顿号分隔。\n\n"
        )
    return (
        "你在整理一个游戏美术素材库。下面是其中一个素材包的信息，附图是包里几张图拼成的预览（可能没有）。\n"
        "请判断它该放进哪个分类，并起一个看名字就知道里面是什么的名字。\n\n"
        f"分类只能从下面选一个，填左边的路径；实在看不出来填 {UNSORTED}：\n{menu}\n\n"
        "名字：30 个字以内，中英文都可以，格式「来源 - 内容」。来源是作者、网站或原名里的包名，"
        "看不出来就只写内容。原名里有意义的英文包名要保留。\n\n"
        f"{caption}只输出一个 JSON，不要别的文字：{fields}\n\n"
        f"素材包信息：\n{facts}"
    )


def parse_answer(text: str) -> Proposal:
    """The model's JSON answer, validated. ValueError if there is none."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("回答里没有 JSON")
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        raise ValueError("回答里的 JSON 不完整") from None
    if not isinstance(data, dict):
        raise ValueError("回答不是 JSON 对象")
    category = data.get("category")
    category = category.strip().strip("/") if isinstance(category, str) else ""
    name = data.get("name") if isinstance(data.get("name"), str) else ""
    reason = data.get("reason") if isinstance(data.get("reason"), str) else ""
    return Proposal(
        category=category if category in CATEGORY_PATHS else UNSORTED,
        name=name,
        reason=" ".join(reason.split())[:200],
        caption=_caption_of(data),
    )


def _caption_of(data: dict) -> str:
    """Description and keywords as the caption job stores them: two lines."""
    text = data.get("caption")
    if not isinstance(text, str) or not clean_caption(text):
        return ""
    keywords = data.get("keywords")
    lines = [" ".join(clean_caption(text).split())]
    if isinstance(keywords, str) and clean_caption(keywords):
        lines.append(" ".join(clean_caption(keywords).split()))
    return clean_caption("\n".join(lines))


_COMPOUND_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst")


def file_suffix(name: str) -> str:
    """The extension to keep when renaming: ".tar.gz" as a whole, else the last one."""
    lowered = name.lower()
    for suffix in _COMPOUND_SUFFIXES:
        if lowered.endswith(suffix) and len(name) > len(suffix):
            return name[-len(suffix):]
    return PurePosixPath(name).suffix


def clean_name(name: str, original: str, is_dir: bool) -> str:
    """A safe folder or file name from the model's suggestion. Keeps the original
    extension (once), and the original name when that was only a number (to trace it back)."""
    suffix = "" if is_dir else file_suffix(original)
    stem = original[: len(original) - len(suffix)] if suffix else original
    cleaned = " ".join(_BAD_NAME_CHARS.sub(" ", name).split()).strip(" .")
    if suffix and cleaned.lower().endswith(suffix.lower()):
        cleaned = cleaned[: len(cleaned) - len(suffix)]
    cleaned = cleaned[:MAX_NAME_CHARS].strip(" .")
    if not cleaned or cleaned in (".", ".."):
        cleaned = stem
    elif _NUMBERED.match(stem) and stem not in cleaned:
        cleaned = f"{cleaned} ({stem})"
    return cleaned + suffix


def target_for(category: str, name: str, is_dir: bool, taken: set[str]) -> str:
    """category/name, numbered ("name (2).zip") if that path is already used or proposed.
    `taken` holds lower-cased paths: NAS file systems may ignore case."""
    suffix = "" if is_dir else file_suffix(name)
    stem = name[: len(name) - len(suffix)] if suffix else name
    target, n = f"{category}/{name}", 2
    while target.lower() in taken:
        target = f"{category}/{stem} ({n}){suffix}"
        n += 1
    return target


def classify_unit(
    client: CaptionClient, conn: sqlite3.Connection, settings: Settings, root: str, path: str, is_dir: bool
) -> Proposal:
    """Ask the model about one unit. Raises EmbedError: an unusable answer counts as an
    item problem (this unit fails, the job goes on), API trouble as a stop.

    A file unit (an archive, a lone image) whose picture's card has no description yet
    gets it in the same question, saving the caption job a request."""
    if is_dir:
        jpeg, card = picture(conn, settings, root, path, is_dir), None
    else:
        jpeg, shown = _file_picture(settings, _unit_rows(conn, root, path, is_dir))
        card = _undescribed_card(conn, root, shown["rel_path"]) if shown else None
    tokens = ANSWER_TOKENS + (CAPTION_TOKENS if card else 0)
    answer = client.ask(build_prompt(digest(conn, root, path, is_dir), with_caption=card is not None), jpeg, tokens)
    try:
        proposal = parse_answer(answer)
    except ValueError as err:
        raise EmbedError(f"{err}：{answer[:100]}", ITEM_PROBLEM) from None
    if card is not None and proposal.caption:
        save_caption(conn, card, client.config.model, proposal.caption, None)
    name = clean_name(proposal.name, path.rsplit("/", 1)[-1], is_dir)
    return Proposal(proposal.category, name, proposal.reason, proposal.caption)


def _undescribed_card(conn: sqlite3.Connection, root: str, path: str) -> sqlite3.Row | None:
    """The file's card if it has no current description (same rule as the caption job)."""
    return conn.execute(
        """
        SELECT f.id, f.mtime FROM files f LEFT JOIN captions c ON c.file_id = f.id
        WHERE f.root = ? AND f.rel_path = ? AND NOT (c.text IS NOT NULL AND c.mtime = f.mtime)
        """,
        (root, path),
    ).fetchone()
