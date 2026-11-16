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
