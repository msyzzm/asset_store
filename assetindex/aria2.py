"""A small client for aria2's JSON-RPC, and starting aria2c for the fetcher service.

aria2c runs inside the fetcher container, started by `python -m assetindex fetch --watch`,
with RPC on 127.0.0.1 only. It still gets a secret, fresh at every start: on a machine
shared with other programs (a dev box), nothing else may tell it what to download where.
"""

import json
import logging
import secrets
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

RPC_PORT = 6800
RPC_TIMEOUT_SECONDS = 10
MAX_CONCURRENT = 3
GID_NOT_FOUND = "is not found"  # in aria2's error message for a gid it does not know

# Fixed for every download. Torrents and metalinks are never followed: a pasted link
# must not turn into a swarm or a list of other downloads. No preallocation: slow or
# failing on NFS. A name that is taken is an error, never a silent rename or overwrite
# (each download gets its own folder, so it should never be taken).
DAEMON_OPTIONS = (
    "--enable-rpc=true",
    "--rpc-listen-all=false",
    "--follow-torrent=false",
    "--follow-metalink=false",
    "--file-allocation=none",
    "--continue=true",
    "--auto-file-renaming=false",
    "--allow-overwrite=false",
    "--content-disposition-default-utf8=true",
    f"--max-concurrent-downloads={MAX_CONCURRENT}",
    "--max-connection-per-server=4",
    "--split=4",
    "--min-split-size=8M",
    "--max-tries=5",
    "--retry-wait=10",
    "--connect-timeout=30",
    "--timeout=60",
    "--console-log-level=warn",
    "--summary-interval=0",
    "--show-console-readout=false",  # the live progress line would flood the container log
)


class Aria2Error(Exception):
    """aria2 answered with an error, or could not be reached."""


class Aria2Unreachable(Aria2Error):
    """No answer at all: aria2 is starting, restarting or gone. Not the link's fault."""


class Aria2:
    def __init__(self, port: int = RPC_PORT, secret: str = "", timeout: float = RPC_TIMEOUT_SECONDS) -> None:
        self.url = f"http://127.0.0.1:{port}/jsonrpc"
        self.secret = secret
        self.timeout = timeout

    def call(self, method: str, *params):
        params = [f"token:{self.secret}", *params] if self.secret else list(params)
        body = json.dumps({"jsonrpc": "2.0", "id": "a", "method": method, "params": params}).encode()
        request = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                answer = json.loads(response.read())
        except urllib.error.HTTPError as err:  # aria2 answers RPC errors with HTTP 400 and a JSON body
            try:
                answer = json.loads(err.read())
            except ValueError:
                raise Aria2Unreachable(f"aria2 HTTP {err.code}") from None
        except (OSError, ValueError) as err:
            raise Aria2Unreachable(f"aria2 unreachable: {err!r}") from None
        if "error" in answer:
            raise Aria2Error(str(answer["error"].get("message", answer["error"])))
        return answer.get("result")

    def add(self, url: str, folder: Path, user_agent: str, md5: str | None = None, out: str | None = None) -> str:
        """Start downloading `url` into `folder`, as `out` (a path inside it, folders
        made as needed) or else under the name the response gives. Returns the gid."""
        options = {"dir": str(folder), "user-agent": user_agent}
        if md5:
            options["checksum"] = f"md5={md5}"
        if out:
            options["out"] = out
        return self.call("aria2.addUri", [url], options)

    def status(self, gid: str) -> dict | None:
        """The download's state, or None if aria2 does not know the gid (it restarted)."""
        try:
            return self.call("aria2.tellStatus", gid, [
                "status", "totalLength", "completedLength", "downloadSpeed", "errorCode", "errorMessage", "files",
            ])
        except Aria2Error as err:
            if GID_NOT_FOUND in str(err):
                return None
            raise

    def forget(self, gid: str) -> None:
        """Stop the download if it runs and drop its result; unknown gids are fine."""
        for method in ("aria2.forceRemove", "aria2.removeDownloadResult"):
            try:
                self.call(method, gid)
            except Aria2Error as err:
                log.debug("%s %s: %s", method, gid, err)


def find_aria2c() -> str | None:
    return shutil.which("aria2c")


def new_secret() -> str:
    return secrets.token_hex(16)


def start_daemon(binary: str, secret: str, port: int = RPC_PORT) -> subprocess.Popen:
    return subprocess.Popen(
        [binary, f"--rpc-listen-port={port}", f"--rpc-secret={secret}", *DAEMON_OPTIONS],
        stdin=subprocess.DEVNULL,
    )
