"""Local mock HTTP server for deterministic security/failure/network tests.

Threaded stdlib http.server. Tracks per-path request counts (in-process) so
tests can assert retries, resume behaviour and header conventions. All routes
serve deterministic bytes generated with Pillow at import time.
"""

from __future__ import annotations

import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw


def make_png(width: int = 100, height: int = 80, color=(120, 40, 200)) -> bytes:
    im = Image.new("RGB", (width, height), color)
    d = ImageDraw.Draw(im)
    d.ellipse((10, 10, width - 10, height - 10), fill=(255, 210, 0))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def make_jpeg(width: int = 120, height: int = 90) -> bytes:
    im = Image.new("RGB", (width, height), (30, 120, 240))
    d = ImageDraw.Draw(im)
    d.rectangle((20, 20, width - 20, height - 20), outline=(255, 255, 255), width=3)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def make_webp() -> bytes:
    im = Image.new("RGB", (90, 70), (10, 200, 120))
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=85)
    return buf.getvalue()


def make_gif() -> bytes:
    im = Image.new("P", (64, 48))
    buf = io.BytesIO()
    im.save(buf, "GIF")
    return buf.getvalue()


class MockServer:
    """Routes (all under one port):

    /api/search            -> JSON {items:[...]} driven by ?q= (echo fixture)
    /img/png/<n>           -> valid PNG (per-n deterministic size)
    /img/jpeg              -> valid JPEG
    /img/webp              -> valid WebP
    /img/gif               -> valid GIF
    /img/html-as-image     -> Content-Type: image/png but body is HTML
    /img/corrupt           -> PNG header + garbage (Pillow must reject)
    /img/empty             -> 200, zero bytes
    /img/big               -> 3 MiB of bytes (oversize test; client limit lower)
    /img/slow              -> sleeps 2s then serves PNG (timeout test)
    /flaky/<n>             -> 500 for first <n> hits, then valid PNG (retry test)
    /always500             -> 500 every time
    /always404             -> 404 every time
    /redirect/meta         -> 302 -> http://169.254.169.254/ (SSRF: must block)
    /redirect/loop         -> 302 -> itself (must hit redirect cap)
    /redirect/img          -> 302 -> /img/png/1 (allowed hop)
    /page/og               -> HTML page with og:image / JSON-LD / <img>
    /headers               -> echoes headers+query as JSON (UA test)
    """

    def __init__(self):
        self.counts: dict[str, int] = {}
        self._lock = threading.Lock()
        self._pngs = {n: make_png(80 + n * 10, 60 + n * 5, (n * 37 % 255, 90, 160)) for n in range(1, 9)}
        self._jpeg = make_jpeg()
        self._webp = make_webp()
        self._gif = make_gif()
        self._html_page = self._page_html().encode()
        self._html_as_image = b"<html><body>not an image</body></html>"
        self._corrupt = b"\x89PNG\r\n\x1a\n" + b"\x00garbage" * 40

    def _page_html(self) -> str:
        return """<!doctype html><html><head>
<title>Mock Page — Isdal Woman archive</title>
<meta property="og:image" content="/img/jpeg">
<meta property="og:image:width" content="120">
<meta property="og:image:height" content="90">
<script type="application/ld+json">{"image":"img/png/2","@type":"Article"}</script>
</head><body>
<img src="/img/png/3"><img src="data:image/png;base64,AAAA"><img src="/img/png/3">
</body></html>"""

    def _bump(self, path: str) -> int:
        with self._lock:
            self.counts[path] = self.counts.get(path, 0) + 1
            return self.counts[path]

    # -- handler factory -------------------------------------------------------
    def handler(self):
        server = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"   # keep-alive: avoids CI connection churn

            def log_message(self, *a):  # silence
                pass

            def handle_one_request(self):
                try:
                    super().handle_one_request()
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

            def _send(self, code: int, body: bytes, ctype: str = "application/octet-stream",
                      headers: dict | None = None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path, _, query = self.path.partition("?")
                n = server._bump(path)
                params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)

                if path == "/api/search":
                    q = params.get("q", "")
                    if q == "empty":
                        self._send(200, b'{"items": []}', "application/json")
                        return
                    items = [
                        {"image": f"http://{self.headers.get('Host')}/img/png/{i}",
                         "thumb": f"http://{self.headers.get('Host')}/img/png/{i}",
                         "title": f"Isdal Woman photo {i} Bergen 1970" if i % 2 else
                                  f"Isdal Woman {i} historical archive Norway",
                         "page": f"http://{self.headers.get('Host')}/page/og",
                         "width": 800 + i * 100, "height": 600 + i * 50,
                         "author": "Unknown photographer", "license": "CC BY-SA 4.0",
                         "fmt": "PNG"}
                        for i in range(1, 6)
                    ]
                    self._send(200, json.dumps({"items": items}).encode(), "application/json")
                elif path.startswith("/img/png/"):
                    key = min(int(path.rsplit("/", 1)[1]), 8)
                    self._send(200, server._pngs[key], "image/png")
                elif path == "/img/jpeg":
                    self._send(200, server._jpeg, "image/jpeg")
                elif path == "/img/webp":
                    self._send(200, server._webp, "image/webp")
                elif path == "/img/gif":
                    self._send(200, server._gif, "image/gif")
                elif path == "/img/html-as-image":
                    self._send(200, server._html_as_image, "image/png")
                elif path == "/img/corrupt":
                    self._send(200, server._corrupt, "image/png")
                elif path == "/img/empty":
                    self._send(200, b"", "image/png")
                elif path == "/img/big":
                    self._send(200, b"x" * (3 * 1024 * 1024), "image/png")
                elif path == "/img/slow":
                    time.sleep(2.0)
                    self._send(200, server._pngs[1], "image/png")
                elif path.startswith("/flaky/"):
                    fail_n = int(path.rsplit("/", 1)[1])
                    if n <= fail_n:
                        self._send(500, b"boom", "text/plain")
                    else:
                        self._send(200, server._pngs[2], "image/png")
                elif path == "/always500":
                    self._send(500, b"boom", "text/plain")
                elif path == "/always404":
                    self._send(404, b"nope", "text/plain")
                elif path == "/redirect/meta":
                    self.send_response(302)
                    self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif path == "/redirect/loop":
                    self.send_response(302)
                    self.send_header("Location", "/redirect/loop")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif path == "/redirect/img":
                    self.send_response(302)
                    self.send_header("Location", "/img/png/1")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif path == "/page/og":
                    self._send(200, server._html_page, "text/html; charset=utf-8")
                elif path == "/headers":
                    payload = {"ua": self.headers.get("User-Agent", ""), "query": params,
                               "path": path}
                    self._send(200, json.dumps(payload).encode(), "application/json")
                elif path == "/notfound":
                    self._send(404, b"nope", "text/plain")
                else:
                    self._send(404, b"unknown route", "text/plain")

            do_POST = do_GET

        return H

    # -- lifecycle ----------------------------------------------------------------
    def start(self) -> MockServer:
        class Q(ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 32

        self._httpd = Q(("127.0.0.1", 0), self.handler())
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
