import io
import json

import pytest
from PIL import Image

from assetindex.caption import (
    MAX_CAPTION_CHARS,
    CaptionClient,
    CaptionConfig,
    apply_update,
    clean_caption,
    load_config,
    probe,
    public_view,
    save_config,
)
from assetindex.embedding import EmbedError
from tests.fake_embed import FakeEmbedServer


@pytest.fixture
def server():
    with FakeEmbedServer() as fake:
        yield fake


def _jpeg(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buf, "JPEG")
    return buf.getvalue()


def test_settings_validate_and_keep_the_key_private(tmp_path):
    config = apply_update(CaptionConfig(), {"base_url": "http://10.0.0.5:8080/v1/", "model": "qwen", "api_key": "k1"})
    assert config.base_url == "http://10.0.0.5:8080/v1" and config.active

    save_config(tmp_path, config)
    assert load_config(tmp_path) == config
    view = public_view(config)
    assert "api_key" not in view and view["has_key"] is True

    moved = apply_update(config, {"base_url": "http://10.0.0.6:8080/v1"})
    assert moved.api_key == ""  # a new address never inherits the stored key
    assert apply_update(config, {"model": "other"}).api_key == "k1"


def test_job_limits_are_settings_with_defaults(tmp_path):
    config = apply_update(CaptionConfig(), {"workers": 4, "retries": 0, "retry_wait": 30, "max_failures": 50})
    assert (config.workers, config.retries, config.retry_wait, config.max_failures) == (4, 0, 30, 50)
    save_config(tmp_path, config)
    assert load_config(tmp_path) == config
    assert public_view(config)["workers"] == 4
    plain = CaptionConfig()
    assert (plain.workers, plain.retries, plain.retry_wait, plain.max_failures) == (3, 3, 10, 100)


@pytest.mark.parametrize(
    "update",
    [{"workers": 0}, {"workers": 9}, {"workers": "3"}, {"workers": True}, {"retries": -1},
     {"retry_wait": 601}, {"max_failures": 0}, {"max_failures": 1.5}],
)
def test_job_limits_out_of_range_are_refused(update):
    with pytest.raises(ValueError):
        apply_update(CaptionConfig(), update)


@pytest.mark.parametrize(
    "update",
    [
        {"base_url": "ftp://x/v1"},
        {"base_url": "http://169.254.169.254/v1"},
        {"api_key": "has space"},
        {"model": 5},
        {"unknown": 1},
    ],
)
def test_bad_settings_are_refused(update):
    with pytest.raises(ValueError):
        apply_update(CaptionConfig(), update)


def test_unreadable_settings_fall_back_to_defaults(tmp_path):
    (tmp_path / "caption.json").write_text("{not json", encoding="utf-8")
    assert load_config(tmp_path) == CaptionConfig()


def test_a_bad_stored_limit_falls_back_alone(tmp_path):
    save_config(tmp_path, CaptionConfig(base_url="http://10.0.0.5:8080/v1", model="qwen", api_key="k", retries=5))
    raw = json.loads((tmp_path / "caption.json").read_text(encoding="utf-8"))
    (tmp_path / "caption.json").write_text(json.dumps({**raw, "workers": 99}), encoding="utf-8")

    config = load_config(tmp_path)

    assert config.base_url == "http://10.0.0.5:8080/v1" and config.api_key == "k"
    assert config.workers == CaptionConfig().workers and config.retries == 5


def test_clean_caption_strips_thinking_controls_and_length():
    assert clean_caption("<think>hmm\nlong</think>\n 一团火焰 \n\n火焰、红色 ") == "一团火焰\n火焰、红色"
    assert clean_caption("坏\x00字符\x1b[31m") == "坏字符[31m"
    assert clean_caption("<think>never closed") == ""
    assert len(clean_caption("长" * 5000)) == MAX_CAPTION_CHARS


def test_describe_sends_the_picture_without_thinking(server):
    client = CaptionClient(CaptionConfig(base_url=server.base_url, model="qwen", api_key="secret"))

    text = client.describe(_jpeg((255, 0, 0)))

    assert text == "一团红色的火焰特效\n火焰、红色、特效"
    request = server.requests[-1]
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer secret"
    body = request["body"]
    assert body["model"] == "qwen" and body["reasoning_effort"] == "none" and body["stream"] is False
    image = [p for p in body["messages"][0]["content"] if p["type"] == "image_url"][0]
    assert image["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_empty_answer_counts_against_that_picture_only(server):
    server.chat_reply = "<think>only thinking"
    client = CaptionClient(CaptionConfig(base_url=server.base_url, model="m"))

    with pytest.raises(EmbedError) as err:
        client.describe(_jpeg((0, 0, 255)))
    assert err.value.item_problem


def test_odd_answers_are_errors_not_crashes(server, monkeypatch):
    from assetindex import caption

    monkeypatch.setattr(caption, "post_json", lambda *a, **k: {"choices": "nope"})
    client = CaptionClient(CaptionConfig(base_url=server.base_url, model="m"))
    with pytest.raises(EmbedError):
        client.describe(_jpeg((0, 0, 255)))


def test_answer_given_as_content_parts_is_joined(server, monkeypatch):
    from assetindex import caption

    reply = {"choices": [{"message": {"content": [{"type": "text", "text": "冰"}, {"type": "text", "text": "晶"}]}}]}
    monkeypatch.setattr(caption, "post_json", lambda *a, **k: reply)
    assert CaptionClient(CaptionConfig(base_url=server.base_url, model="m")).describe(_jpeg((0, 0, 255))) == "冰晶"


def test_probe_reports_the_answer_or_the_error(server):
    config = CaptionConfig(base_url=server.base_url, model="m")
    result = probe(config)
    assert result["ok"] and "火焰" in result["text"]  # the test picture is red

    server.fail_status = 503
    failed = probe(config)
    assert not failed["ok"] and "503" in failed["error"]
