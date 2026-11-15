import io
import json
import os
import sys

import pytest
from PIL import Image

from assetindex import embedding
from assetindex.embedding import EmbedClient, EmbedConfig, EmbedError, apply_update
from tests.fake_embed import FakeEmbedServer, text_vector

# --- settings ----------------------------------------------------------------------


def test_missing_or_corrupt_settings_mean_semantic_search_is_off(tmp_path):
    assert embedding.load_config(tmp_path) == EmbedConfig()
    assert not EmbedConfig().active

    (tmp_path / embedding.CONFIG_NAME).write_text("{oops", encoding="utf-8")
    assert embedding.load_config(tmp_path) == EmbedConfig()


def test_settings_round_trip_and_stay_private(tmp_path):
    config = EmbedConfig(enabled=True, base_url="http://10.0.0.5:8000/v1", model="m", api_key="sk-1")

    embedding.save_config(tmp_path, config)

    assert embedding.load_config(tmp_path) == config
    if sys.platform != "win32":
        assert (tmp_path / embedding.CONFIG_NAME).stat().st_mode & 0o077 == 0


def test_public_view_never_contains_the_key():
    view = embedding.public_view(EmbedConfig(enabled=True, base_url="http://h/v1", model="m", api_key="sk-secret"))

    assert view["has_key"] is True
    assert "sk-secret" not in json.dumps(view)


def test_update_keeps_key_when_omitted_and_clears_it_when_empty():
    current = EmbedConfig(enabled=True, base_url="http://h/v1", model="m", api_key="sk-1")

    assert apply_update(current, {"model": "m2"}).api_key == "sk-1"
    assert apply_update(current, {"api_key": ""}).api_key == ""
    assert apply_update(current, {"api_key": "sk-2"}).api_key == "sk-2"


def test_changing_the_address_drops_the_old_key():
    # Otherwise anyone on the LAN could point the scanner at their own host and read the key.
    current = EmbedConfig(enabled=True, base_url="http://h/v1", model="m", api_key="sk-1")

    assert apply_update(current, {"base_url": "http://evil/v1"}).api_key == ""
    assert apply_update(current, {"base_url": "http://evil/v1", "api_key": "sk-2"}).api_key == "sk-2"
    assert apply_update(current, {"base_url": "http://h/v1/"}).api_key == "sk-1"  # same address


@pytest.mark.parametrize(
    "update",
    [
        {"base_url": "ftp://h/v1"},
        {"base_url": "not a url"},
        {"base_url": "http://user:pw@h/v1"},
        {"image_format": "png"},
        {"batch_size": 0},
        {"batch_size": 10_000},
        {"model": "x" * 300},
        {"enabled": "yes"},
        {"unknown": 1},
        {"base_url": "http://h/v1?x="},  # would swallow the "/embeddings" suffix
        {"base_url": "http://h/v1#frag"},
        {"base_url": "http://h:99999/v1"},
        {"base_url": "http://h/v1\x00"},
        {"base_url": "http://169.254.169.254/v1"},  # cloud metadata / link-local
        {"base_url": "http://0.0.0.0/v1"},
        {"base_url": "http://[fe80::1]/v1"},
        {"api_key": "sk-1\r\nX-Evil: 1"},
        {"api_key": "密钥"},
    ],
)
def test_bad_settings_are_rejected(update):
    with pytest.raises(ValueError):
        apply_update(EmbedConfig(), update)


def test_enabling_needs_an_address_and_a_model():
    with pytest.raises(ValueError):
        apply_update(EmbedConfig(), {"enabled": True})
    config = apply_update(EmbedConfig(), {"enabled": True, "base_url": "http://h:9/v1", "model": "m"})
    assert config.active and config.base_url == "http://h:9/v1"


# --- client ------------------------------------------------------------------------


@pytest.fixture
def server():
    with FakeEmbedServer() as fake:
        yield fake


def _client(server, **kw) -> EmbedClient:
    config = EmbedConfig(enabled=True, base_url=server.base_url, model="test-model", api_key="sk-1", **kw)
    return EmbedClient(config, timeout=5)


def test_texts_are_sent_openai_style_and_returned_in_order(server):
    vectors = _client(server).embed_texts(["sword", "music"])

    assert vectors == [text_vector("sword"), text_vector("music")]
    request = server.requests[0]
    assert request["path"] == "/v1/embeddings"
    assert request["headers"]["Authorization"] == "Bearer sk-1"
    assert request["body"] == {"model": "test-model", "input": ["sword", "music"], "encoding_format": "float"}


def test_no_key_means_no_authorization_header(server):
    config = EmbedConfig(enabled=True, base_url=server.base_url, model="m")
    EmbedClient(config, timeout=5).embed_texts(["a"])
    assert "Authorization" not in server.requests[0]["headers"]


def test_texts_are_batched(server):
    _client(server, batch_size=2).embed_texts(["a", "b", "c", "d", "e"])
    assert [len(r["body"]["input"]) for r in server.requests] == [2, 2, 1]


def _jpeg(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.mark.parametrize("image_format", ["input_object", "input_string", "messages"])
def test_each_image_format_reaches_the_server(server, image_format):
    vectors = _client(server, image_format=image_format).embed_images([_jpeg((255, 0, 0)), _jpeg((0, 0, 255))])

    assert vectors[0][1] > 0.9 and vectors[1][1] < 0.1  # red, then blue
    body = server.requests[0]["body"]
    if image_format == "input_object":
        assert body["input"][0]["image"].startswith("data:image/jpeg;base64,")
    elif image_format == "input_string":
        assert body["input"][0].startswith("data:image/jpeg;base64,")
    else:  # vLLM-style chat messages: one image per request
        assert len(server.requests) == 2
        assert body["messages"][0]["content"][0]["type"] == "image_url"


def test_images_refused_when_format_is_none(server):
    with pytest.raises(EmbedError):
        _client(server).embed_images([_jpeg((255, 0, 0))])


def test_http_errors_carry_the_status(server):
    server.fail_status = 401
    with pytest.raises(EmbedError) as err:
        _client(server).embed_texts(["a"])
    assert err.value.status == 401 and not err.value.item_problem
    assert "fake failure" in str(err.value)

    server.fail_status = 400
    with pytest.raises(EmbedError) as err:
        _client(server).embed_texts(["a"])
    assert err.value.item_problem


def test_unreachable_server_is_an_embed_error():
    config = EmbedConfig(enabled=True, base_url="http://127.0.0.1:9/v1", model="m")
    with pytest.raises(EmbedError) as err:
        EmbedClient(config, timeout=2).embed_texts(["a"])
    assert err.value.status is None


def test_malformed_responses_are_rejected(server, monkeypatch):
    for payload in ({"data": []}, {"nope": 1}, {"data": [{"index": 0, "embedding": ["x"]}]}):
        monkeypatch.setattr(server, "respond", lambda path, body, p=payload: (200, p))
        with pytest.raises(EmbedError):
            _client(server).embed_texts(["a"])


def test_config_file_is_written_atomically(tmp_path):
    embedding.save_config(tmp_path, EmbedConfig(model="m"))
    assert [p.name for p in tmp_path.iterdir()] == [embedding.CONFIG_NAME]
    assert not os.path.exists(tmp_path / (embedding.CONFIG_NAME + ".tmp"))


# --- connection test ---------------------------------------------------------------


def test_probe_reports_dimension_for_text_and_images(server):
    result = embedding.probe(_client(server, image_format="input_object").config)
    assert result["text"] == {"ok": True, "dim": 6}
    assert result["image"] == {"ok": True, "dim": 6}


def test_probe_skips_images_when_not_configured(server):
    assert embedding.probe(_client(server).config)["image"] is None


def test_probe_reports_errors_without_raising(server):
    server.reject_images = True
    result = embedding.probe(_client(server, image_format="input_object").config)
    assert result["text"]["ok"] is True
    assert result["image"]["ok"] is False and "images not supported" in result["image"]["error"]



# --- hostile or broken servers ---------------------------------------------------


def test_oversized_responses_are_refused(server, monkeypatch):
    monkeypatch.setattr(embedding, "MAX_RESPONSE_BYTES", 1000)
    monkeypatch.setattr(server, "respond", lambda path, body: (200, {"data": [{"index": 0, "embedding": [0.5] * 1000}]}))
    with pytest.raises(EmbedError, match="太大"):
        _client(server).embed_texts(["a"])


def test_huge_vectors_are_refused(server, monkeypatch):
    big = [0.1] * (embedding.MAX_DIMENSIONS + 1)
    monkeypatch.setattr(server, "respond", lambda path, body: (200, {"data": [{"index": 0, "embedding": big}]}))
    with pytest.raises(EmbedError):
        _client(server).embed_texts(["a"])


def test_only_openai_style_error_messages_are_passed_on(server, monkeypatch):
    # A non-API service's error page (the address could be anything) is not echoed back.
    monkeypatch.setattr(server, "respond", lambda path, body: (404, {"secret": "internal admin page"}))
    with pytest.raises(EmbedError) as err:
        _client(server).embed_texts(["a"])
    assert "internal admin" not in str(err.value) and "404" in str(err.value)


def test_protocol_errors_become_embed_errors_without_the_key(server, monkeypatch):
    import http.client

    def broken(*a, **kw):
        raise http.client.IncompleteRead(b"partial")

    monkeypatch.setattr(embedding._opener, "open", broken)
    with pytest.raises(EmbedError) as err:
        _client(server).embed_texts(["a"])
    assert "sk-1" not in str(err.value)
