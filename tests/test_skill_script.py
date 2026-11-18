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


def test_organize_and_file_jobs_go_to_their_routes(monkeypatch):
    calls = _calls(monkeypatch, {})
    for argv in (
        ["organize-start", "--root", "nas", "--prefix", "UE"],
        ["units", "--root", "nas", "--show", "duplicates", "--limit", "5"],
        ["decide", "accept", "--root", "nas", "a.zip", "b"],
        ["accept-all", "--root", "nas", "--category", "01_2D/UI"],
        ["retarget", "--root", "nas", "a.zip", "_待删除/a"],
        ["split", "--root", "nas", "UE"],
        ["move", "apply", "--root", "nas"],
        ["move", "undo", "--root", "nas", "--journal", "20261118-101500.jsonl"],
        ["move", "unpack", "--root", "nas", "--password", "pw", "--limit", "3"],
        ["move", "verify", "--root", "nas", "a.zip", "b.zip"],
        ["move", "purge", "--root", "nas"],
    ):
        assert skill.main(argv) == 0
    assert calls == [
        ("/api/organize", None, {"root": "nas", "prefix": "UE", "redo": False}),
        ("/api/organize/units", {"root": "nas", "show": "duplicates", "category": "", "q": "", "limit": 5, "offset": 0}, None),
        ("/api/organize/units/decide", None, {"root": "nas", "paths": ["a.zip", "b"], "action": "accept"}),
        ("/api/organize/units/accept-all", None, {"root": "nas", "show": "proposed", "category": "01_2D/UI", "q": ""}),
        ("/api/organize/units/target", None, {"root": "nas", "path": "a.zip", "target": "_待删除/a"}),
        ("/api/organize/split", None, {"root": "nas", "path": "UE"}),
        ("/api/organize/moves", None, {"action": "apply", "root": "nas"}),
        ("/api/organize/moves", None, {"action": "undo", "root": "nas", "journal": "20261118-101500.jsonl"}),
        ("/api/organize/moves", None, {"action": "unpack", "root": "nas", "password": "pw", "limit": 3}),
        ("/api/organize/moves", None, {"action": "verify", "root": "nas", "paths": ["a.zip", "b.zip"]}),
        ("/api/organize/moves", None, {"action": "purge", "root": "nas"}),
    ]


def test_settings_are_changed_with_put_and_a_json_object(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(skill, "_json", lambda args, path, params=None, body=None, method=None: calls.append(
        (path, body, method)) or {})
    assert skill.main(["settings-set", "caption", '{"model": "qwen"}']) == 0
    assert skill.main(["update-run", "check"]) == 0
    assert calls == [("/api/settings/caption", {"model": "qwen"}, "PUT"), ("/api/update", {"action": "check"}, None)]
    assert skill.main(["settings-set", "embedding", "[1]"]) == 1
    assert "JSON object" in capsys.readouterr().err


def test_every_server_route_is_in_the_skill(tmp_path):
    """An agent can do whatever the web page can: a new route the skill misses fails here."""
    from assetindex.config import Settings
    from assetindex.web import create_app

    text = (SCRIPT.parent.parent / "SKILL.md").read_text(encoding="utf-8") + SCRIPT.read_text(encoding="utf-8")
    app = create_app(Settings(roots={}, data_dir=tmp_path))
    skipped = (
        "/api/files/{file_id}/", "/api/captions/", "/api/settings/", "/api/external/{source}/",  # covered by their bases
        "/api/search", "/api/packs/",  # the page's own search: /api/agent/search and /api/agent/packs serve agents
    )
    missing = sorted(
        r.path for r in app.routes
        if r.path.startswith("/api/") and r.path.split("{")[0].rstrip("/") not in text
        and not any(r.path.startswith(s) for s in skipped)
    )
    assert missing == []


def _upload_calls(monkeypatch, fail_put=False):
    calls = []

    def fake_json(args, path, params=None, body=None):
        calls.append((path, body))
        return {"id": 9, "status": "uploading"} if path == "/api/uploads" else {"id": 9, "status": "done"}

    def fake_put(args, path, local):
        if fail_put:
            raise skill.ApiError("HTTP 422: 文件没传完整")
        calls.append((path, local.read_bytes()))

    monkeypatch.setattr(skill, "_json", fake_json)
    monkeypatch.setattr(skill, "_put_file", fake_put)
    return calls


def test_a_folder_is_uploaded_as_one_pack_without_hidden_files(monkeypatch, tmp_path, capsys):
    pack = tmp_path / "Knight Pack"
    (pack / "sub").mkdir(parents=True)
    (pack / "sub" / "a.png").write_bytes(b"1")
    (pack / "b.txt").write_bytes(b"22")
    (pack / ".DS_Store").write_bytes(b"x")
    (pack / ".git").mkdir()
    (pack / ".git" / "HEAD").write_bytes(b"x")
    calls = _upload_calls(monkeypatch)

    assert skill.main(["upload", str(pack)]) == 0

    assert calls == [
        ("/api/uploads", {"folder": True, "name": "Knight Pack",
                          "files": [{"path": "b.txt", "size": 2}, {"path": "sub/a.png", "size": 1}]}),
        ("/api/uploads/9/files/0", b"22"),
        ("/api/uploads/9/files/1", b"1"),
        ("/api/uploads/9/finish", {}),
    ]
    assert json.loads(capsys.readouterr().out)["skipped"] == [".DS_Store", ".git/HEAD"]


def test_one_file_is_uploaded_as_itself(monkeypatch, tmp_path):
    (tmp_path / "a.zip").write_bytes(b"abc")
    calls = _upload_calls(monkeypatch)

    assert skill.main(["upload", str(tmp_path / "a.zip"), "--root", "nas"]) == 0

    assert calls[0] == ("/api/uploads", {"files": [{"path": "a.zip", "size": 3}], "root": "nas"})


def test_a_failed_upload_is_cancelled(monkeypatch, tmp_path):
    (tmp_path / "a.zip").write_bytes(b"abc")
    calls = _upload_calls(monkeypatch, fail_put=True)

    assert skill.main(["upload", str(tmp_path / "a.zip")]) == 1

    assert calls[-1] == ("/api/downloads/9/cancel", {})


def test_an_upload_waits_for_a_move_job_and_is_not_cancelled(monkeypatch, tmp_path):
    (tmp_path / "a.zip").write_bytes(b"abc")
    calls, busy = [], [2]

    def fake_json(args, path, params=None, body=None):
        calls.append(path)
        if path.endswith("/finish") and busy[0]:
            busy[0] -= 1
            raise skill.ApiError("HTTP 409: 整理正在移动文件：等它结束再放进 _下载")
        return {"id": 9, "status": "done"}

    monkeypatch.setattr(skill, "_json", fake_json)
    monkeypatch.setattr(skill, "_put_file", lambda args, path, local: None)
    monkeypatch.setattr(skill.time, "sleep", lambda s: None)

    assert skill.main(["upload", str(tmp_path / "a.zip")]) == 0
    assert calls.count("/api/uploads/9/finish") == 3 and not any("cancel" in c for c in calls)
