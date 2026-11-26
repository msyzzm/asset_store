"""Passwords saved on the server for locked archives (2026-11-26: DAZ products wrapped in
RARs that share one password). A lift that names no password tries no password, then
each of these in order. They live in the data folder, never in the code or the NAS, and
no page or API ever shows them back: only how many there are.
"""

import json
import logging
from pathlib import Path

from assetindex.embedding import save_private_json

log = logging.getLogger(__name__)

CONFIG_NAME = "archive_passwords.json"
MAX_COUNT = 50  # each one is a full 7-Zip run on a locked archive
MAX_LENGTH = 200


def load(data_dir: Path) -> tuple[str, ...]:
    try:
        raw = json.loads((data_dir / CONFIG_NAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except (OSError, ValueError) as err:
        log.warning("cannot read the saved archive passwords: %r", err)
        return ()
    items = raw.get("passwords") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return ()
    return tuple(p for p in items if isinstance(p, str) and p)


def save(data_dir: Path, passwords: tuple[str, ...]) -> None:
    save_private_json(data_dir, CONFIG_NAME, {"passwords": list(passwords)})


def public_view(passwords: tuple[str, ...]) -> dict:
    return {"count": len(passwords)}


def apply_update(current: tuple[str, ...], update) -> tuple[str, ...]:
    """{"add": [...]} appends the new ones; {"passwords": [...]} replaces them all ([] clears)."""
    if not isinstance(update, dict) or len(update) != 1 or not set(update) <= {"add", "passwords"}:
        raise ValueError('要 {"add": [密码…]} 或 {"passwords": [密码…]}（[] 清空）')
    (key, given), = update.items()
    if not isinstance(given, list) or not all(isinstance(p, str) and 0 < len(p) <= MAX_LENGTH for p in given):
        raise ValueError(f"密码要是 1 到 {MAX_LENGTH} 个字符的字符串")
    merged = (*current, *given) if key == "add" else tuple(given)
    result = tuple(dict.fromkeys(merged))
    if len(result) > MAX_COUNT:
        raise ValueError(f"最多存 {MAX_COUNT} 个密码")
    return result
