"""The "更新" button: ask the host to pull the latest code and rebuild the containers.

Containers can't (and shouldn't) rebuild themselves: that needs the Docker socket, i.e.
root on the host. Instead a small systemd service on the Docker host (deploy/updater)
watches update.request in the data volume. The web server only writes that one-word
request ("check" or "update") and shows update.status, which the host writes:

    {"state": "idle|checking|running|done|error", "current": "e95b3c0",
     "latest": "f00ba12", "behind": 2, "pending": ["f00ba12 feat: ..."],
     "checked": <epoch s>, "started": <epoch s>, "finished": <epoch s>,
     "error": "...", "log": "last lines of output"}
"""

import json
import logging
import time
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Body, Depends, FastAPI, HTTPException
from pydantic import BaseModel

log = logging.getLogger(__name__)

REQUEST_NAME = "update.request"
STATUS_NAME = "update.status"
ACTIONS = ("check", "update")
STUCK_SECONDS = 50 * 60  # longer than the service's own 45 min limit: by now it has died
STUCK_CHECK_SECONDS = 10 * 60  # a check is one git fetch (itself limited to 5 min)
REQUEST_STALE_SECONDS = 2 * 60  # systemd picks a request up within a second when it's running
MAX_PENDING = 50
MAX_TEXT = 4000
_STRINGS = ("state", "current", "latest", "deployed", "error", "log")
_NUMBERS = ("checked", "started", "finished")


def _clean_status(raw) -> dict:
    """Only the expected fields, each of the expected type: the file comes from outside."""
    raw = raw if isinstance(raw, dict) else {}
    out = {k: raw[k][:MAX_TEXT] if isinstance(raw.get(k), str) else None for k in _STRINGS}
    for k in _NUMBERS:
        value = raw.get(k)
        out[k] = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    behind = raw.get("behind")
    out["behind"] = behind if isinstance(behind, int) and not isinstance(behind, bool) and behind >= 0 else None
    pending = raw.get("pending")
    out["pending"] = [p[:300] for p in pending if isinstance(p, str)][:MAX_PENDING] if isinstance(pending, list) else []
    return out


def update_state(data_dir: Path) -> dict:
    """What the page shows: is the host updater installed, what's pending, what happened."""
    try:
        # A commit message in another encoding must not hide the whole status.
        raw = json.loads((data_dir / STATUS_NAME).read_text(encoding="utf-8", errors="replace"))
        installed = True
    except FileNotFoundError:
        raw, installed = {}, False
    except (OSError, ValueError):
        raw, installed = {}, True
    requested, stale = _requested(data_dir)
    state = {"installed": installed, "requested": requested, **_clean_status(raw)}
    # Built from an older commit than the checkout (e.g. pull worked, build failed): rebuild.
    state["needs_rebuild"] = bool(state["deployed"] and state["current"] and state["deployed"] != state["current"])
    age = time.time() - state["started"] if state["started"] else 0
    if state["state"] == "running" and age > STUCK_SECONDS:
        state["state"] = "error"
        state["error"] = state["error"] or "更新太久没完成，可能卡住了，看数据卷里的 update.log"
    elif state["state"] == "checking" and age > STUCK_CHECK_SECONDS:
        state["state"] = "error"
        state["error"] = "检查更新卡住了（网络？），可以再点一次检查"
    if stale:
        state["state"] = "error"
        state["error"] = "更新服务没响应：请求两分钟没人处理，检查 LXC 上的 asset-updater.path 是否在运行"
    return state


def _requested(data_dir: Path) -> tuple[str | None, bool]:
    """The pending request, and whether it has waited so long nobody will pick it up."""
    path = data_dir / REQUEST_NAME
    try:
        word = path.read_text(encoding="utf-8").strip()
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None, False
    if word not in ACTIONS:
        return None, False
    if age > REQUEST_STALE_SECONDS:
        return None, True  # don't block the buttons: a new request replaces it
    return word, False


def request(data_dir: Path, action: str) -> None:
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}")
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = data_dir / f"{REQUEST_NAME}.tmp"
    tmp.write_text(action + "\n", encoding="utf-8")
    tmp.replace(data_dir / REQUEST_NAME)  # the watcher fires on creation: never show it half-written


class UpdateRequest(BaseModel):
    action: Literal["check", "update"]


def add_update_routes(app: FastAPI, data_dir: Path, page_request) -> None:
    @app.get("/api/update")
    def api_update_status() -> dict:
        return update_state(data_dir)

    # No login: anyone on the LAN can press this. It only pulls the configured branch
    # (fast-forward only) and rebuilds; the host decides what that means.
    @app.post("/api/update", status_code=202, dependencies=[Depends(page_request)])
    def api_update_request(body: Annotated[UpdateRequest, Body()]) -> dict:
        state = update_state(data_dir)
        if state["state"] in ("running", "checking") or state["requested"]:
            raise HTTPException(409, "已经在检查或更新了")
        try:
            request(data_dir, body.action)
        except OSError as err:
            log.error("cannot write update request: %r", err)
            raise HTTPException(503, "没能提交请求，检查数据目录") from None
        return update_state(data_dir)
