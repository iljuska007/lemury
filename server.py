#!/usr/bin/env python3
"""Сервер «Взрывных лемуров»: отдаёт страницу игры и хранит состояние партий в памяти.

Логика игры живёт в браузере. Сервер только хранит документ партии с номером
версии: запись проходит, только если клиент видел последнюю версию (иначе 409).
"""
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", "8765"))
ROOT = os.path.dirname(os.path.abspath(__file__))
CODE_RE = re.compile(r"^[A-Z]{4}$")
# Files the server hands out as-is: path -> (file, content type, caching)
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8", "no-store"),
    "/index.html": ("index.html", "text/html; charset=utf-8", "no-store"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json", "max-age=3600"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8", "no-store"),
    "/icon-192.png": ("icon-192.png", "image/png", "max-age=86400"),
    "/icon-512.png": ("icon-512.png", "image/png", "max-age=86400"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png", "max-age=86400"),
}
CARDS_DIR = os.path.join(ROOT, "cards")
CARD_TYPES = {"hole", "shield", "radar", "warp", "beam", "shuffle", "board", "worm", "jam", "reverse", "drift"}
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}


def card_art():
    """Картинки из папки cards/: файл с именем типа карты (hole.jpg, shield.png, ...)."""
    art = {}
    if os.path.isdir(CARDS_DIR):
        for name in sorted(os.listdir(CARDS_DIR)):
            base, ext = os.path.splitext(name)
            if base.lower() in CARD_TYPES and ext.lower() in IMAGE_TYPES:
                art[base.lower()] = "/cards/" + name
    return art
MAX_BODY = 512 * 1024
MAX_GAMES = 300
GAME_TTL = 12 * 3600

games = {}  # code -> {"rev": int, "state": dict, "touched": float}
cond = threading.Condition()


def now_ms():
    return int(time.time() * 1000)


def prune():
    cutoff = time.time() - GAME_TTL
    for code in [c for c, g in games.items() if g["touched"] < cutoff]:
        del games[code]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path in STATIC:
            name, ctype, cache = STATIC[url.path]
            with open(os.path.join(ROOT, name), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/game":
            code = q.get("code", [""])[0]
            if not CODE_RE.match(code):
                return self.send_json(400, {"error": "bad code"})
            since = int(q.get("since", ["-1"])[0] or -1)
            with cond:
                # Long-poll: wait until the game changes from the version the client already has.
                cond.wait_for(lambda: games.get(code, {"rev": 0})["rev"] != since, timeout=25)
                g = games.get(code)
                rev, state = (g["rev"], g["state"]) if g else (0, None)
            self.send_json(200, {"rev": rev, "state": state, "now": now_ms()})
        elif url.path == "/api/art":
            self.send_json(200, {"art": card_art(), "now": now_ms()})
        elif url.path.startswith("/cards/"):
            name = os.path.basename(url.path)
            ext = os.path.splitext(name)[1].lower()
            path = os.path.join(CARDS_DIR, name)
            if "/cards/" + name not in card_art().values() or not os.path.isfile(path):
                return self.send_json(404, {"error": "not found"})
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", IMAGE_TYPES[ext])
            self.send_header("Cache-Control", "max-age=300")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/api/save":
            return self.send_json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self.send_json(413, {"error": "too large"})
        try:
            data = json.loads(self.rfile.read(length))
            code, rev, state = data["code"], int(data["rev"]), data["state"]
            assert CODE_RE.match(code) and isinstance(state, dict)
        except Exception:
            return self.send_json(400, {"error": "bad request"})
        with cond:
            g = games.get(code)
            current = g["rev"] if g else 0
            if rev != current:
                return self.send_json(409, {"rev": current, "state": g["state"] if g else None, "now": now_ms()})
            if not g and len(games) >= MAX_GAMES:
                prune()
                if len(games) >= MAX_GAMES:
                    return self.send_json(503, {"error": "server full"})
            games[code] = {"rev": current + 1, "state": state, "touched": time.time()}
            cond.notify_all()
        self.send_json(200, {"rev": current + 1, "now": now_ms()})


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # A player closing or reloading the page drops the connection mid-reply; that's normal.
        import sys
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


if __name__ == "__main__":
    server = QuietServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"Сервер игры запущен: http://localhost:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
