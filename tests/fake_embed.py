"""A tiny OpenAI-style /embeddings server for tests.

Vectors are "semantic" on purpose so search tests can check meaning, not just plumbing:
synonyms across languages share a dimension, and an image's vector comes from its
colour (a red image means "fire").
"""

import base64
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

CONCEPTS = {
    "sword": 0, "剑": 0, "blade": 0,
    "fire": 1, "火": 1, "火焰": 1, "red": 1,
    "explosion": 2, "爆炸": 2,
    "music": 3, "音乐": 3, "battle": 3, "theme": 3,
}
DIM = 6
BIAS = DIM - 1  # every vector has a little of this so none is all zeros


def text_vector(text: str) -> list[float]:
    vec = [0.0] * DIM
    lowered = text.lower()
    for word, dim in CONCEPTS.items():
        if word in lowered:
            vec[dim] += 1.0
    vec[BIAS] = 0.1
    return vec


def image_vector(data_url: str) -> list[float]:
    raw = base64.b64decode(data_url.split(",", 1)[1])
    r, g, b = Image.open(io.BytesIO(raw)).convert("RGB").resize((1, 1)).getpixel((0, 0))
    vec = [0.0] * DIM
    vec[1] = r / 255  # red = fire
    vec[4] = (g + b) / 510
    vec[BIAS] = 0.1
    return vec


def _item_vector(item) -> list[float]:
    if isinstance(item, dict) and "image" in item:
        return image_vector(item["image"])
    if isinstance(item, str) and item.startswith("data:image/"):
        return image_vector(item)
    return text_vector(str(item))


class FakeEmbedServer:
    def __init__(self):
        self.requests: list[dict] = []  # {"path", "headers", "body"}
        self.fail_status: int | None = None
        self.reject_images = False  # answer 400 to any request containing an image
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                server.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                status, payload = server.respond(self.path, body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def respond(self, path: str, body: dict) -> tuple[int, dict]:
        if self.fail_status:
            return self.fail_status, {"error": {"message": "fake failure"}}
        if path != "/v1/embeddings":
            return 404, {"error": {"message": "not found"}}
        if "messages" in body:
            parts = body["messages"][0]["content"]
            items = [p["image_url"]["url"] for p in parts if p.get("type") == "image_url"]
        else:
            items = body["input"] if isinstance(body["input"], list) else [body["input"]]
        has_image = any(
            (isinstance(i, dict) and "image" in i) or (isinstance(i, str) and i.startswith("data:image/"))
            for i in items
        )
        if self.reject_images and has_image:
            return 400, {"error": {"message": "images not supported"}}
        data = [{"object": "embedding", "index": n, "embedding": _item_vector(i)} for n, i in enumerate(items)]
        # Out of order on purpose: clients must sort by index.
        return 200, {"object": "list", "data": list(reversed(data)), "model": body.get("model")}

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
