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
        self.refuse = False

    def add(self, url, folder, user_agent, md5=None):
        if self.refuse:
            raise Aria2Error("bad uri")
        gid = f"g{len(self.added)}"
        self.added.append((url, folder, md5))
        self.jobs[gid] = {"status": "active", "totalLength": "0", "completedLength": "0", "downloadSpeed": "0",
                          "folder": folder}
        return gid

    def status(self, gid):
        return self.jobs.get(gid)

    def forget(self, gid):
        self.forgotten.append(gid)
        self.jobs.pop(gid, None)

    def finish(self, gid, name="pack.zip", data=b"new pack"):
        folder = self.jobs[gid]["folder"]
        (folder / name).write_bytes(data)
        (folder / f"{name}.aria2").write_bytes(b"ctl")
        self.jobs[gid]["status"] = "complete"


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

    def add_then_cancel(*args):
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

    def unreachable(*args):
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
