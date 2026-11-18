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
from assetindex.categories import CATEGORIES, CATEGORY_PATHS, DUPLICATES, TO_DELETE, UNSORTED  # noqa: F401 (re-exported)
from assetindex.config import Settings
from assetindex.embed_stage import BACKGROUND, JPEG_QUALITY, _has_image, card_jpeg
from assetindex.embedding import EmbedError
from assetindex.kinds import LISTABLE_ARCHIVE_EXTS, THUMBNAIL_EXTS
from assetindex.thumbs import file_thumb_path, open_image, sheet_image, sheet_path
from assetindex.units import SCHEME_FOLDERS, archive_stem, layout, pack_keys, stored_joins, stored_splits


MAX_NAME_CHARS = 60
SAMPLE_NAMES = 40
SHEET_CELLS = 9
CELL_BOX = (160, 160)
PICTURE_BOX = (512, 512)
ANSWER_TOKENS = 1500  # 300 was cut short when "auto:fast" landed on a model that reasons first
FLAT_OBJECT = re.compile(r"\{[^{}]*\}")
ANSWER_SHOWN = 100  # chars of an unusable answer kept in its error
CAPTION_TOKENS = 300  # extra room when the answer also describes the picture (Chinese: ~1.5 tokens a character)
ITEM_PROBLEM = 422  # an answer we can't use counts against this unit, like a refused picture
_BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_DIGITS = re.compile(r"\d+")
_NUMBERED = re.compile(r"^[\W\d_]+$")
# "Sources" that name no author: engines, download sites, the library's own folders.
_JUNK_SOURCE = re.compile(
    r"^\s*(ue\s*[45]?|unreal(\s*engine)?\s*[45]?|unity(3d)?|ue\s*角色|虚幻\s*[45]?(引擎)?|虚幻\s*[45]?\s*资源站|"
    r"ansucai(\.com)?|unreal\s*dojo|unsorted|素材|资源|未分类|待分类|_unsorted)\s*[-–—:：]\s*",
    re.IGNORECASE,
)  # "127327921", "(12)": says nothing about the content


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
    preview here or in a preview folder below). The same rules as the whole root's
    layout, run on the folder's subtree: everything that decides how a file there is
    grouped (its folder's rules, splits and joins) lives in that subtree."""
    folder = path.rpartition("/")[0]
    low, high = under(folder) if folder else ("", "\U0010ffff")
    paths = [r[0] for r in conn.execute(
        "SELECT rel_path FROM files WHERE root = ? AND rel_path >= ? AND rel_path < ?", (root, low, high)
    )]
    shape = layout(paths, stored_splits(conn, root), stored_joins(conn, root), SCHEME_FOLDERS)
    return [path, *sorted(p for p in paths if p != path and shape.unit_of(p) == (path, False))]


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
        "名字：30 个字以内，中英文都可以。原名里有意义的英文包名要保留，后面加一段中文说明里面是什么，"
        "比如「Modern Assassin - 现代女刺客角色」。"
        "只有从包里的文件名或原名能确认作者或商店（比如 Brushify、Synty）时，才写成「作者 - 内容」；"
        "不要写引擎名（UE、Unity）、下载网站名或文件夹名。\n\n"
        f"{caption}只输出一个 JSON，不要别的文字：{fields}\n\n"
        f"素材包信息：\n{facts}\n\n"
        "再说一次：直接回答，只输出那一个 JSON。"
    )


def last_json_object(text: str) -> object:
    """The last flat {...} in the answer that parses: a model that thinks aloud first may
    quote the format on its way. ValueError if there is none."""
    found = FLAT_OBJECT.findall(text)
    if not found:
        raise ValueError("回答里没有 JSON")
    for candidate in reversed(found):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    raise ValueError("回答里的 JSON 不完整")


def parse_answer(text: str) -> Proposal:
    """The model's JSON answer, validated. ValueError if there is none."""
    data = last_json_object(text)
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


def strip_junk_source(name: str) -> str:
    """Drop a "source - " that names no author: an engine, a download site, a folder
    ("UE - Military Drone" -> "Military Drone"). The model writes them when it does not know."""
    stripped = name
    while True:
        shorter = _JUNK_SOURCE.sub("", stripped, count=1)
        if shorter == stripped or not shorter.strip():
            return stripped
        stripped = shorter


def clean_name(name: str, original: str, is_dir: bool) -> str:
    """A safe folder or file name from the model's suggestion. Keeps the original
    extension (once), and the original name when that was only a number (to trace it back)."""
    suffix = "" if is_dir else file_suffix(original)
    stem = original[: len(original) - len(suffix)] if suffix else original
    cleaned = " ".join(_BAD_NAME_CHARS.sub(" ", strip_junk_source(name)).split()).strip(" .")
    if suffix and cleaned.lower().endswith(suffix.lower()):
        cleaned = cleaned[: len(cleaned) - len(suffix)]
    cleaned = cleaned[:MAX_NAME_CHARS].strip(" .")
    if not cleaned or cleaned in (".", ".."):
        cleaned = stem
    elif _NUMBERED.match(stem) and stem not in cleaned:
        cleaned = f"{cleaned} ({stem})"
    return cleaned + suffix


def _file_keys(name: str) -> frozenset[str]:
    """The keys a file is matched to previews by ("Pack.part1.rar" -> "pack"), none for a
    name with no extension: a folder, whose name previews are never matched to."""
    stem = archive_stem(name)
    if stem is None:
        suffix = file_suffix(name)
        if not suffix:
            return frozenset()
        stem = name[: len(name) - len(suffix)]
    return pack_keys(stem)


def name_clashes(category: str, name: str, is_dir: bool, taken: set[str]) -> bool:
    """The path is used or proposed, or (a file pack) another pack there has a name its
    previews would be matched to as well: "X.zip" next to "X.rar", "X - 3D.7z" or "X (2).zip"."""
    if f"{category}/{name}".lower() in taken:
        return True
    if is_dir:
        return False
    folder, keys = category.lower(), _file_keys(name)
    return bool(keys) and any(t.rpartition("/")[0] == folder and keys & _file_keys(t.rpartition("/")[2]) for t in taken)


def target_for(category: str, name: str, is_dir: bool, taken: set[str]) -> str:
    """category/name, numbered if it clashes (see name_clashes): "Icons (2)" for a folder,
    "pack 2.zip" for a file ("pack (2).png" would preview "pack.zip"). `taken` holds
    lower-cased paths: NAS file systems may ignore case."""
    suffix = "" if is_dir else file_suffix(name)
    stem = name[: len(name) - len(suffix)] if suffix else name
    numbered, n = name, 2
    while name_clashes(category, numbered, is_dir, taken):
        numbered = f"{stem} ({n})" if is_dir else f"{stem} {n}{suffix}"
        n += 1
    return f"{category}/{numbered}"


@dataclass(frozen=True)
class Question:
    """Everything the model needs for one unit, made from the database and the cached
    pictures (no model call), so the next one can be made while the model works."""

    prompt: str
    jpeg: bytes | None
    tokens: int
    card: sqlite3.Row | None  # the picture's card, to describe in the same answer
    original: str  # the unit's current name
    is_dir: bool


def prepare_question(conn: sqlite3.Connection, settings: Settings, root: str, path: str, is_dir: bool) -> Question:
    """A file unit (an archive, a lone image) whose picture's card has no description yet
    gets it in the same question, saving the caption job a request."""
    if is_dir:
        jpeg, card = picture(conn, settings, root, path, is_dir), None
    else:
        jpeg, shown = _file_picture(settings, _unit_rows(conn, root, path, is_dir))
        card = _undescribed_card(conn, root, shown["rel_path"]) if shown else None
    return Question(
        prompt=build_prompt(digest(conn, root, path, is_dir), with_caption=card is not None),
        jpeg=jpeg,
        tokens=ANSWER_TOKENS + (CAPTION_TOKENS if card else 0),
        card=card,
        original=path.rsplit("/", 1)[-1],
        is_dir=is_dir,
    )


def answer_shown(answer: str) -> str:
    """An unusable answer as its error shows it: the end, where a model that thinks
    aloud either gave up or was cut off."""
    return answer if len(answer) <= ANSWER_SHOWN else "…" + answer[-ANSWER_SHOWN:]


def read_answer(conn: sqlite3.Connection, model: str, question: Question, answer: str) -> Proposal:
    """The proposal in the model's answer. Raises EmbedError (an item problem) if unusable."""
    try:
        proposal = parse_answer(answer)
    except ValueError as err:
        raise EmbedError(f"{err}：{answer_shown(answer)}", ITEM_PROBLEM) from None
    if question.card is not None and proposal.caption:
        save_caption(conn, question.card, model, proposal.caption, None)
    name = clean_name(proposal.name, question.original, question.is_dir)
    return Proposal(proposal.category, name, proposal.reason, proposal.caption)


def classify_unit(
    client: CaptionClient, conn: sqlite3.Connection, settings: Settings, root: str, path: str, is_dir: bool
) -> Proposal:
    """Ask the model about one unit. Raises EmbedError: an unusable answer counts as an
    item problem (this unit fails, the job goes on), API trouble as a stop."""
    question = prepare_question(conn, settings, root, path, is_dir)
    return read_answer(conn, client.config.model, question, client.ask(question.prompt, question.jpeg, question.tokens))


def _undescribed_card(conn: sqlite3.Connection, root: str, path: str) -> sqlite3.Row | None:
    """The file's card if it has no current description (same rule as the caption job)."""
    return conn.execute(
        """
        SELECT f.id, f.mtime FROM files f LEFT JOIN captions c ON c.file_id = f.id
        WHERE f.root = ? AND f.rel_path = ? AND NOT (c.text IS NOT NULL AND c.mtime = f.mtime)
        """,
        (root, path),
    ).fetchone()
