import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "skills" / "asset-store" / "scripts" / "asset_store.py"
spec = importlib.util.spec_from_file_location("asset_store_skill", SCRIPT)
skill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skill)


class _Resp:
    def __init__(self, disposition):
        self.headers = {"Content-Disposition": disposition}


def test_saved_file_name_comes_from_the_header_but_never_a_path():
    assert skill._filename(_Resp("attachment; filename*=UTF-8''%E5%89%91.png"), "x") == "剑.png"
    assert skill._filename(_Resp('attachment; filename="a.fbx"'), "x") == "a.fbx"
    assert skill._filename(_Resp("attachment; filename=\"../../etc/passwd\""), "x") == "passwd"
    assert skill._filename(_Resp(""), "fallback.bin") == "fallback.bin"


def test_output_drops_nulls_but_keeps_empty_lists():
    assert skill._prune({"a": None, "b": [], "c": [{"d": None, "e": 1}]}) == {"b": [], "c": [{"e": 1}]}


def test_unreachable_server_is_a_clear_error(capsys):
    assert skill.main(["--url", "http://127.0.0.1:1", "tasks"]) == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert "cannot reach" in error and "ASSET_STORE_URL" in error


def test_lowercase_utf8_header_from_starlette_is_understood():
    assert skill._filename(_Resp("attachment; filename*=utf-8''%E5%AE%9D%E7%AE%B1.png"), "x") == "宝箱.png"


def test_existing_files_are_never_overwritten(tmp_path):
    (tmp_path / "icon.png").write_bytes(b"old")
    assert skill._free_name(tmp_path, "icon.png").name == "icon (1).png"
    (tmp_path / "icon (1).png").write_bytes(b"old")
    assert skill._free_name(tmp_path, "icon.png").name == "icon (2).png"
    assert skill._free_name(tmp_path, "new.png").name == "new.png"


def _calls(monkeypatch, answer):
    calls = []

    def fake_json(args, path, params=None, body=None):
        calls.append((path, params, body))
        return answer

    monkeypatch.setattr(skill, "_json", fake_json)
    return calls


def test_external_search_asks_every_site_and_keeps_a_few_items_each(monkeypatch, capsys):
    answer = {"query": "crate", "chinese": False, "sources": {
        "polyhaven": {"label": "Poly Haven", "license": "CC0", "error": None, "total": 13,
                      "items": [{"id": str(n)} for n in range(13)]},
        "itch": {"label": "itch.io", "license": "", "error": "超时", "total": 0, "items": []},
    }}
    calls = _calls(monkeypatch, answer)
    assert skill.main(["external", "wooden", "crate", "--limit", "3"]) == 0
    assert calls == [("/api/external/search", {"q": "wooden crate", "source": "", "offset": 0}, None)]
    out = json.loads(capsys.readouterr().out)
    assert [i["id"] for i in out["sources"]["polyhaven"]["items"]] == ["0", "1", "2"]
    assert out["sources"]["polyhaven"]["total"] == 13
    assert out["sources"]["itch"]["error"] == "超时"


def test_external_search_of_one_site_pages_with_offset(monkeypatch):
    calls = _calls(monkeypatch, {"sources": {}})
    assert skill.main(["external", "rock", "--source", "ambientcg", "--offset", "24"]) == 0
    assert calls[0][1] == {"q": "rock", "source": "ambientcg", "offset": 24}


def test_external_show_and_get_name_the_site_asset_and_option(monkeypatch):
    calls = _calls(monkeypatch, {})
    assert skill.main(["external-show", "polyhaven", "treasure_chest"]) == 0
    assert skill.main(["external-get", "polyhaven", "treasure_chest", "--option", "fbx/1k"]) == 0
    assert skill.main(["external-get", "freesound", "640170", "--option", "original", "--root", "nas"]) == 0
    assert calls == [
        ("/api/external/polyhaven/treasure_chest", None, None),
        ("/api/external/polyhaven/treasure_chest/download", None, {"option": "fbx/1k"}),
        ("/api/external/freesound/640170/download", None, {"option": "original", "root": "nas"}),
    ]


def test_external_ids_cannot_reach_other_routes(monkeypatch):
    calls = _calls(monkeypatch, {})
    assert skill.main(["external-show", "polyhaven", "../../scan"]) == 0
    assert calls[0][0] == "/api/external/polyhaven/..%2F..%2Fscan"


def test_downloads_can_be_cancelled_and_retried_and_keys_looked_at(monkeypatch):
    calls = _calls(monkeypatch, {})
    assert skill.main(["download-cancel", "7"]) == 0
    assert skill.main(["download-retry", "7"]) == 0
    assert skill.main(["external-keys"]) == 0
    assert calls == [
        ("/api/downloads/7/cancel", None, {}),
        ("/api/downloads/7/retry", None, {}),
        ("/api/external/keys", None, None),
    ]


def test_every_site_the_server_searches_is_in_the_skill():
    from assetindex.external_web import SOURCES

    text = (SCRIPT.parent.parent / "SKILL.md").read_text(encoding="utf-8")
    assert [name for name in SOURCES if f"`{name}`" not in text] == []
