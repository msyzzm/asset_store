"""Against a real aria2c, if one is on PATH: the RPC calls and options are what aria2
accepts, and a download goes all the way to _下载. Skipped without aria2c."""

import functools
import hashlib
import socket
import threading
import time
from http.server import (
    BaseHTTPRequestHandler,
    SimpleHTTPRequestHandler,
    ThreadingHTTPServer,
)

import pytest

from assetindex import aria2, downloads
from assetindex.categories import DOWNLOADS
from assetindex.fetch_job import Fetcher

BINARY = aria2.find_aria2c()
pytestmark = pytest.mark.skipif(BINARY is None, reason="aria2c not installed")
DATA = bytes(range(256)) * 2000


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def site(tmp_path):
    folder = tmp_path / "site"
    folder.mkdir()
    (folder / "pack.zip").write_bytes(DATA)

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(folder)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def rpc():
    port, secret = _free_port(), aria2.new_secret()
    process = aria2.start_daemon(BINARY, secret, port)
    client = aria2.Aria2(port=port, secret=secret)
    for _ in range(50):
        try:
            client.call("aria2.getVersion")
            break
        except aria2.Aria2Error:
            time.sleep(0.1)
    yield client
    process.terminate()
    process.wait(10)


def _run(fetcher, conn, download_id, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        fetcher.tick()
        if downloads.get(conn, download_id)["status"] not in downloads.OPEN:
            return downloads.get(conn, download_id)
        time.sleep(0.2)
    raise AssertionError("download did not end in time")


def test_a_real_download_lands_in_the_downloads_folder(conn, settings, rpc, site):
    md5 = hashlib.md5(DATA).hexdigest()
    download_id = downloads.add(conn, "art", f"{site}/pack.zip", md5=md5)

    row = _run(Fetcher(conn, settings, rpc), conn, download_id)

    assert (row["status"], row["path"]) == ("done", f"{DOWNLOADS}/pack.zip")
    assert (settings.roots["art"] / DOWNLOADS / "pack.zip").read_bytes() == DATA


def test_a_wrong_md5_fails(conn, settings, rpc, site):
    download_id = downloads.add(conn, "art", f"{site}/pack.zip", md5="0" * 32)

    row = _run(Fetcher(conn, settings, rpc), conn, download_id)

    assert row["status"] == "failed" and "md5" in row["error"]


def test_a_missing_file_fails_as_not_found(conn, settings, rpc, site):
    download_id = downloads.add(conn, "art", f"{site}/nope.zip")

    row = _run(Fetcher(conn, settings, rpc), conn, download_id)

    assert row["status"] == "failed" and "404" in row["error"]


# Over 2 x --min-split-size (8M), so aria2 fetches it in segments, out of order.
BIG = bytes(range(251)) * (24 * 1024 * 1024 // 251)


@pytest.fixture
def range_site():
    """Serves BIG at /big.bin with Range support (SimpleHTTPRequestHandler has none)."""

    class RangeHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path != "/big.bin":
                self.send_error(404)
                return
            start, end = 0, len(BIG) - 1
            asked = self.headers.get("Range")
            if asked:
                first, _, last = asked.removeprefix("bytes=").partition("-")
                start, end = int(first), int(last) if last else len(BIG) - 1
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(BIG)}")
            else:
                self.send_response(200)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            try:
                self.wfile.write(BIG[start:end + 1])
            except OSError:
                pass  # aria2 was killed mid-transfer

    server = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/big.bin"
    server.shutdown()


def _daemon(port, secret):
    process = aria2.start_daemon(BINARY, secret, port)
    client = aria2.Aria2(port=port, secret=secret)
    for _ in range(50):
        try:
            client.call("aria2.getVersion")
            return process, client
        except aria2.Aria2Error:
            time.sleep(0.1)
    raise AssertionError("aria2c did not start")


@pytest.mark.parametrize("lose_control_file", [False, True])
def test_a_download_cut_by_a_hard_kill_resumes_into_the_same_bytes(conn, settings, range_site, lose_control_file):
    port, secret = _free_port(), aria2.new_secret()
    process, client = _daemon(port, secret)
    client.call("aria2.changeGlobalOption", {"max-overall-download-limit": "3M"})
    fetcher = Fetcher(conn, settings, client, run_later=lambda fn, *args: None)
    download_id = downloads.add(conn, "art", range_site)
    folder = settings.roots["art"].resolve() / DOWNLOADS / ".partial" / str(download_id)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        progress = fetcher.tick()
        if progress.get(download_id, {}).get("done", 0) > 6 * 1024 * 1024:
            break
        time.sleep(0.2)
    process.kill()  # no chance to save anything
    process.wait(10)
    if lose_control_file:
        for control in folder.glob("*.aria2"):
            control.unlink()
    process, client = _daemon(port, secret)  # the container came back
    try:
        fetcher.rpc = client
        row = _run(fetcher, conn, download_id, seconds=60)
    finally:
        process.terminate()
        process.wait(10)

    assert row["status"] == "done"
    assert (settings.roots["art"] / row["path"]).read_bytes() == BIG


def test_a_real_download_of_several_files_lands_as_one_folder(conn, settings, rpc, site):
    parts = [{"url": f"{site}/pack.zip", "out": "pack.zip", "md5": hashlib.md5(DATA).hexdigest()},
             {"url": f"{site}/pack.zip", "out": "textures/copy.zip"}]
    download_id = downloads.add_bundle(conn, "art", site, "Crate 1K", parts)

    row = _run(Fetcher(conn, settings, rpc, run_later=lambda fn, *args: None), conn, download_id)

    landed = settings.roots["art"] / DOWNLOADS / "Crate 1K"
    assert row["status"] == "done"
    assert (landed / "pack.zip").read_bytes() == DATA and (landed / "textures" / "copy.zip").read_bytes() == DATA
    assert not list(landed.rglob("*.aria2"))


@pytest.mark.parametrize("lose_control_file", [False, True])
def test_a_download_of_several_files_cut_by_a_hard_kill_ends_with_the_same_bytes(
        conn, settings, site, range_site, lose_control_file):
    port, secret = _free_port(), aria2.new_secret()
    process, client = _daemon(port, secret)
    client.call("aria2.changeGlobalOption", {"max-overall-download-limit": "3M"})
    fetcher = Fetcher(conn, settings, client, run_later=lambda fn, *args: None)
    parts = [{"url": f"{site}/pack.zip", "out": "small.zip"}, {"url": range_site, "out": "maps/big.bin"}]
    download_id = downloads.add_bundle(conn, "art", site, "Big Set", parts)
    folder = settings.roots["art"].resolve() / DOWNLOADS / ".partial" / str(download_id)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        progress = fetcher.tick()
        if progress.get(download_id, {}).get("done", 0) > 6 * 1024 * 1024:
            break
        time.sleep(0.2)
    process.kill()
    process.wait(10)
    if lose_control_file:
        (folder / "maps" / "big.bin.aria2").unlink(missing_ok=True)
    process, client = _daemon(port, secret)
    try:
        fetcher.rpc = client
        row = _run(fetcher, conn, download_id, seconds=60)
    finally:
        process.terminate()
        process.wait(10)

    landed = settings.roots["art"] / row["path"]
    assert row["status"] == "done"
    assert (landed / "maps" / "big.bin").read_bytes() == BIG and (landed / "small.zip").read_bytes() == DATA


def test_an_unknown_gid_reads_as_none(rpc):
    assert rpc.status("0123456789abcdef") is None


def test_a_caller_without_the_secret_is_refused(rpc):
    stranger = aria2.Aria2(port=int(rpc.url.split(":")[2].split("/")[0]))

    with pytest.raises(aria2.Aria2Error):
        stranger.call("aria2.getVersion")
