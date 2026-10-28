"""meat³ web UI: a read-only view of meat3d's sensors in a browser (servers, Proxmox hosts, ...).

    meat3-web [--socket /run/meat3/meat3.sock] [--listen 0.0.0.0] [--port 8733]
    meat3-web --gen-token        create the login token file (root)
    meat3-web --url              print the login link (needs read access to the token file)

Standard library only (no Qt). One poller thread reads `snapshot` from the daemon every poll and
keeps a short history for the graphs; any number of browsers share that one daemon connection.

Read-only by design: the server only sends the daemon's read requests (hello, snapshot,
update_status) and has no endpoint that changes anything. The systemd unit also runs it as a
dynamic user that the daemon doesn't grant write access to.

Login: a random token in /etc/meat3/web.token (root:meat3 0640). Opening /login?token=... (or
pasting it into the login form) sets an HttpOnly cookie. Settings in /etc/meat3/config.json "web".
"""
from __future__ import annotations

import argparse
import collections
import gzip
import hmac
import json
import math
import os
import secrets
import socket
import ssl
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from meat3.core import config
from meat3.tui import Client, overview_devices, overview_rows, severity

DEFAULT_SOCKET = "/run/meat3/meat3.sock"
COOKIE = "meat3_web"
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
ICON = os.path.join(os.path.dirname(os.path.dirname(__file__)), "gui", "assets", "meat3.svg")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8")}
META_KEYS = ("id", "group", "label", "unit", "decimals", "hint")
HISTORY_MAX_S = 3600


def web_config() -> dict:
    w = {"listen": "0.0.0.0", "port": 8733, "auth": True, "token_file": "/etc/meat3/web.token",
         "history_s": 900, "cert": "", "key": ""}
    w.update(config.get().get("web") or {})
    return w


# ----------------------------------------------------------------------------- daemon side
def _num(x):
    """JSON-safe number: NaN/inf (which the daemon's JSON may carry) become null."""
    return x if not isinstance(x, float) or math.isfinite(x) else None


class Hub:
    """Polls the daemon and keeps the latest snapshot plus a value history per sensor."""

    def __init__(self, sock_path: str, history_s: int = 900):
        self.client = Client(sock_path)
        self.lock = threading.Lock()
        self.history_s = max(60, min(HISTORY_MAX_S, int(history_s)))
        self.interval = 1.0
        self.hello: dict = {}
        self.update: dict = {}
        self.meta: dict = {"rev": "", "sensors": []}
        self.values: dict = {"connected": False, "error": "connecting…"}
        self.ts: collections.deque = collections.deque()
        self.hist: dict[str, collections.deque] = {}
        self._stop = threading.Event()
        self._last_hello = self._last_update = 0.0

    def start(self):
        threading.Thread(target=self._run, name="meat3-web-poll", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.poll_once()
            except (OSError, ValueError) as e:
                self.client.close()
                with self.lock:
                    self.values = {**self.values, "connected": False, "error": f"daemon: {e}"}
            self._stop.wait(max(0.2, self.interval - (time.monotonic() - t0)))

    def poll_once(self):
        now = time.time()
        if now - self._last_hello > 60 or not self.hello:
            h = self.client.call({"cmd": "hello"})
            self.hello = {k: h.get(k) for k in ("version", "host", "backends")}
            self._last_hello = now
        if now - self._last_update > 60:
            u = self.client.call({"cmd": "update_status"})
            self.update = {"installed": u.get("installed"), "behind": u.get("behind")}
            self._last_update = now
        snap = self.client.call({"cmd": "snapshot"})
        if not snap.get("ok"):
            raise ValueError(snap.get("msg") or "snapshot failed")
        sensors = snap.get("sensors") or []
        order = [s["id"] for s in sensors]
        by_id = {s["id"]: s for s in sensors}
        rev = format(hash(tuple(order)) & 0xFFFFFFFF, "08x")
        poll_ms = snap.get("poll_ms")
        self.interval = max(0.5, float(config.get().get("poll_interval_s", 1.0)))
        overview = []
        for p, title in overview_devices(order, by_id):
            rows = []
            for label, s, mx in overview_rows(p, order, by_id):
                if label in ("pins", "cores"):
                    rows.append({"kind": label, "ids": [x["id"] if x else None for x in s]})
                else:
                    rows.append({"label": label, "id": s["id"], "max": _num(mx)})
            if rows:
                overview.append({"title": title, "rows": rows})
        values = {s["id"]: [_num(s.get("value")), _num(s.get("min")), _num(s.get("max")), _num(s.get("avg")),
                            severity(s)] for s in sensors}
        with self.lock:
            if rev != self.meta["rev"]:
                self.meta = {"rev": rev, "sensors": [{k: s.get(k) for k in META_KEYS} for s in sensors]}
            self.values = {"connected": True, "error": "", "t": snap.get("t") or now, "rev": rev,
                           "poll_ms": poll_ms, "v": values, "overview": overview, **self.hello,
                           "update": self.update}
            t = snap.get("t") or now
            self.ts.append(t)
            n = len(self.ts)
            for sid, row in values.items():
                d = self.hist.get(sid)
                if d is None:
                    d = self.hist[sid] = collections.deque([None] * (n - 1))
                d.append(row[0])
            for sid, d in self.hist.items():
                if len(d) < n:
                    d.append(None)
            while self.ts and self.ts[0] < t - self.history_s:
                self.ts.popleft()
                for d in self.hist.values():
                    d.popleft()
            for sid in [k for k, d in self.hist.items()        # sensors that went away for good
                        if k not in values and not any(x is not None for x in d)]:
                del self.hist[sid]

    def history(self, ids: list[str], seconds: float) -> dict:
        with self.lock:
            ts = list(self.ts)
            start = 0
            if ts:
                cut = ts[-1] - seconds
                while start < len(ts) and ts[start] < cut:
                    start += 1
            return {"t": ts[start:],
                    "series": {i: list(self.hist[i])[start:] for i in ids if i in self.hist}}


# ----------------------------------------------------------------------------- auth
def read_token(path: str) -> str:
    with open(path) as f:
        tok = f.read().strip()
    if len(tok) < 16:
        raise ValueError(f"{path}: token too short")
    return tok


def gen_token(path: str) -> str:
    """Create the token file if it doesn't exist (root:meat3 0640 when run as root)."""
    if os.path.exists(path):
        return read_token(path)
    tok = secrets.token_urlsafe(24)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    if os.geteuid() == 0:
        try:
            import grp
            os.chown(path, 0, grp.getgrnam("meat3").gr_gid)
        except KeyError:
            os.chmod(path, 0o600)
    return tok


def host_ips() -> list[str]:
    """Best guess at the LAN address(es) to show in the login link."""
    out = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))          # no packet is sent; picks the default-route address
        out.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return out or [socket.gethostname()]


# ----------------------------------------------------------------------------- HTTP
LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>meat³ login</title>
<link rel="icon" href="/icon.svg"><link rel="stylesheet" href="/app.css"></head>
<body class="login"><form method="post" action="/login" class="card">
<h1><img src="/icon.svg" alt="" width="28" height="28"> meat³</h1>
<p>Paste the token from <code>/etc/meat3/web.token</code> on {host},<br>or run <code>meat3-web --url</code> there.</p>
<input name="token" type="password" autocomplete="current-password" placeholder="token" autofocus required>
<button type="submit">Log in</button>{err}</form></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "meat3-web"
    sys_version = ""
    hub: Hub = None
    token: Optional[str] = None
    secure = False

    def log_message(self, fmt, *args):       # quiet: no per-request lines in the journal
        pass

    # -- helpers
    def _authed(self) -> bool:
        if self.token is None:
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and hmac.compare_digest(v.encode(), self.token.encode()):
                return True
        return False

    def _send(self, code: int, body: bytes, ctype: str, extra: Optional[dict] = None, cache: bool = False):
        if len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, 5)
            extra = {**(extra or {}), "Content-Encoding": "gzip"}
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=300" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                         "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, separators=(",", ":"), allow_nan=False, default=str).encode(),
                   "application/json")

    def _login_page(self, err: str = "", code: int = 200):
        host = (self.hub.hello.get("host") if self.hub else None) or socket.gethostname()
        page = LOGIN_PAGE.format(host=_esc(host), err=f'<p class="err">{_esc(err)}</p>' if err else "")
        self._send(code, page.encode(), "text/html; charset=utf-8")

    def _login_ok(self, given: str) -> bool:
        return self.token is not None and hmac.compare_digest(given.strip().encode(), self.token.encode())

    def _set_cookie_and_go_home(self):
        attrs = "; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000" + ("; Secure" if self.secure else "")
        self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", {"Location": "/", "Set-Cookie": f"{COOKIE}={self.token}{attrs}"})

    # -- verbs
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlsplit(self.path)
        path, q = url.path, parse_qs(url.query)
        if path == "/icon.svg":
            try:
                with open(ICON, "rb") as f:
                    return self._send(200, f.read(), "image/svg+xml", cache=True)
            except OSError:
                return self._send(404, b"", "text/plain")
        if path == "/app.css":
            return self._static(path)
        if path == "/login":
            if self.token is None:
                return self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", {"Location": "/"})
            given = (q.get("token") or [""])[0]
            if given and self._login_ok(given):
                return self._set_cookie_and_go_home()
            if given:
                time.sleep(0.5)
                return self._login_page("Wrong token.", 403)
            return self._login_page()
        if path == "/logout":
            return self._send(HTTPStatus.SEE_OTHER, b"", "text/plain",
                              {"Location": "/login", "Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0"})
        if not self._authed():
            if path.startswith("/api/"):
                return self._json({"ok": False, "msg": "login required"}, 401)
            return self._login_page(code=200 if path == "/" else 401)
        if path in STATIC:
            return self._static(path)
        if path == "/api/meta":
            with self.hub.lock:
                meta = self.hub.meta
            return self._json({"ok": True, **meta, "auth": self.token is not None,
                               "history_s": self.hub.history_s})
        if path == "/api/values":
            with self.hub.lock:
                v = self.hub.values
            return self._json({"ok": True, **v})
        if path == "/api/history":
            ids = [i for i in (q.get("ids") or [""])[0].split(",") if i][:64]
            try:
                secs = float((q.get("s") or ["600"])[0])
            except ValueError:
                secs = 600.0
            return self._json({"ok": True, **self.hub.history(ids, max(10.0, min(secs, HISTORY_MAX_S)))})
        return self._send(404, b"not found", "text/plain")

    def do_POST(self):
        # the only POST is the login form; nothing in the web UI changes the machine
        if urlsplit(self.path).path != "/login" or self.token is None:
            return self._send(405, b"read-only", "text/plain", {"Allow": "GET, HEAD"})
        n = min(int(self.headers.get("Content-Length") or 0), 4096)
        form = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
        if self._login_ok((form.get("token") or [""])[0]):
            return self._set_cookie_and_go_home()
        time.sleep(0.5)
        return self._login_page("Wrong token.", 403)

    def _static(self, path: str):
        name, ctype = STATIC[path]
        try:
            with open(os.path.join(STATIC_DIR, name), "rb") as f:
                body = f.read()
        except OSError:
            return self._send(404, b"", "text/plain")
        self._send(200, body, ctype)


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler):
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        super().__init__(addr, handler)


def make_server(hub: Hub, listen: str, port: int, token: Optional[str],
                cert: str = "", key: str = "") -> Server:
    handler = type("BoundHandler", (Handler,), {"hub": hub, "token": token, "secure": bool(cert)})
    srv = Server((listen, port), handler)
    if cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key or None)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    return srv


def main(argv=None) -> int:
    w = web_config()
    ap = argparse.ArgumentParser(prog="meat3-web", description="meat³ read-only web UI")
    ap.add_argument("--socket", default=os.environ.get("MEAT3_SOCKET") or DEFAULT_SOCKET)
    ap.add_argument("--listen", default=w["listen"])
    ap.add_argument("--port", type=int, default=int(w["port"]))
    ap.add_argument("--token-file", default=w["token_file"])
    ap.add_argument("--no-auth", action="store_true", help="no login (only behind your own auth proxy / on localhost)")
    ap.add_argument("--gen-token", action="store_true", help="create the token file and exit")
    ap.add_argument("--url", action="store_true", help="print the login link and exit")
    args = ap.parse_args(argv)
    scheme = "https" if w.get("cert") else "http"

    if args.gen_token:
        gen_token(args.token_file)
        print(f"token in {args.token_file}")
        return 0
    if args.url:
        try:
            tok = read_token(args.token_file)
        except (OSError, ValueError) as e:
            print(f"meat3-web: can't read the token ({e}); try sudo", file=sys.stderr)
            return 1
        for ip in host_ips():
            print(f"{scheme}://{ip}:{args.port}/login?token={tok}")
        return 0

    token = None
    if w.get("auth", True) and not args.no_auth:
        try:
            token = read_token(args.token_file)
        except (OSError, ValueError) as e:
            print(f"meat3-web: no login token ({e}). Run: sudo meat3-web --gen-token", file=sys.stderr)
            return 1
    hub = Hub(args.socket, w.get("history_s", 900))
    hub.start()
    srv = make_server(hub, args.listen, args.port, token, w.get("cert", ""), w.get("key", ""))
    print(f"meat3-web: {scheme}://{args.listen}:{args.port}/ (read-only; "
          f"{'token login' if token else 'NO login'}; daemon {args.socket})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    hub.stop()
    return 0
