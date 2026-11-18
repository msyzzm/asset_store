import json

import pytest
from PIL import Image

from assetindex import dupes, organize
from assetindex.caption import CaptionConfig
from assetindex.caption_job import Scope
from assetindex.config import Settings
from assetindex.organize import read_report, refresh_units, run_analysis
from assetindex.scanner import scan_root
from tests.fake_embed import FakeEmbedServer


@pytest.fixture(autouse=True)
def small_threshold(monkeypatch):
    monkeypatch.setattr(dupes, "HASH_MIN_BYTES", 100)


@pytest.fixture
def server():
    with FakeEmbedServer() as fake:
        yield fake


def _png(path, color):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 40), color).save(path)


@pytest.fixture
def lib(tmp_path, conn):
    root = tmp_path / "lib"
    for pack in ("Fire Pack", "Fire Pack (1)"):
        _png(root / "2D" / pack / "flame.png", (255, 0, 0))
        _png(root / "2D" / pack / "spark.png", (255, 60, 0))
    _png(root / "2D" / "Ice Pack" / "ice.png", (0, 0, 255))
    (root / "Audio").mkdir()
    (root / "Audio" / "boom.wav").write_bytes(b"RIFF" + bytes(200))
    settings = Settings(roots={"lib": root}, data_dir=tmp_path / "data")
    scan_root(conn, "lib", root)
    return conn, settings, root


def _config(server, **limits):
    return CaptionConfig(base_url=server.base_url, model="qwen", **limits)


def _rows(conn):
    return {r["path"]: r for r in conn.execute("SELECT * FROM organize_units")}


def test_analysis_marks_duplicates_and_proposes_a_place_for_everything_else(lib, server):
    conn, settings, _ = lib
    statuses = []

    final = run_analysis(conn, settings, Scope("lib"), _config(server), report=statuses.append)

    assert not final.running and final.error is None and final.done == 3 and final.failed == 0
    phases = [s.phase for s in statuses]
    assert phases[:2] == ["units", "sampling"] and "hashing" in phases
    rows = _rows(conn)
    assert set(rows) == {"2D/Fire Pack", "2D/Fire Pack (1)", "2D/Ice Pack", "Audio/boom.wav"}
    dup = rows["2D/Fire Pack (1)"]
    assert dup["dup_of"] == "2D/Fire Pack" and dup["target"] == "_Duplicates/Fire Pack (1)" and dup["status"] == "proposed"
    assert rows["2D/Fire Pack"]["target"] == "01_2D/VFX/Fake - 火焰特效"
    assert rows["2D/Ice Pack"]["category"] == "01_2D/Textures"
    assert rows["Audio/boom.wav"]["target"] == "03_Audio/SFX/Fake - 音效.wav"
    report = read_report(settings.data_dir)["lib"]
    assert report["dup_groups"] == 1 and report["dup_copies"] == 1 and report["dup_wasted"] > 0


def test_second_run_only_asks_about_new_packs(lib, server):
    conn, settings, root = lib
    run_analysis(conn, settings, Scope("lib"), _config(server))
    asked = len(server.requests)

    _png(root / "2D" / "New Pack" / "a.png", (0, 0, 255))
    scan_root(conn, "lib", root)
    final = run_analysis(conn, settings, Scope("lib"), _config(server))

    assert final.total == 1 and len(server.requests) == asked + 1


def test_proposals_of_changed_or_gone_packs_are_dropped(lib, server):
    conn, settings, root = lib
    run_analysis(conn, settings, Scope("lib"), _config(server))
    _png(root / "2D" / "Ice Pack" / "more.png", (0, 0, 200))
    (root / "Audio" / "boom.wav").unlink()
    scan_root(conn, "lib", root)

    refresh_units(conn, "lib")

    rows = _rows(conn)
    assert rows["2D/Ice Pack"]["status"] == "todo" and rows["2D/Ice Pack"]["target"] is None
    assert "Audio/boom.wav" not in rows


def test_decisions_survive_a_redo(lib, server):
    conn, settings, _ = lib
    run_analysis(conn, settings, Scope("lib"), _config(server))
    with conn:
        conn.execute("UPDATE organize_units SET status = 'accepted', target = '01_2D/UI/Mine' WHERE path = '2D/Ice Pack'")

    run_analysis(conn, settings, Scope("lib", redo=True), _config(server))

    row = _rows(conn)["2D/Ice Pack"]
    assert row["status"] == "accepted" and row["target"] == "01_2D/UI/Mine"


def test_scope_limits_classification_to_a_folder(lib, server):
    conn, settings, _ = lib
    final = run_analysis(conn, settings, Scope("lib", "Audio"), _config(server))
    rows = _rows(conn)
    assert final.done == 1 and rows["2D/Ice Pack"]["status"] == "todo" and rows["Audio/boom.wav"]["status"] == "proposed"


def test_scope_without_any_pack_says_so_instead_of_finishing_quietly(lib, server):
    conn, settings, _ = lib

    final = run_analysis(conn, settings, Scope("lib", "No Such Folder"), _config(server))

    assert final.total == 0 and "No Such Folder" in final.error


def test_split_turns_a_folder_into_several_packs(lib):
    conn, _, _ = lib
    with conn:
        conn.execute("INSERT INTO unit_splits (root, path) VALUES ('lib', '2D/Ice Pack')")
    refresh_units(conn, "lib")
    assert "2D/Ice Pack/ice.png" in _rows(conn) and "2D/Ice Pack" not in _rows(conn)


def test_api_down_stops_without_proposals(lib, server, monkeypatch):
    conn, settings, _ = lib
    server.fail_status = 503

    final = run_analysis(conn, settings, Scope("lib"), _config(server, max_failures=3, retry_wait=0))

    assert "503" in final.error and "连续 3 次" in final.error and final.done == 0 and final.failed == 0
    assert all(r["category"] is None for r in _rows(conn).values() if r["dup_of"] is None)


def test_unusable_answers_are_recorded_per_pack(lib, server, monkeypatch):
    conn, settings, _ = lib
    server.chat_reply = "说不清"

    final = run_analysis(conn, settings, Scope("lib"), _config(server, max_failures=2))

    assert final.failed >= 2 and "连续" in final.error  # questions already asked still finish
    assert sum(1 for r in _rows(conn).values() if r["error"]) == final.failed


def test_stop_between_packs(lib, server):
    conn, settings, _ = lib
    state = {"classifying": False, "checks": 0}

    def report(status):
        state["classifying"] = status.phase == "classifying"

    def should_stop():
        if state["classifying"]:
            state["checks"] += 1
        return state["checks"] > 1  # one pack, then stop

    final = run_analysis(conn, settings, Scope("lib"), _config(server), should_stop=should_stop, report=report)

    assert final.stopped and final.done == 1


def test_duplicate_marks_go_when_the_kept_copy_does(lib, server):
    import shutil

    conn, settings, root = lib
    run_analysis(conn, settings, Scope("lib"), _config(server))
    with conn:  # even a decided one: it would move the only copy left
        conn.execute("UPDATE organize_units SET status = 'accepted' WHERE path = '2D/Fire Pack (1)'")
    shutil.rmtree(root / "2D" / "Fire Pack")
    scan_root(conn, "lib", root)

    refresh_units(conn, "lib")

    row = _rows(conn)["2D/Fire Pack (1)"]
    assert row["dup_of"] is None and row["status"] == "todo" and row["target"] is None


@pytest.mark.parametrize(
    "status, target, marked",
    [
        ("accepted", "2D/Fire Pack (1)", True),  # accepted and moved before the twin was found: still a copy
        ("accepted", "01_2D/VFX/Fire", False),  # accepted, move still to come: the user's plan stands
        ("rejected", None, False),
    ],
)
def test_a_decided_copy_found_later_is_marked_unless_its_move_is_pending_or_rejected(lib, status, target, marked):
    """Live 2026-11-17: 6 packs and their .rar twins were all accepted and moved; the
    report counted 7 groups (7.35 GB) while the 重复 tab showed 0."""
    conn, _, _ = lib
    refresh_units(conn, "lib")
    with conn:
        conn.execute("UPDATE organize_units SET status = ?, target = ? WHERE path = '2D/Fire Pack (1)'", (status, target))

    organize.apply_duplicates(conn, "lib", organize.current_units(conn, "lib"))

    row = _rows(conn)["2D/Fire Pack (1)"]
    assert (row["dup_of"] == "2D/Fire Pack") is marked
    assert row["status"] == ("proposed" if marked else status)


def test_a_copy_already_set_aside_is_not_marked_again(lib):
    import shutil

    conn, _, root = lib
    (root / "_Duplicates").mkdir()
    shutil.move(root / "2D" / "Fire Pack (1)", root / "_Duplicates" / "Fire Pack (1)")
    scan_root(conn, "lib", root)
    refresh_units(conn, "lib")
    with conn:
        conn.execute("UPDATE organize_units SET status = 'accepted', target = path WHERE path = '_Duplicates/Fire Pack (1)'")

    organize.apply_duplicates(conn, "lib", organize.current_units(conn, "lib"))

    row = _rows(conn)["_Duplicates/Fire Pack (1)"]
    assert row["status"] == "accepted" and row["dup_of"] is None


def test_old_names_lose_junk_sources_and_notes_are_not_sorted(lib, server):
    conn, settings, root = lib
    (root / "2D" / "解压密码.txt").write_text("1234", encoding="utf-8")
    scan_root(conn, "lib", root)
    run_analysis(conn, settings, Scope("lib"), _config(server))
    assert _rows(conn)["2D/解压密码.txt"]["status"] == "todo"  # left where it is, not asked about

    organize.save_proposal(conn, "lib", "2D/Ice Pack", "01_2D/VFX", "UE - 冰霜特效", "r", "m")
    assert organize.clean_proposed_names(conn, "lib") == 1
    row = _rows(conn)["2D/Ice Pack"]
    assert row["name"] == "冰霜特效" and row["target"] == "01_2D/VFX/冰霜特效"


def test_a_proposal_to_stay_put_needs_no_one_to_accept_it(lib):
    """Live 2026-11-17: 10 of 12 packs to confirm had an arrow to where they already were."""
    conn, _, _ = lib
    conn.execute("INSERT OR REPLACE INTO organize_units (root, path, is_dir, files, size, signature, status, updated)"
                 " VALUES ('lib', '01_2D/VFX/冰霜特效', 1, 1, 1, 's', 'todo', 0), ('lib', '2D/Ice Pack', 1, 1, 1, 's', 'todo', 0)")
    conn.commit()

    organize.save_proposal(conn, "lib", "01_2D/VFX/冰霜特效", "01_2D/VFX", "冰霜特效", "r", "m")
    organize.save_proposal(conn, "lib", "2D/Ice Pack", "01_2D/VFX", "冰霜特效 2", "r", "m")

    rows = _rows(conn)
    assert rows["01_2D/VFX/冰霜特效"]["status"] == "accepted" and rows["01_2D/VFX/冰霜特效"]["reason"] == "r"
    assert rows["2D/Ice Pack"]["status"] == "proposed"  # a real move still waits for the user


def test_request_files(tmp_path):
    organize.request_job(tmp_path, Scope("lib", "2D"))
    assert organize.is_requested(tmp_path)
    assert organize.take_job(tmp_path) == {"root": "lib", "prefix": "2D", "redo": False}
    organize.request_stop(tmp_path)
    assert organize.stop_requested(tmp_path)
    organize.write_status(tmp_path, organize.OrganizeStatus(running=True, phase="hashing"))
    assert organize.read_status(tmp_path).phase == "hashing"
    (tmp_path / organize.STATUS_NAME).write_text(json.dumps({"running": 1}), encoding="utf-8")
    assert organize.read_status(tmp_path).running is False


def test_a_decided_folder_pack_is_not_taken_apart_by_the_rules(lib):
    conn, _, root = lib
    for name in ("Barrels", "Crates", "Lamps"):  # enough packs inside to be opened by default
        (root / "2D" / "Ice Pack" / f"{name}.zip").write_bytes(name.encode())
    scan_root(conn, "lib", root)
    refresh_units(conn, "lib")
    assert "2D/Ice Pack/Barrels.zip" in _rows(conn)

    with conn:  # as before an update brought the rule: the folder was one pack, and accepted
        conn.execute("DELETE FROM organize_units WHERE path LIKE '2D/Ice Pack/%'")
        conn.execute(
            "INSERT INTO organize_units (root, path, is_dir, files, size, signature, status, target, updated)"
            " VALUES ('lib', '2D/Ice Pack', 1, 4, 1, 'old', 'accepted', '01_2D/UI/冰', 0)"
        )
    refresh_units(conn, "lib")

    rows = _rows(conn)
    assert "2D/Ice Pack" in rows and "2D/Ice Pack/Barrels.zip" not in rows


def test_a_preview_appearing_next_to_an_archive_keeps_the_decision(lib):
    conn, _, root = lib
    (root / "2D" / "Swords.zip").write_bytes(b"swords")
    scan_root(conn, "lib", root)
    refresh_units(conn, "lib")
    with conn:
        conn.execute("UPDATE organize_units SET status = 'accepted', target = '01_2D/Props/Swords.zip' WHERE path = '2D/Swords.zip'")

    _png(root / "2D" / "Swords.png", (9, 9, 9))
    scan_root(conn, "lib", root)
    refresh_units(conn, "lib")

    row = _rows(conn)["2D/Swords.zip"]
    assert row["status"] == "accepted" and row["files"] == 2


class _Overlapping:
    """A model client that waits until two questions are open at once (or gives up)."""

    def __init__(self, server):
        import threading

        from assetindex.caption import CaptionClient

        self.inner = CaptionClient(_config(server))
        self.config = self.inner.config
        self.lock = threading.Lock()
        self.both = threading.Event()
        self.open = self.peak = 0

    def ask(self, prompt, jpeg, max_tokens):
        with self.lock:
            self.open += 1
            self.peak = max(self.peak, self.open)
            if self.open >= 2:
                self.both.set()
        self.both.wait(5)
        try:
            return self.inner.ask(prompt, jpeg, max_tokens)
        finally:
            with self.lock:
                self.open -= 1


def test_several_packs_wait_for_the_model_at_once(lib, server):
    conn, settings, _ = lib
    client = _Overlapping(server)

    final = run_analysis(conn, settings, Scope("lib"), _config(server), client=client)

    assert client.peak >= 2 and final.done == 3 and final.error is None
    assert _rows(conn)["2D/Ice Pack"]["category"] == "01_2D/Textures"


def test_a_picture_that_cannot_be_made_fails_only_that_pack(lib, server, monkeypatch):
    conn, settings, _ = lib
    real = organize.prepare_question

    def broken(conn_, settings_, root, path, is_dir):
        if path == "2D/Ice Pack":
            raise OSError("NAS gone for a moment")
        return real(conn_, settings_, root, path, is_dir)

    monkeypatch.setattr(organize, "prepare_question", broken)

    final = run_analysis(conn, settings, Scope("lib"), _config(server))

    assert final.done == 2 and final.failed == 1 and final.error is None
    assert "NAS gone" in _rows(conn)["2D/Ice Pack"]["error"]


def test_api_going_down_stops_asking_but_keeps_answers_already_given(lib, server, monkeypatch):
    from assetindex.embedding import EmbedError

    conn, settings, _ = lib
    client = _Overlapping(server)
    real = client.ask
    calls = []

    def ask(prompt, jpeg, max_tokens):
        calls.append(1)
        if len(calls) == 2:
            client.both.set()
            raise EmbedError("HTTP 503")
        return real(prompt, jpeg, max_tokens)

    client.ask = ask
    final = run_analysis(conn, settings, Scope("lib"), _config(server, workers=2, max_failures=1), client=client)

    assert "503" in final.error and final.done == 1 and len(calls) == 2  # the third is never asked
    assert sum(1 for r in _rows(conn).values() if r["status"] == "proposed" and r["dup_of"] is None) == 1


def _refusing(server, refusals):
    """A client whose requests fail like a restarting model server when refusals(n, prompt)."""
    import itertools
    import threading

    from assetindex.caption import CaptionClient
    from assetindex.embedding import EmbedError

    client = CaptionClient(_config(server))
    real, counter, lock = client.ask, itertools.count(1), threading.Lock()
    client.calls = 0

    def ask(prompt, jpeg, max_tokens):
        with lock:
            n = client.calls = next(counter)
        if refusals(n, prompt):
            raise EmbedError("connection refused")
        return real(prompt, jpeg, max_tokens)

    client.ask = ask
    return client


def test_a_pack_the_api_failed_on_is_asked_again_after_a_pause(lib, server, monkeypatch):
    conn, settings, _ = lib
    client = _refusing(server, lambda n, prompt: n <= 2)

    final = run_analysis(conn, settings, Scope("lib"), _config(server, retry_wait=0), client=client)

    assert final.error is None and final.done == 3 and final.failed == 0 and client.calls == 5
    assert all(r["error"] is None for r in _rows(conn).values())


def test_a_pack_the_api_always_fails_on_is_given_up_without_stopping_the_rest(lib, server, monkeypatch):
    conn, settings, _ = lib
    client = _refusing(server, lambda n, prompt: "boom.wav" in prompt)

    final = run_analysis(conn, settings, Scope("lib"), _config(server, retry_wait=0, retries=1), client=client)

    assert final.error is None and final.done == 2 and final.failed == 1
    assert "2 次都出错" in _rows(conn)["Audio/boom.wav"]["error"]


def test_stop_during_the_pause_after_an_api_error(lib, server, monkeypatch):
    import time

    conn, settings, _ = lib
    client = _refusing(server, lambda n, prompt: True)

    started = time.monotonic()
    final = run_analysis(conn, settings, Scope("lib"), _config(server, retry_wait=30, workers=1), client=client,
                         should_stop=lambda: client.calls > 0)

    assert final.stopped and final.done == 0 and final.failed == 0
    assert time.monotonic() - started < 10  # left the 30 s pause at once


def test_an_old_proposal_for_a_note_is_dropped(lib, server):
    conn, settings, root = lib
    (root / "2D" / "解压密码.txt").write_text("1234", encoding="utf-8")
    scan_root(conn, "lib", root)
    refresh_units(conn, "lib")
    organize.save_proposal(conn, "lib", "2D/解压密码.txt", "_Unsorted", "解压密码说明", "r", "m")

    run_analysis(conn, settings, Scope("lib"), _config(server))

    row = _rows(conn)["2D/解压密码.txt"]
    assert row["status"] == "todo" and row["target"] is None and row["category"] is None


def test_packs_already_set_aside_are_not_sorted_again(lib, server):
    conn, settings, root = lib
    _png(root / "_待删除" / "Old Pack" / "a.png", (9, 9, 9))
    _png(root / "_Duplicates" / "Copy Pack" / "b.png", (8, 8, 8))
    scan_root(conn, "lib", root)

    run_analysis(conn, settings, Scope("lib"), _config(server))

    rows = _rows(conn)
    assert rows["_待删除/Old Pack"]["status"] == "todo" and rows["_Duplicates/Copy Pack"]["status"] == "todo"
