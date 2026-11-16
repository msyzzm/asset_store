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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8000"
TIMEOUT_SECONDS = 300  # downloads of big archives and extractions can take a while
CHUNK_BYTES = 1024 * 1024
WRITE_HEADERS = {"X-Requested-With": "asset-store-skill", "Content-Type": "application/json"}


class ApiError(Exception):
    pass


def _base(args) -> str:
    return (args.url or os.environ.get("ASSET_STORE_URL") or DEFAULT_URL).rstrip("/")


def _open(args, path: str, params: dict | None = None, body: dict | None = None):
    url = _base(args) + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=WRITE_HEADERS if data is not None else {})
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


def _json(args, path, params=None, body=None):
    with _open(args, path, params, body) as resp:
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


def cmd_scan(args):
    return _json(args, "/api/scan", body={})


def cmd_caption(args):
    return _json(args, "/api/captions", body={"root": args.root, "prefix": args.prefix, "redo": args.redo})


def cmd_caption_stop(args):
    return _json(args, "/api/captions/stop", body={})


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
    sub.add_parser("scan", help="ask for a scan now (picks up new files on the NAS)").set_defaults(func=cmd_scan)

    p = sub.add_parser("caption", help="start describing pictures in a folder (slow: seconds per picture)")
    p.add_argument("--root", required=True)
    p.add_argument("--prefix", default="", help="folder under the root; empty = the whole root")
    p.add_argument("--redo", action="store_true")
    p.set_defaults(func=cmd_caption)
    sub.add_parser("caption-stop", help="stop the describe-pictures job").set_defaults(func=cmd_caption_stop)
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
