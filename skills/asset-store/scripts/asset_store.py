#!/usr/bin/env python3
"""Command-line client for the asset library's agent API. Standard library only.

Set ASSET_STORE_URL (e.g. http://192.168.1.50:8000) or pass --url. Output is JSON.
Run with --help, or see ../SKILL.md.
"""

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8000"
TIMEOUT_SECONDS = 300  # downloads of big archives and extractions can take a while
CHUNK_BYTES = 1024 * 1024
WRITE_HEADERS = {"X-Requested-With": "asset-store-skill", "Content-Type": "application/json"}
UPLOAD_WAIT_SECONDS = 5  # a running move job holds an upload's landing back: asked again after this


class ApiError(Exception):
    pass


def _base(args) -> str:
    return (args.url or os.environ.get("ASSET_STORE_URL") or DEFAULT_URL).rstrip("/")


def _open(args, path: str, params: dict | None = None, body: dict | None = None, method: str | None = None):
    url = _base(args) + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=WRITE_HEADERS if data is not None else {}, method=method)
    return _send(request)


def _send(request: urllib.request.Request):
    url = request.full_url
    try:
        return urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)
    except urllib.error.HTTPError as err:
        try:
            body = json.loads(err.read())
            detail = body.get("detail") if isinstance(body, dict) else None
        except (ValueError, OSError):
            detail = None
        raise ApiError(f"HTTP {err.code}: {detail or err.reason} ({url})") from None
    except urllib.error.URLError as err:
        raise ApiError(f"cannot reach {url}: {err.reason} (is ASSET_STORE_URL right?)") from None


def _json(args, path, params=None, body=None, method=None):
    with _open(args, path, params, body, method) as resp:
        return json.loads(resp.read())


def _filename(resp, fallback: str) -> str:
    header = resp.headers.get("Content-Disposition", "")
    match = re.search(r"filename\*=UTF-8''([^;]+)", header, re.I) or re.search(r'filename="([^"]+)"', header)
    name = urllib.parse.unquote(match.group(1).strip()) if match else fallback
    name = Path(name.replace("\\", "/")).name  # never a path from the server
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name)  # not allowed in Windows file names
    return name or fallback


def _free_name(out_dir: Path, name: str) -> Path:
    """`name` in out_dir, or "name (1).ext", ...: two entries called icon.png from
    different folders must not overwrite each other (or an existing file)."""
    target = out_dir / name
    stem, suffix = target.stem, target.suffix
    n = 1
    while target.exists():
        target = out_dir / f"{stem} ({n}){suffix}"
        n += 1
    return target


def _save(args, path: str, fallback: str) -> dict:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    with _open(args, path) as resp:
        target = _free_name(out_dir, _filename(resp, fallback))
        partial = target.with_name(target.name + ".part")
        size = 0
        try:
            with partial.open("wb") as f:
                while chunk := resp.read(CHUNK_BYTES):
                    f.write(chunk)
                    size += len(chunk)
            partial.replace(target)  # only a complete download gets the real name
        finally:
            partial.unlink(missing_ok=True)
    return {"saved": str(target.resolve()), "bytes": size}


def cmd_search(args):
    params = {"q": " ".join(args.query), "kind": args.kind, "mode": args.mode, "limit": args.limit, "offset": args.offset}
    return _json(args, "/api/agent/search", params)


def cmd_packs(args):
    params = {"q": " ".join(args.query), "category": args.category, "view": args.view, "style": args.style,
              "mode": args.mode, "limit": args.limit,
              "offset": args.offset}
    return _json(args, "/api/agent/packs", params)


def cmd_show(args):
    return _json(args, f"/api/agent/files/{args.id}")


def cmd_entries(args):
    return _json(args, f"/api/agent/files/{args.id}/entries", {"q": args.filter, "limit": args.limit, "offset": args.offset})


def cmd_get(args):
    if args.entry:
        return _save(args, f"/api/agent/files/{args.id}/entries/{args.entry}/download", f"entry-{args.entry}")
    return _save(args, f"/api/files/{args.id}/download", f"file-{args.id}")


def cmd_thumb(args):
    return _save(args, f"/api/files/{args.id}/thumb", f"thumb-{args.id}.webp")


def cmd_tasks(args):
    return _json(args, "/api/agent/tasks")


def cmd_encrypted(args):
    return _json(args, "/api/agent/encrypted", {"limit": args.limit, "offset": args.offset})


def cmd_scan(args):
    return _json(args, "/api/scan", body={})


def cmd_scan_stop(args):
    return _json(args, "/api/scan/stop", body={})


def cmd_caption(args):
    return _json(args, "/api/captions", body={"root": args.root, "prefix": args.prefix, "redo": args.redo})


def cmd_caption_stop(args):
    return _json(args, "/api/captions/stop", body={})


def cmd_download(args):
    body = {"url": args.link, "title": args.title or None, "root": args.root or None}
    return _json(args, "/api/downloads", body={k: v for k, v in body.items() if v is not None})


def cmd_downloads(args):
    state = _json(args, "/api/downloads")
    return {**state, "items": state["items"][: args.limit]}


def cmd_download_cancel(args):
    return _json(args, f"/api/downloads/{args.id}/cancel", body={})


def cmd_download_retry(args):
    return _json(args, f"/api/downloads/{args.id}/retry", body={})


def _upload_files(path: Path) -> tuple[list[tuple[str, Path]], list[str]]:
    """The files to send, as (path inside the upload, local path), and the hidden ones left out."""
    if path.is_file():
        return [(path.name, path)], []
    files, skipped = [], []
    for local in sorted(path.rglob("*")):
        if not local.is_file():
            continue
        rel = local.relative_to(path).as_posix()
        (skipped.append(rel) if any(n.startswith(".") for n in rel.split("/")) else files.append((rel, local)))
    return files, skipped


def _put_file(args, path: str, local: Path) -> None:
    with local.open("rb") as f:
        headers = {"X-Requested-With": WRITE_HEADERS["X-Requested-With"], "Content-Type": "application/octet-stream",
                   "Content-Length": str(local.stat().st_size)}
        _send(urllib.request.Request(_base(args) + path, data=f, headers=headers, method="PUT")).close()


def _finish_upload(args, upload_id: int) -> dict:
    while True:  # a move job ends; giving up would throw away what was sent
        try:
            return _json(args, f"/api/uploads/{upload_id}/finish", body={})
        except ApiError as err:
            if "整理正在移动" not in str(err):
                raise
            time.sleep(UPLOAD_WAIT_SECONDS)


def cmd_upload(args):
    path = Path(args.path)
    if not path.exists():
        raise ValueError(f"没有这个文件或文件夹：{path}")
    files, skipped = _upload_files(path)
    if not files:
        raise ValueError("文件夹里没有可上传的文件")
    body = {"files": [{"path": rel, "size": local.stat().st_size} for rel, local in files], "root": args.root or None}
    if path.is_dir():
        body = {"folder": True, "name": args.name or path.resolve().name, **body}
    upload = _json(args, "/api/uploads", body={k: v for k, v in body.items() if v is not None})
    try:
        for idx, (_, local) in enumerate(files):
            _put_file(args, f"/api/uploads/{upload['id']}/files/{idx}", local)
        result = _finish_upload(args, upload["id"])
    except BaseException:
        try:  # nothing half-sent left on the NAS
            _json(args, f"/api/downloads/{upload['id']}/cancel", body={})
        except (ApiError, OSError):
            pass
        raise
    return {**result, "skipped": skipped}


def cmd_external_keys(args):
    return _json(args, "/api/external/keys")


# ---- organizing the library, moving and deleting on the NAS, settings, updates ----


def cmd_stats(args):
    return _json(args, "/api/stats")


def cmd_organize(args):
    return _json(args, "/api/organize")


def cmd_organize_start(args):
    return _json(args, "/api/organize", body={"root": args.root, "prefix": args.prefix, "redo": args.redo})


def cmd_organize_stop(args):
    return _json(args, "/api/organize/stop", body={})


def cmd_units(args):
    params = {"root": args.root, "show": args.show, "category": args.category, "q": args.q,
              "limit": args.limit, "offset": args.offset}
    return _json(args, "/api/organize/units", params)


def cmd_unit_pictures(args):
    return _json(args, "/api/organize/units/pictures", {"root": args.root, "path": args.path})


def cmd_decide(args):
    return _json(args, "/api/organize/units/decide", body={"root": args.root, "paths": args.paths, "action": args.action})


def cmd_accept_all(args):
    body = {"root": args.root, "show": args.show, "category": args.category, "q": args.q}
    return _json(args, "/api/organize/units/accept-all", body=body)


def cmd_retarget(args):
    return _json(args, "/api/organize/units/target", body={"root": args.root, "path": args.path, "target": args.target})


def _unit_action(route):
    def run(args):
        return _json(args, route, body={"root": args.root, "path": args.path})
    return run


def cmd_splits(args):
    return _json(args, "/api/organize/splits", {"root": args.root})


def cmd_moves(args):
    return _json(args, "/api/organize/moves")


def cmd_move_plan(args):
    return _json(args, "/api/organize/moves/plan", {"root": args.root})


def cmd_trash(args):
    return _json(args, "/api/organize/trash", {"root": args.root})


def cmd_locked(args):
    return _json(args, "/api/organize/locked", {"root": args.root})


def cmd_move(args):
    body = {"action": args.action, "root": args.root, "journal": args.journal, "password": args.password,
            "limit": args.limit, "paths": args.paths or None}
    return _json(args, "/api/organize/moves", body={k: v for k, v in body.items() if v is not None})


def cmd_move_stop(args):
    return _json(args, "/api/organize/moves/stop", body={})


def cmd_tags(args):
    return _json(args, "/api/organize/tags")


def cmd_tag(args):
    return _json(args, "/api/organize/tags", body={"root": args.root, "paths": args.paths})


def cmd_tag_stop(args):
    return _json(args, "/api/organize/tags/stop", body={})


SETTINGS = ("caption", "embedding", "passwords")
TESTABLE = ("caption", "embedding")


def cmd_settings(args):
    return {name: _json(args, f"/api/settings/{name}") for name in SETTINGS}


def cmd_settings_set(args):
    try:
        update = json.loads(args.values)
    except ValueError:
        raise ValueError('values must be JSON, e.g. {"model": "qwen"}') from None
    if not isinstance(update, dict):
        raise ValueError('values must be a JSON object, e.g. {"model": "qwen"}')
    return _json(args, f"/api/settings/{args.which}", body=update, method="PUT")


def cmd_settings_test(args):
    return _json(args, f"/api/settings/{args.which}/test", body={})


def cmd_update(args):
    return _json(args, "/api/update")


def cmd_update_run(args):
    return _json(args, "/api/update", body={"action": args.action})


def cmd_update_stop(args):
    return _json(args, "/api/update/stop", body={})


def _admin_parsers(sub) -> None:
    sub.add_parser("stats", help="how many files of each kind the library has").set_defaults(func=cmd_stats)

    sub.add_parser("organize", help="the organize analysis: progress and its last report").set_defaults(func=cmd_organize)
    p = sub.add_parser("organize-start", help="analyse a root or folder: duplicates and where each pack belongs (uses the model)")
    p.add_argument("--root", required=True)
    p.add_argument("--prefix", default="", help="only this folder; empty = the whole root")
    p.add_argument("--redo", action="store_true", help="ask the model again for packs that have a proposal")
    p.set_defaults(func=cmd_organize_start)
    sub.add_parser("organize-stop", help="stop the organize analysis").set_defaults(func=cmd_organize_stop)

    p = sub.add_parser("units", help="the packs of a root and what the analysis proposes for each")
    p.add_argument("--root", required=True)
    p.add_argument("--show", default="proposed", choices=["proposed", "duplicates", "accepted", "rejected", "todo", "failed", "all"])
    p.add_argument("--category", default="")
    p.add_argument("--q", default="", help="words in the pack's path")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(func=cmd_units)
    p = sub.add_parser("unit-pictures", help="the pictures of one pack (to look at it)")
    p.add_argument("--root", required=True)
    p.add_argument("path")
    p.set_defaults(func=cmd_unit_pictures)
    p = sub.add_parser("decide", help="accept, reject, reset or redo (ask the model again) proposals")
    p.add_argument("action", choices=["accept", "reject", "reset", "redo"])
    p.add_argument("--root", required=True)
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_decide)
    p = sub.add_parser("accept-all", help="accept every proposal of one category in the list")
    p.add_argument("--root", required=True)
    p.add_argument("--show", default="proposed", choices=["proposed", "duplicates"])
    p.add_argument("--category", required=True)
    p.add_argument("--q", default="")
    p.set_defaults(func=cmd_accept_all)
    p = sub.add_parser("retarget", help="change where a pack goes: <category>/<name> (also _待删除/<name>)")
    p.add_argument("--root", required=True)
    p.add_argument("path")
    p.add_argument("target")
    p.set_defaults(func=cmd_retarget)
    for name, route, text in (
        ("keep", "/api/organize/units/keep", "keep this copy of a duplicate instead of the one chosen"),
        ("split", "/api/organize/split", "treat each subfolder of this folder as its own pack"),
        ("join", "/api/organize/join", "keep this folder as one pack"),
        ("unsplit", "/api/organize/unsplit", "undo a split"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("--root", required=True)
        p.add_argument("path")
        p.set_defaults(func=_unit_action(route))
    p = sub.add_parser("splits", help="the folders split into packs")
    p.add_argument("--root", required=True)
    p.set_defaults(func=cmd_splits)

    sub.add_parser("moves", help="file jobs on the NAS: the running one, history, whether the NAS is writable").set_defaults(
        func=cmd_moves)
    p = sub.add_parser("move-plan", help="what `move apply` would move now (accepted proposals), with problems")
    p.add_argument("--root", required=True)
    p.set_defaults(func=cmd_move_plan)
    p = sub.add_parser("trash", help="what `move purge` would delete for good (the _待删除 folder)")
    p.add_argument("--root", required=True)
    p.set_defaults(func=cmd_trash)
    p = sub.add_parser("locked", help="password-protected archives `move unpack` could open")
    p.add_argument("--root", required=True)
    p.set_defaults(func=cmd_locked)
    p = sub.add_parser("move", help="a file job on the NAS: apply, undo, recover, unpack, verify, purge, repair, lift")
    p.add_argument("action", choices=["apply", "undo", "recover", "unpack", "verify", "purge", "repair", "lift"])
    p.add_argument("--root", required=True)
    p.add_argument("--journal", help="undo: which run (a name from `moves` history)")
    p.add_argument("--password", help="unpack (required), repair, lift: the archives' password")
    p.add_argument("--limit", type=int, help="unpack: at most this many archives")
    p.add_argument("paths", nargs="*", help="verify, repair (2+ copies, the first named), lift; unpack: only these")
    p.set_defaults(func=cmd_move)
    sub.add_parser("move-stop", help="stop the running file job after the pack it is on").set_defaults(func=cmd_move_stop)

    sub.add_parser("tags", help="the tagging job (view, style, themes of packs)").set_defaults(func=cmd_tags)
    p = sub.add_parser("tag", help="tag these packs with the model (view, style, themes)")
    p.add_argument("--root", required=True)
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_tag)
    sub.add_parser("tag-stop", help="stop the tagging job").set_defaults(func=cmd_tag_stop)

    sub.add_parser("settings", help="the vision model (caption) and semantic search (embedding) settings; keys never shown"
                   ).set_defaults(func=cmd_settings)
    p = sub.add_parser("settings-set", help="change settings: settings-set caption '{\"model\": \"qwen\"}'")
    p.add_argument("which", choices=SETTINGS)
    p.add_argument("values", help="JSON object of the fields to change")
    p.set_defaults(func=cmd_settings_set)
    p = sub.add_parser("settings-test", help="try the settings: caption describes a sample picture, embedding embeds a word")
    p.add_argument("which", choices=TESTABLE)
    p.set_defaults(func=cmd_settings_test)

    sub.add_parser("update", help="the server's version and whether a newer one is out").set_defaults(func=cmd_update)
    p = sub.add_parser("update-run", help="check: look for a newer version; update: install it and restart the services")
    p.add_argument("action", choices=["check", "update"])
    p.set_defaults(func=cmd_update_run)
    sub.add_parser("update-stop", help="stop an update before it restarts the services").set_defaults(func=cmd_update_stop)


def _external_path(args, suffix: str = "") -> str:
    site, asset = (urllib.parse.quote(v, safe="") for v in (args.site, args.asset_id))
    return f"/api/external/{site}/{asset}{suffix}"


def cmd_external(args):
    params = {"q": " ".join(args.query), "source": args.source, "offset": args.offset}
    found = _json(args, "/api/external/search", params)
    sites = {name: {**site, "items": site.get("items", [])[: args.limit]} for name, site in found["sources"].items()}
    return {**found, "sources": sites}


def cmd_external_show(args):
    return _json(args, _external_path(args))


def cmd_external_get(args):
    body = {"option": args.option, "root": args.root or None}
    return _json(args, _external_path(args, "/download"), body={k: v for k, v in body.items() if v is not None})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asset_store.py", description="Search and fetch assets from the asset library.")
    parser.add_argument("--url", help=f"server address (default: $ASSET_STORE_URL or {DEFAULT_URL})")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search", help="find assets by name, path, contents or description")
    p.add_argument("query", nargs="*")
    p.add_argument("--kind", choices=["image", "audio", "video", "model", "archive", "unreal", "other"])
    p.add_argument("--mode", choices=["auto", "text", "semantic"], default="auto")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("packs", help="find asset packs (folders, archives): one result per pack, best first")
    p.add_argument("query", nargs="*", help="none: every pack (of --category), by path")
    p.add_argument("--category", default="", help="e.g. 02_3D/Environments or 01_2D")
    p.add_argument("--view", default="", help="view tag, e.g. 俯视 or 横版 (only tagged packs)")
    p.add_argument("--style", default="", help="style tag, e.g. 像素 or 卡通 (only tagged packs)")
    p.add_argument("--mode", choices=["auto", "text"], default="auto")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(func=cmd_packs)

    p = sub.add_parser("show", help="one asset in detail (archives: their contents)")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("entries", help="files inside an archive, optionally filtered")
    p.add_argument("id", type=int)
    p.add_argument("--filter", default="")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(func=cmd_entries)

    p = sub.add_parser("get", help="download an asset, or with --entry one file from inside an archive")
    p.add_argument("id", type=int)
    p.add_argument("--entry", type=int)
    p.add_argument("--out", default=".", help="directory to save into (created if missing)")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("thumb", help="save an asset's thumbnail (.webp) to look at it")
    p.add_argument("id", type=int)
    p.add_argument("--out", default=".")
    p.set_defaults(func=cmd_thumb)

    sub.add_parser("tasks", help="what the scanner and the captioner are doing").set_defaults(func=cmd_tasks)
    p = sub.add_parser("encrypted", help="archives that need a password: locked files, or locked names")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(func=cmd_encrypted)
    sub.add_parser("scan", help="ask for a scan now (picks up new files on the NAS)").set_defaults(func=cmd_scan)
    sub.add_parser("scan-stop", help="stop the running scan (or drop one not started yet)").set_defaults(
        func=cmd_scan_stop
    )

    p = sub.add_parser("caption", help="start describing pictures in a folder (slow: seconds per picture)")
    p.add_argument("--root", required=True)
    p.add_argument("--prefix", default="", help="folder under the root; empty = the whole root")
    p.add_argument("--redo", action="store_true")
    p.set_defaults(func=cmd_caption)
    sub.add_parser("caption-stop", help="stop the describe-pictures job").set_defaults(func=cmd_caption_stop)

    p = sub.add_parser("download", help="have the server download a link (http/https) into the library's _下载 folder")
    p.add_argument("link")
    p.add_argument("--title", default="", help="a name to show in the download list")
    p.add_argument("--root", default="", help="root to download into; empty = the first one")
    p.set_defaults(func=cmd_download)
    p = sub.add_parser("upload", help="send a local file or folder into the library's _下载 folder (a folder = one pack)")
    p.add_argument("path")
    p.add_argument("--name", default="", help="the pack's name for a folder; empty = the folder's own name")
    p.add_argument("--root", default="", help="root to upload into; empty = the first one")
    p.set_defaults(func=cmd_upload)
    p = sub.add_parser("downloads", help="the download queue: running first, then the latest finished")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_downloads)

    p = sub.add_parser("download-cancel", help="cancel a queued or running download")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_download_cancel)
    p = sub.add_parser("download-retry", help="queue a failed or cancelled download again")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_download_retry)

    sub.add_parser("external-keys", help="which sites' keys are set (Sketchfab, Freesound, Poly Pizza)").set_defaults(
        func=cmd_external_keys)
    p = sub.add_parser("external", help="search free asset sites (Poly Haven, OpenGameArt, ...): English words")
    p.add_argument("query", nargs="*")
    p.add_argument("--source", default="", help="one site only, e.g. polyhaven or openverse_audio; empty = every site")
    p.add_argument("--offset", type=int, default=0, help="next page: the site's `next`")
    p.add_argument("--limit", type=int, default=10, help="items kept per site")
    p.set_defaults(func=cmd_external)

    p = sub.add_parser("external-show", help="one asset on a site: description, license, download options")
    p.add_argument("site")
    p.add_argument("asset_id")
    p.set_defaults(func=cmd_external_show)

    p = sub.add_parser("external-get", help="have the server download one option of a site's asset into _下载")
    p.add_argument("site")
    p.add_argument("asset_id")
    p.add_argument("--option", required=True, help="an option's `key` from external-show, e.g. fbx/1k")
    p.add_argument("--root", default="", help="root to download into; empty = the first one")
    p.set_defaults(func=cmd_external_get)

    _admin_parsers(sub)
    return parser


def _prune(value):
    """Drop nulls: less to read, same meaning. Empty lists stay (callers index into them)."""
    if isinstance(value, dict):
        return {k: _prune(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_prune(v) for v in value]
    return value


def _fail(message: str) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
    return 1


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")  # Chinese paths on a Windows console
    args = _parser().parse_args(argv)
    try:
        result = args.func(args)
    except ApiError as err:
        return _fail(str(err))
    except (OSError, http.client.HTTPException, ValueError) as err:  # timeouts, cut-off downloads, disk errors
        return _fail(f"{type(err).__name__}: {err}")
    print(json.dumps(_prune(result), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
