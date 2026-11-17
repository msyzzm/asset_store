"""Describe pictures with a vision chat model through an OpenAI-compatible
/chat/completions API (e.g. a local Qwen served by Strata).

A picture's description makes it findable by what it shows, not only by its file name:
it goes into keyword search and into the card's text for semantic search. Settings are
edited in the web UI and stored in the data dir; the API key is write-only, as for the
embeddings API.
"""

import base64
import io
import json
import re
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from PIL import Image, ImageDraw

from assetindex.embedding import EmbedError, check_url, next_key, post_json, save_private_json

CONFIG_NAME = "caption.json"
MAX_MODEL_LENGTH = 200
MAX_CAPTION_CHARS = 400
MAX_ANSWER_TOKENS = 300
TIMEOUT_SECONDS = 180  # one picture; a busy local model can queue us for a while
PROBE_TIMEOUT_SECONDS = 120
EMPTY_ANSWER_STATUS = 422  # treated like a rejected input: that picture fails, the job goes on
PROMPT = (
    "这是一张游戏美术素材的预览图（如果是多张小图拼在一起，说明是一个素材包里的几个文件）。"
    "用中文回答，只输出两行：第一行用一句话描述画面，包括是什么东西、角色或场景，画风，主要颜色；"
    "第二行写 5 到 10 个关键词，用顿号分隔。"
)
_THINKING = re.compile(r"<think>.*?</think>", re.DOTALL)
_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")


# Job limits, each (lowest, highest) and how the page names it in an error.
LIMITS = {
    "workers": (1, 8, "并行数量"),
    "retries": (0, 20, "接口出错重试次数"),
    "retry_wait": (0, 600, "重试前等待秒数"),
    "max_failures": (1, 10000, "连续失败上限"),
}


@dataclass(frozen=True)
class CaptionConfig:
    base_url: str = ""  # e.g. http://192.168.1.10:8080/v1; "/chat/completions" is appended
    model: str = ""  # Strata ignores it, OpenAI-style servers need it
    api_key: str = ""
    workers: int = 3  # questions open at once in the organize analysis
    retries: int = 3  # API errors on one pack before it counts as failed (it may be what chokes the model)
    retry_wait: int = 10  # seconds without new questions after an API error (model restarting...)
    max_failures: int = 100  # failures in a row (bad answers or API errors) before a job stops

    @property
    def active(self) -> bool:
        return bool(self.base_url) and bool(self.model)


def apply_update(current: CaptionConfig, update: dict) -> CaptionConfig:
    """New settings from a partial update, validated like the embeddings settings."""
    unknown = set(update) - {f.name for f in fields(CaptionConfig)}
    if unknown:
        raise ValueError(f"未知设置项：{', '.join(sorted(unknown))}")
    if not all(isinstance(v, str) for k, v in update.items() if k not in LIMITS):
        raise ValueError("设置项的类型不对")
    base_url = check_url(update.get("base_url", current.base_url))
    model = update.get("model", current.model).strip()
    if len(model) > MAX_MODEL_LENGTH:
        raise ValueError("模型名太长")
    api_key = next_key(update, current.api_key, base_url == current.base_url)
    limits = {name: _limit(name, update.get(name, getattr(current, name))) for name in LIMITS}
    return CaptionConfig(base_url=base_url, model=model, api_key=api_key, **limits)


def _valid_limit(name: str, value: object) -> bool:
    try:
        _limit(name, value)
    except ValueError:
        return False
    return True


def _limit(name: str, value: object) -> int:
    low, high, label = LIMITS[name]
    if type(value) is not int or not low <= value <= high:  # bool is an int subclass: refused too
        raise ValueError(f"「{label}」要是 {low} 到 {high} 之间的整数")
    return value


def load_config(data_dir: Path) -> CaptionConfig:
    try:
        raw = json.loads((data_dir / CONFIG_NAME).read_text(encoding="utf-8"))
        raw = raw if isinstance(raw, dict) else {}
        # A bad limit (hand-edited...) falls back alone: the address and key must survive it.
        return apply_update(CaptionConfig(), {k: v for k, v in raw.items() if k not in LIMITS or _valid_limit(k, v)})
    except (OSError, ValueError):
        return CaptionConfig()


def save_config(data_dir: Path, config: CaptionConfig) -> None:
    save_private_json(data_dir, CONFIG_NAME, asdict(config))


def public_view(config: CaptionConfig) -> dict:
    view = asdict(config)
    del view["api_key"]
    return {**view, "has_key": bool(config.api_key), "active": config.active}


def clean_caption(text: str) -> str:
    """The model's answer as stored: no reasoning, no control characters, no blank
    lines, bounded. It is shown in the page and searched, never trusted further."""
    text = _THINKING.sub("", text)
    if "<think>" in text:  # cut off while still thinking: no answer yet
        text = text.split("<think>", 1)[0]
    text = _CONTROL.sub("", text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)[:MAX_CAPTION_CHARS]


def _answer_text(parsed) -> str:
    try:
        content = parsed["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise EmbedError("返回内容不是 OpenAI chat 格式") from None
    if isinstance(content, list):  # content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
    return content if isinstance(content, str) else ""


class CaptionClient:
    def __init__(self, config: CaptionConfig, timeout: float = TIMEOUT_SECONDS):
        self.config = config
        self.timeout = timeout

    def ask(self, prompt: str, jpeg: bytes | None, max_tokens: int = MAX_ANSWER_TOKENS) -> str:
        """One question, with a picture if given; the answer without any reasoning text."""
        content = [{"type": "text", "text": prompt}]
        if jpeg is not None:
            url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": url}})
        body = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "stream": False,
            # Thinking first would take far longer per picture (and could use up max_tokens).
            "reasoning_effort": "none",
            "chat_template_kwargs": {"enable_thinking": False},
        }
        parsed = post_json(self.config.base_url + "/chat/completions", self.config.api_key, body, self.timeout)
        answer = _THINKING.sub("", _answer_text(parsed))
        return answer.split("<think>", 1)[0].strip()  # cut off while still thinking: nothing yet

    def describe(self, jpeg: bytes) -> str:
        caption = clean_caption(self.ask(PROMPT, jpeg))
        if not caption:
            raise EmbedError("模型没有返回描述", EMPTY_ANSWER_STATUS)
        return caption


def _probe_jpeg() -> bytes:
    """A picture with an obvious answer: a red ball on white."""
    image = Image.new("RGB", (256, 256), (255, 255, 255))
    ImageDraw.Draw(image).ellipse((48, 48, 208, 208), fill=(220, 30, 30))
    buf = io.BytesIO()
    image.save(buf, "JPEG")
    return buf.getvalue()


def probe(config: CaptionConfig) -> dict:
    """Describe a test picture; report the answer and how long it took. Never raises."""
    started = time.monotonic()
    try:
        text = CaptionClient(config, timeout=PROBE_TIMEOUT_SECONDS).describe(_probe_jpeg())
    except EmbedError as err:
        return {"ok": False, "error": str(err)}
    return {"ok": True, "text": text, "seconds": round(time.monotonic() - started, 1)}
