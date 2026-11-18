from pathlib import Path

import pytest

from assetindex import downloads, scan_request
from assetindex.aria2 import Aria2Error, Aria2Unreachable
from assetindex.categories import DOWNLOADS, PARTIAL
from assetindex.fetch_job import Fetcher, find_twin, partial_dir, safe_name
from assetindex.scanner import scan_root
from assetindex.units import SCHEME_FOLDERS, layout

PLENTY = 10**15


class FakeAria2:
    """Keeps downloads in a dict; a test sets their state, or `finish` writes the file."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.added: list[tuple[str, Path, str | None]] = []
        self.forgotten: list[str] = []
        self.headers: list[dict | None] = []
        self.refuse = False

    def add(self, url, folder, user_agent, md5=None, out=None, headers=None):
        if self.refuse:
            raise Aria2Error("bad uri")
        gid = f"g{len(self.added)}"
        self.headers.append(headers)
        self.added.append((url, folder, md5) if out is None else (url, folder, md5, out))
        self.jobs[gid] = {"status": "active", "totalLength": "0", "completedLength": "0", "downloadSpeed": "0",
                          "folder": folder, "out": out}
        return gid

    def status(self, gid):
        return self.jobs.get(gid)

    def forget(self, gid):
        self.forgotten.append(gid)
        self.jobs.pop(gid, None)

    def finish(self, gid, name="pack.zip", data=b"new pack"):
        job = self.jobs[gid]
        path = job["folder"] / (job["out"] or name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if job["out"] is None:
            path.with_name(f"{path.name}.aria2").write_bytes(b"ctl")  # a leftover the landing must not carry
        job["status"] = "complete"

    def gid_of(self, out):
        return next(g for g, j in self.jobs.items() if j["out"] == out)


@pytest.fixture
def rpc():
    return FakeAria2()


def _fetcher(conn, settings, rpc, **kw):
    kw.setdefault("free", lambda path: PLENTY)
    return Fetcher(conn, settings, rpc, now=lambda: 100.0, run_later=lambda fn, *args: fn(*args), **kw)


def _base(settings) -> Path:
    return settings.roots["art"].resolve()


def test_a_queued_link_goes_to_aria2_into_its_own_folder(conn, settings, rpc):
    download_id = downloads.add(conn, "art", "https://example.org/a.zip", md5="0" * 32)

    _fetcher(conn, settings, rpc).tick()

    assert rpc.added == [("https://example.org/a.zip", partial_dir(_base(settings), download_id), "0" * 32)]
    assert partial_dir(_base(settings), download_id).is_dir()
    assert downloads.get(conn, download_id)["status"] == "active"


def test_a_finished_download_lands_in_the_downloads_folder_and_asks_for_a_scan(conn, settings, rpc):
    download_id = downloads.add(conn, "art", "https://example.org/a.zip")
    fetcher = _fetcher(conn, settings, rpc)
    fetcher.tick()
    rpc.finish("g0", name="石墙 Pack.zip")

    fetcher.tick()

    row = downloads.get(conn, download_id)
    assert (row["status"], row["path"], row["dup_of"], row["finished"]) == ("done", f"{DOWNLOADS}/石墙 Pack.zip", None, 100.0)
    assert (_base(settings) / DOWNLOADS / "石墙 Pack.zip").read_bytes() == b"new pack"
    assert not partial_dir(_base(settings), download_id).exists()  # the .aria2 file went with it
    assert scan_request.is_requested(settings.data_dir)
    assert "g0" in rpc.forgotten


def test_a_name_already_taken_gets_a_number_never_an_overwrite(conn, settings, rpc):
    (_base(settings) / DOWNLOADS).mkdir()
    (_base(settings) / DOWNLOADS / "pack.zip").write_bytes(b"older")
    download_id = downloads.add(conn, "art", "https://example.org/a.zip")
    fetcher = _fetcher(conn, settings, rpc)
    fetcher.tick()
    rpc.finish("g0")

    fetcher.tick()

    assert downloads.get(conn, download_id)["path"] == f"{DOWNLOADS}/pack-2.zip"
    assert (_base(settings) / DOWNLOADS / "pack.zip").read_bytes() == b"older"


def test_a_download_identical_to_a_library_file_is_kept_and_marked(conn, settings, rpc):
    data = (_base(settings) / "Audio" / "Battle_Theme.mp3").read_bytes()
    scan_root(conn, "art", _base(settings))
    download_id = downloads.add(conn, "art", "https://example.org/theme.mp3")
    fetcher = _fetcher(conn, settings, rpc)
    fetcher.tick()
    rpc.finish("g0", name="theme.mp3", data=data)

    fetcher.tick()

    row = downloads.get(conn, download_id)
    assert (row["status"], row["dup_of"]) == ("duplicate", "Audio/Battle_Theme.mp3")
    assert (_base(settings) / DOWNLOADS / "theme.mp3").exists()


def test_same_size_but_different_content_is_no_twin(conn, settings, tmp_path):
    scan_root(conn, "art", _base(settings))
    other = tmp_path / "other.mp3"
    other.write_bytes(b"9876543210" * 100)  # same size as Battle_Theme.mp3

    assert find_twin(conn, "art", _base(settings), other, "x/other.mp3") is None


@pytest.mark.parametrize("page", [b"<!DOCTYPE html><html><body>login</body></html>", b"\n  <html lang=en>"])
def test_a_web_page_instead_of_a_file_fails_with_a_hint(conn, settings, rpc, page):
    download_id = downloads.add(conn, "art", "https://example.org/asset/123")
    fetcher = _fetcher(conn, settings, rpc)
    fetcher.tick()
    rpc.finish("g0", name="123", data=page)

    fetcher.tick()

    row = downloads.get(conn, download_id)
    assert row["status"] == "failed" and "网页" in row["error"]
    assert not (_base(settings) / DOWNLOADS).joinpath("123").exists()
    assert not partial_dir(_base(settings), download_id).exists()


def test_a_finished_download_waits_while_a_move_job_runs(conn, settings, rpc):
    download_id = downloads.add(conn, "art", "https://example.org/a.zip")
    busy = {"on": True}
    fetcher = _fetcher(conn, settings, rpc, mover_busy=lambda: busy["on"])
    fetcher.tick()
    rpc.finish("g0")

    fetcher.tick()
    assert downloads.get(conn, download_id)["status"] == "active"

    busy["on"] = False
    fetcher.tick()
    assert downloads.get(conn, download_id)["status"] == "done"


def test_an_aria2_error_fails_the_download_with_a_readable_reason_and_clears_its_folder(conn, settings, rpc):
    download_id = downloads.add(conn, "art", "https://example.org/a.zip")
    fetcher = _fetcher(conn, settings, rpc)
    fetcher.tick()
    rpc.jobs["g0"].update(status="error", errorCode="32", errorMessage="")

    fetcher.tick()

    row = downloads.get(conn, download_id)
    assert row["status"] == "failed" and "md5" in row["error"]
    assert not partial_dir(_base(settings), download_id).exists()


def test_a_link_aria2_refuses_fails_at_once(conn, settings, rpc):
    rpc.refuse = True
    download_id = downloads.add(conn, "art", "https://example.org/a.zip")

    _fetcher(conn, settings, rpc).tick()

    assert downloads.get(conn, download_id)["status"] == "failed"


def test_an_active_download_aria2_forgot_is_handed_over_again_into_the_same_folder(conn, settings, rpc):
    download_id = downloads.add(conn, "art", "https://example.org/a.zip")
    fetcher = _fetcher(conn, settings, rpc)
    fetcher.tick()
    rpc.jobs.clear()  # the container restarted

    fetcher.tick()

    assert len(rpc.added) == 2 and rpc.added[1][1] == partial_dir(_base(settings), download_id)
    assert downloads.get(conn, download_id)["status"] == "active"


def test_a_partial_file_without_its_control_file_is_started_over(conn, settings, rpc):
    download_id = downloads.add(conn, "art", "https://example.org/a.zip")
    fetcher = _fetcher(conn, settings, rpc)
    fetcher.tick()
    folder = partial_dir(_base(settings), download_id)
    (folder / "a.zip").write_bytes(b"half, with holes")
    (folder / "kept.zip").write_bytes(b"half")
    (folder / "kept.zip.aria2").write_bytes(b"ctl")
    rpc.jobs.clear()  # restarted, and a.zip lost its .aria2

    fetcher.tick()

    assert list(folder.iterdir()) == []  # handed over again into an empty folder
    assert len(rpc.added) == 2


def test_a_partial_file_with_its_control_file_is_kept_for_aria2_to_resume(conn, settings, rpc):
    download_id = downloads.add(conn, "art", "https://example.org/a.zip")
    fetcher = _fetcher(conn, settings, rpc)
    fetcher.tick()
    folder = partial_dir(_base(settings), download_id)
    (folder / "a.zip").write_bytes(b"half")
    (folder / "a.zip.aria2").write_bytes(b"ctl")
    rpc.jobs.clear()

    fetcher.tick()

    assert sorted(p.name for p in folder.iterdir()) == ["a.zip", "a.zip.aria2"]


def test_a_cancelled_download_is_stopped_and_its_folder_cleared(conn, settings, rpc):
    download_id = downloads.add(conn, "art", "https://example.org/a.zip")
    fetcher = _fetcher(conn, settings, rpc)
    fetcher.tick()
    downloads.cancel(conn, download_id)

    fetcher.tick()

    assert "g0" in rpc.forgotten
    assert not partial_dir(_base(settings), download_id).exists()
    assert downloads.retry(conn, download_id)  # let go: it can be queued again


def test_a_download_cancelled_while_being_handed_over_is_not_revived(conn, settings, rpc):
    download_id = downloads.add(conn, "art", "https://example.org/a.zip")
    real_add = rpc.add

    def add_then_cancel(*args, **kwargs):
        gid = real_add(*args)
        downloads.cancel(conn, download_id)  # the web server, between our read and our write
        return gid

    rpc.add = add_then_cancel

    _fetcher(conn, settings, rpc).tick()

    assert downloads.get(conn, download_id)["status"] == "cancelled"
    assert rpc.forgotten == ["g0"]
    assert not partial_dir(_base(settings), download_id).exists()


def test_while_aria2_does_not_answer_queued_downloads_wait(conn, settings, rpc):
    download_id = downloads.add(conn, "art", "https://example.org/a.zip")

    def unreachable(*args, **kwargs):
        raise Aria2Unreachable("starting")

    rpc.add = unreachable

    with pytest.raises(Aria2Unreachable):
        _fetcher(conn, settings, rpc).tick()
    assert downloads.get(conn, download_id)["status"] == "queued"


def test_a_file_that_cannot_be_moved_fails_and_its_folder_is_cleared(conn, settings, rpc):
    download_id = downloads.add(conn, "art", "https://example.org/a.zip")

    def broken_move(src, dst, is_dir):
        raise PermissionError("read-only")

    fetcher = _fetcher(conn, settings, rpc, move=broken_move)
    fetcher.tick()
    rpc.finish("g0")

    fetcher.tick()

    assert "放不进" in downloads.get(conn, download_id)["error"]
    assert not partial_dir(_base(settings), download_id).exists()


def test_a_landed_download_is_recorded_even_if_the_duplicate_check_breaks(conn, settings, rpc):
    download_id = downloads.add(conn, "art", "https://example.org/a.zip")
    later = []
    fetcher = Fetcher(conn, settings, rpc, free=lambda path: PLENTY, run_later=lambda fn, *args: later.append(args))
    fetcher.tick()
    rpc.finish("g0")

    fetcher.tick()

    row = downloads.get(conn, download_id)
    assert (row["status"], row["path"]) == ("done", f"{DOWNLOADS}/pack.zip")
    assert later == [(download_id, "art", f"{DOWNLOADS}/pack.zip")]


def test_a_known_size_without_room_is_refused_before_downloading(conn, settings, rpc):
    download_id = downloads.add(conn, "art", "https://example.org/a.zip", size=5 * 1024**3)

    _fetcher(conn, settings, rpc, free=lambda path: 2 * 1024**3).tick()

    assert rpc.added == []
    assert "空间" in downloads.get(conn, download_id)["error"]


def test_a_size_learnt_while_downloading_is_checked_against_free_space(conn, settings, rpc):
    download_id = downloads.add(conn, "art", "https://example.org/a.zip")
    free = {"bytes": PLENTY}
    fetcher = _fetcher(conn, settings, rpc, free=lambda path: free["bytes"])
    fetcher.tick()
    rpc.jobs["g0"].update(totalLength=str(5 * 1024**3), completedLength="0")
    free["bytes"] = 2 * 1024**3

    assert fetcher.tick() == {}

    assert downloads.get(conn, download_id)["status"] == "failed"


def test_progress_reports_running_downloads(conn, settings, rpc):
    download_id = downloads.add(conn, "art", "https://example.org/a.zip")
    fetcher = _fetcher(conn, settings, rpc)
    fetcher.tick()
    rpc.jobs["g0"].update(totalLength="100", completedLength="40", downloadSpeed="7")

    assert fetcher.tick() == {download_id: {"done": 40, "total": 100, "speed": 7}}
    assert downloads.get(conn, download_id)["size"] == 100


def test_scans_skip_downloads_still_running(conn, settings):
    folder = _base(settings) / DOWNLOADS / PARTIAL / "1"
    folder.mkdir(parents=True)
    (folder / "half.zip").write_bytes(b"half")

    scan_root(conn, "art", _base(settings))

    assert conn.execute("SELECT COUNT(*) FROM files WHERE rel_path LIKE ?", (f"{DOWNLOADS}/%",)).fetchone()[0] == 0


def test_each_download_is_a_pack_of_its_own():
    shape = layout([f"{DOWNLOADS}/a.zip", f"{DOWNLOADS}/b.zip"], always_open=SCHEME_FOLDERS)

    assert shape.unit_of(f"{DOWNLOADS}/a.zip") == (f"{DOWNLOADS}/a.zip", False)


PARTS = [
    {"url": "https://example.org/crate.fbx", "out": "crate.fbx", "size": 3, "md5": "a" * 32},
    {"url": "https://example.org/diff.jpg", "out": "textures/crate_diff.jpg", "size": 4},
]


def _bundle(conn, title="Wooden Crate 01 FBX"):
    return downloads.add_bundle(conn, "art", "https://polyhaven.com/a/crate", title, PARTS, source="polyhaven",
                                source_id="crate")


def test_a_download_of_several_files_lands_as_one_folder(conn, settings, rpc):
    download_id = _bundle(conn)
    fetcher = _fetcher(conn, settings, rpc)
    fetcher.tick()
    folder = partial_dir(_base(settings), download_id)
    assert [a[3] for a in rpc.added] == ["crate.fbx", "textures/crate_diff.jpg"]
    assert rpc.added[0][2] == "a" * 32 and rpc.added[0][1] == folder
    rpc.finish(rpc.gid_of("crate.fbx"), data=b"fbx")
    assert fetcher.tick() == {download_id: {"done": 3, "total": 7, "speed": 0}}
    rpc.finish(rpc.gid_of("textures/crate_diff.jpg"), data=b"diff")

    fetcher.tick()

    row = downloads.get(conn, download_id)
    landed = _base(settings) / DOWNLOADS / "Wooden Crate 01 FBX"
    assert (row["status"], row["path"]) == ("done", f"{DOWNLOADS}/Wooden Crate 01 FBX")
    assert sorted(p.relative_to(landed).as_posix() for p in landed.rglob("*") if p.is_file()) == [
        "crate.fbx", "textures/crate_diff.jpg"]
    assert not folder.exists() and scan_request.is_requested(settings.data_dir)


def test_after_a_restart_only_unfinished_parts_go_again_and_a_part_file_without_control_is_dropped(conn, settings, rpc):
    download_id = _bundle(conn)
    fetcher = _fetcher(conn, settings, rpc)
    fetcher.tick()
    rpc.finish(rpc.gid_of("crate.fbx"), data=b"fbx")
    fetcher.tick()  # crate.fbx recorded done
    folder = partial_dir(_base(settings), download_id)
    (folder / "textures").mkdir()
    (folder / "textures" / "crate_diff.jpg").write_bytes(b"di")  # half, its control file lost
    rpc.jobs.clear()  # the container restarted

    fetcher.tick()

    assert [a[3] for a in rpc.added[2:]] == ["textures/crate_diff.jpg"]
    assert not (folder / "textures" / "crate_diff.jpg").exists()
    assert (folder / "crate.fbx").read_bytes() == b"fbx"


def test_a_part_that_fails_fails_the_whole_download(conn, settings, rpc):
    download_id = _bundle(conn)
    fetcher = _fetcher(conn, settings, rpc)
    fetcher.tick()
    rpc.jobs[rpc.gid_of("textures/crate_diff.jpg")].update(status="error", errorCode="3")

    fetcher.tick()

    row = downloads.get(conn, download_id)
    assert row["status"] == "failed" and "textures/crate_diff.jpg" in row["error"] and "404" in row["error"]
    assert "g0" in rpc.forgotten and not partial_dir(_base(settings), download_id).exists()
    assert all(p["gid"] is None for p in downloads.parts(conn, download_id))


def test_a_cancelled_download_of_several_files_stops_every_part_and_retries_from_scratch(conn, settings, rpc):
    download_id = _bundle(conn)
    fetcher = _fetcher(conn, settings, rpc)
    fetcher.tick()
    rpc.finish(rpc.gid_of("crate.fbx"), data=b"fbx")
    fetcher.tick()
    downloads.cancel(conn, download_id)

    fetcher.tick()

    assert "g1" in rpc.forgotten and not partial_dir(_base(settings), download_id).exists()
    assert downloads.retry(conn, download_id)
    assert [p["done"] for p in downloads.parts(conn, download_id)] == [0, 0]


def test_parts_started_before_aria2_stopped_answering_are_stopped_when_cancelled(conn, settings, rpc):
    download_id = _bundle(conn)
    real_add = rpc.add

    def add_once_then_unreachable(*args, **kwargs):
        if rpc.added:
            raise Aria2Unreachable("restarting")
        return real_add(*args, **kwargs)

    rpc.add = add_once_then_unreachable
    fetcher = _fetcher(conn, settings, rpc)
    with pytest.raises(Aria2Unreachable):
        fetcher.tick()
    downloads.cancel(conn, download_id)

    fetcher.tick()

    assert rpc.forgotten == ["g0"]
    assert downloads.retry(conn, download_id)


def test_a_download_of_several_files_without_room_is_refused(conn, settings, rpc):
    download_id = _bundle(conn)

    _fetcher(conn, settings, rpc, free=lambda path: 1024**3).tick()

    assert downloads.get(conn, download_id)["status"] == "failed" and rpc.added == []


@pytest.mark.parametrize(("name", "expected"), [
    ("pack.zip", "pack.zip"),
    ("../../etc/passwd", "_.._etc_passwd"),
    ('a<b>:c"d|e?f*.zip', "a_b__c_d_e_f_.zip"),
    ("..hidden.zip", "hidden.zip"),
    ("", "download-7"),
    ("...", "download-7"),
    ("x" * 300 + ".zip", "x" * 196 + ".zip"),
    ("石" * 100 + ".7z", "石" * 65 + ".7z"),  # 3 bytes each: 198 of 200
])
def test_safe_name(name, expected):
    assert safe_name(name, 7) == expected


# ---- links that expire (Sketchfab): asked for at each start ----


SPEC = "sketchfab:" + "a" * 32 + ":glb"


def _expiring(conn):
    return downloads.add(conn, "art", "https://sketchfab.com/3d-models/x", title="Crate GLB", source="sketchfab",
                         resolve=SPEC)


def test_a_link_that_expires_is_asked_for_at_each_start_under_the_name_given(conn, settings, rpc):
    asked = []

    def resolve(spec, title):
        asked.append((spec, title))
        return f"https://cdn.example.org/{len(asked)}.glb?sig=x", "Crate GLB.glb", {}

    clock = [100.0]
    download_id = _expiring(conn)
    fetcher = _fetcher(conn, settings, rpc, resolve=resolve)
    fetcher.now = lambda: clock[0]
    fetcher.tick()
    rpc.jobs.clear()  # the container restarted: the first link has expired by now
    clock[0] += 600
    fetcher.tick()

    assert asked == [(SPEC, "Crate GLB"), (SPEC, "Crate GLB")]
    assert [a[0] for a in rpc.added] == ["https://cdn.example.org/1.glb?sig=x", "https://cdn.example.org/2.glb?sig=x"]
    assert rpc.added[1][3] == "Crate GLB.glb" and downloads.get(conn, download_id)["status"] == "active"


def test_a_link_the_site_will_not_give_fails_the_download_and_retry_asks_again(conn, settings, rpc):
    answers = [ValueError("网站不认这个 key（HTTP 401）"), ("https://cdn.example.org/1.glb", "Crate GLB.glb", {})]

    def resolve(spec, title):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    download_id = _expiring(conn)
    fetcher = _fetcher(conn, settings, rpc, resolve=resolve)
    fetcher.tick()
    row = downloads.get(conn, download_id)
    assert row["status"] == "failed" and "HTTP 401" in row["error"] and not rpc.added

    assert downloads.retry(conn, download_id)
    fetcher.tick()
    assert downloads.get(conn, download_id)["status"] == "active" and rpc.added[0][0] == "https://cdn.example.org/1.glb"


def test_plain_links_are_not_asked_for(conn, settings, rpc):
    downloads.add(conn, "art", "https://example.org/a.zip")

    _fetcher(conn, settings, rpc, resolve=lambda spec, title: pytest.fail("asked")).tick()

    assert rpc.added[0][0] == "https://example.org/a.zip"


class _Busy(Exception):
    transient = True


def test_a_site_down_while_asked_for_a_link_leaves_the_download_waiting_and_asks_again_later(conn, settings, rpc):
    clock, asked = [100.0], []

    def resolve(spec, title):
        asked.append(clock[0])
        if len(asked) == 1:
            raise _Busy("连不上网站：TimeoutError")
        return "https://cdn.example.org/1.glb", "Crate GLB.glb", {}

    download_id = _expiring(conn)
    fetcher = _fetcher(conn, settings, rpc, resolve=resolve)
    fetcher.now = lambda: clock[0]
    fetcher.tick()
    fetcher.tick()  # too soon to ask again
    assert downloads.get(conn, download_id)["status"] == "queued" and asked == [100.0]

    clock[0] += 61
    fetcher.tick()
    assert downloads.get(conn, download_id)["status"] == "active" and len(asked) == 2


def test_a_link_asked_for_lately_serves_again_while_aria2_does_not_answer(conn, settings, rpc):
    asked = []

    def resolve(spec, title):
        asked.append(spec)
        return "https://cdn.example.org/1.glb", "Crate GLB.glb", {}

    def unreachable(*args, **kwargs):
        raise Aria2Unreachable("starting")

    _expiring(conn)
    fetcher = _fetcher(conn, settings, rpc, resolve=resolve)
    rpc.add = unreachable
    for _ in range(3):
        with pytest.raises(Aria2Unreachable):
            fetcher.tick()

    assert len(asked) == 1


def test_a_download_whose_link_expired_midway_gets_a_fresh_one_a_few_times_then_fails(conn, settings, rpc):
    links = iter(f"https://cdn.example.org/{n}.glb" for n in range(10))
    clock = [100.0]
    download_id = _expiring(conn)
    fetcher = _fetcher(conn, settings, rpc, resolve=lambda spec, title: (next(links), "Crate GLB.glb", {}))
    fetcher.now = lambda: clock[0]
    fetcher.tick()

    for n in range(4):
        gid = conn.execute("SELECT gid FROM downloads WHERE id = ?", (download_id,)).fetchone()["gid"]
        rpc.jobs[gid].update(status="error", errorCode="22")  # 403: the link expired
        fetcher.tick()

    row = downloads.get(conn, download_id)
    assert [a[0] for a in rpc.added] == [f"https://cdn.example.org/{n}.glb" for n in range(4)]
    assert row["status"] == "failed"


def test_a_site_key_goes_to_aria2_as_a_header_and_never_in_the_queue(conn, settings, rpc):
    download_id = downloads.add(conn, "art", "https://poly.pizza/m/abc", title="Tree GLB", source="polypizza",
                                resolve="polypizza:abc:glb")

    _fetcher(conn, settings, rpc, resolve=lambda spec, title: ("https://static.poly.pizza/x.glb", "Tree GLB.glb",
                                                                {"x-auth-token": "secret"})).tick()

    assert rpc.headers == [{"x-auth-token": "secret"}] and rpc.added[0][3] == "Tree GLB.glb"
    assert "secret" not in str(dict(conn.execute("SELECT * FROM downloads WHERE id = ?", (download_id,)).fetchone()))
