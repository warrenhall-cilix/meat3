"""meat³ web UI: a read-only view of meat3d's sensors in a browser (servers, Proxmox hosts, ...).

    meat3-web [--socket /run/meat3/meat3.sock] [--listen 0.0.0.0] [--port 8733]
    meat3-web --gen-token        create the login token file (root)
    meat3-web --url              print the login link (needs read access to the token file)

Standard library only (no Qt). One poller thread reads `snapshot` from the daemon every poll and
keeps a short history for the graphs; any number of browsers share that one daemon connection.

Read-only by design: the server only sends the daemon's read requests (hello, snapshot,
update_status) and has no endpoint that changes anything. The systemd unit also runs it as a
dynamic user that the daemon doesn't grant write access to.

Prometheus: GET /metrics (Authorization: Bearer <token>, or "metrics_public": true in the config).

Login: a random token in /etc/meat3/web.token (root:meat3 0640). Opening /login?token=... (or
pasting it into the login form) sets an HttpOnly cookie. Settings in /etc/meat3/config.json "web".
"""
from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import hmac
import ipaddress
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
         "history_s": 900, "cert": "", "key": "", "metrics_public": False, "allowed_hosts": []}
    w.update(config.get().get("web") or {})
    return w


# ----------------------------------------------------------------------------- daemon side
def _num(x):
    """JSON-safe number: NaN/inf (which the daemon's JSON may carry) become null."""
    return x if not isinstance(x, float) or math.isfinite(x) else None


def installed_version() -> str:
    """git describe of the install (/opt/meat3/VERSION), read locally: asking the daemon's
    update_status would make it contact GitHub on the web UI's behalf."""
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                               "VERSION")) as f:
            return f.read().strip()
    except OSError:
        return ""


class Hub:
    """Polls the daemon and keeps the latest snapshot plus a value history per sensor."""

    STALE_POLLS = 10            # daemon answering but its timestamp frozen this many polls = not live

    def __init__(self, sock_path: str, history_s: int = 900, interval: float = 1.0):
        self.client = Client(sock_path)
        self.lock = threading.Lock()
        self.history_s = max(60, min(HISTORY_MAX_S, int(history_s)))
        self.interval = max(0.5, float(interval))
        self.hello: dict = {}
        self.version = installed_version()
        self.meta: dict = {"rev": "", "sensors": []}
        self.values: dict = {"connected": False, "error": "connecting…"}
        self.ts: collections.deque = collections.deque()
        self.hist: dict[str, collections.deque] = {}
        self._stop = threading.Event()
        self._last_hello = 0.0
        self._last_t, self._same_t = None, 0

    def start(self):
        threading.Thread(target=self._run, name="meat3-web-poll", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _disconnected(self, error: str):
        with self.lock:
            self.values = {**self.values, "connected": False, "error": error}

    def _run(self):
        last_err = None
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.poll_once()
                last_err = None
            except Exception as e:       # never let the poller die: /metrics would freeze as "up"
                self.client.close()
                msg = f"daemon: {e}" if isinstance(e, (OSError, ValueError)) else f"poll failed: {e!r}"
                if msg != last_err:
                    print(f"meat3-web: {msg}", file=sys.stderr, flush=True)
                    last_err = msg
                self._disconnected(msg)
            self._stop.wait(max(0.2, self.interval - (time.monotonic() - t0)))

    def poll_once(self):
        now = time.time()
        if now - self._last_hello > 60 or not self.hello:
            h = self.client.call({"cmd": "hello"})
            self.hello = {k: h.get(k) for k in ("version", "host", "backends")}
            self._last_hello = now
        snap = self.client.call({"cmd": "snapshot"})
        if not snap.get("ok"):
            raise ValueError(snap.get("msg") or "snapshot failed")
        t = snap.get("t") or now
        if t == self._last_t:
            self._same_t += 1
            if self._same_t >= self.STALE_POLLS:
                self._disconnected("daemon answers but stopped updating its sensors")
            return                        # nothing new: no duplicate history point
        self._last_t, self._same_t = t, 0
        sensors = snap.get("sensors") or []
        order = [s["id"] for s in sensors]
        by_id = {s["id"]: s for s in sensors}
        meta_rows = [{k: s.get(k) for k in META_KEYS} for s in sensors]
        rev = format(hash(json.dumps(meta_rows, sort_keys=True, default=str)) & 0xFFFFFFFF, "08x")
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
                self.meta = {"rev": rev, "sensors": meta_rows}
            self.values = {"connected": True, "error": "", "t": t, "rev": rev, "poll_ms": snap.get("poll_ms"),
                           "v": values, "overview": overview, **self.hello, "installed": self.version}
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


# ----------------------------------------------------------------------------- Prometheus
# unit -> (metric family, factor to the Prometheus base unit)
PROM_UNITS = {
    "°C": ("temperature_celsius", 1), "V": ("voltage_volts", 1), "mV": ("voltage_volts", 1e-3),
    "W": ("power_watts", 1), "A": ("current_amperes", 1), "mA": ("current_amperes", 1e-3),
    "MHz": ("frequency_hertz", 1e6), "GHz": ("frequency_hertz", 1e9), "RPM": ("fan_speed_rpm", 1),
    "%": ("percent", 1), "MB/s": ("throughput_bytes_per_second", 1e6), "MiB/s": ("throughput_bytes_per_second", 2 ** 20),
    "GB/s": ("throughput_bytes_per_second", 1e9), "MB": ("size_bytes", 1e6), "GB": ("size_bytes", 1e9),
    "TB": ("size_bytes", 1e12), "MiB": ("size_bytes", 2 ** 20), "GiB": ("size_bytes", 2 ** 30),
    "h": ("duration_seconds", 3600), "min": ("duration_seconds", 60), "s": ("duration_seconds", 1),
    "ms": ("duration_seconds", 1e-3), "us": ("duration_seconds", 1e-6), "J": ("energy_joules", 1),
    "Wh": ("energy_joules", 3600), "kWh": ("energy_joules", 3.6e6),
}
PROM_HELP = {"value": "meat3 sensor without a base unit (see the unit label)"}


def _plabel(v) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", " ").replace('"', '\\"')


def prometheus_text(meta: dict, values: dict) -> str:
    """Prometheus text exposition (v0.0.4) of every sensor: one family per base unit, labelled with
    the sensor id, device, group path, label and the original unit."""
    fams: dict[str, list[str]] = {}
    live = values.get("v", {}) if values.get("connected") else {}      # no stale values when the daemon is gone
    for m in meta.get("sensors", []):
        row = live.get(m["id"])
        if not row or row[0] is None:
            continue
        unit = m.get("unit") or ""
        fam, k = PROM_UNITS.get(unit, ("value", 1))
        grp = m.get("group") or ""
        dev, _, sub = grp.partition("/")
        lab = (f'id="{_plabel(m["id"])}",device="{_plabel(dev.strip())}",group="{_plabel(sub)}",'
               f'sensor="{_plabel(m.get("label", ""))}",unit="{_plabel(unit)}"')
        fams.setdefault(fam, []).append(f"meat3_{fam}{{{lab}}} {row[0] * k:.10g}")
    out = []
    up = 1 if values.get("connected") else 0
    out += ["# HELP meat3_up 1 when meat3-web is getting data from the meat3d daemon", "# TYPE meat3_up gauge",
            f"meat3_up {up}"]
    host, ver = values.get("host") or socket.gethostname(), values.get("installed") or ""
    out += ["# HELP meat3_info meat3 build and host", "# TYPE meat3_info gauge",
            f'meat3_info{{host="{_plabel(host)}",version="{_plabel(ver)}"}} 1']
    if up and values.get("poll_ms") is not None:
        out += ["# HELP meat3_poll_duration_seconds time the daemon took for its last sensor poll",
                "# TYPE meat3_poll_duration_seconds gauge", f"meat3_poll_duration_seconds {values['poll_ms'] / 1000:.6g}"]
    for fam in sorted(fams):
        out.append(f"# HELP meat3_{fam} {PROM_HELP.get(fam, 'meat3 sensors, ' + fam.replace('_', ' '))}")
        out.append(f"# TYPE meat3_{fam} gauge")
        out += fams[fam]
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------- HTTP
def session_value(token: str) -> str:
    """Cookie value: derived from the token, so a leaked cookie isn't the /metrics bearer secret.
    Changing the token (meat3-web --gen-token) logs every browser out."""
    return hmac.new(token.encode(), b"meat3-web session v1", hashlib.sha256).hexdigest()


def host_allowed(host_header: str, extra: tuple = ()) -> bool:
    """Without a login, only answer requests addressed to this machine (IP literal, localhost, its
    own hostname or a configured name): a DNS-rebinding page uses its own domain name."""
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        host = host[1:host.find("]")] if "]" in host else host[1:]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    me = socket.gethostname().lower()
    names = {"localhost", me, me.split(".", 1)[0], *(h.lower() for h in extra)}
    short = host.split(".", 1)[0]
    return host in names or (short in names and host.endswith((".local", ".lan", ".home", ".internal")))


def accepts_gzip(header: str) -> bool:
    for part in (header or "").lower().split(","):
        enc, _, params = part.strip().partition(";")
        if enc.strip() in ("gzip", "*"):
            q = params.strip()
            return not (q.startswith("q=") and q[2:].strip() in ("0", "0.0", "0.00", "0.000"))
    return False


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
    metrics_public = False
    allowed_hosts: tuple = ()
    timeout = 20                             # per-connection socket timeout: idle clients can't hold threads

    def log_message(self, fmt, *args):       # quiet: no per-request lines in the journal
        pass

    # -- helpers
    def _authed(self) -> bool:
        if self.token is None:
            return True
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip().encode(), self.token.encode()):
            return True                      # Prometheus: authorization.credentials(_file)
        want = session_value(self.token).encode()
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and hmac.compare_digest(v.encode(), want):
                return True
        return False

    def _send(self, code: int, body: bytes, ctype: str, extra: Optional[dict] = None, cache: bool = False):
        if len(body) > 1024 and accepts_gzip(self.headers.get("Accept-Encoding")):
            body = gzip.compress(body, 5)
            extra = {**(extra or {}), "Content-Encoding": "gzip"}
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=300" if cache else "no-store")
        self.send_header("Vary", "Accept-Encoding")
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
        self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", {"Location": "/", "Set-Cookie": f"{COOKIE}={session_value(self.token)}{attrs}"})

    # -- verbs
    def do_HEAD(self):
        self.do_GET()

    def _host_check(self, path: str) -> bool:
        """False (and a 421 sent) for a foreign Host when the request isn't protected by the token."""
        unprotected = self.token is None or (path == "/metrics" and self.metrics_public and not self._authed())
        if unprotected and not host_allowed(self.headers.get("Host", ""), self.allowed_hosts):
            self._send(421, b"unknown host name; add it to web.allowed_hosts in /etc/meat3/config.json\n",
                       "text/plain")
            return False
        return True

    def do_GET(self):
        url = urlsplit(self.path)
        path, q = url.path, parse_qs(url.query)
        if not self._host_check(path):
            return
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
        if path == "/metrics":
            if not (self.metrics_public or self._authed()):
                return self._send(401, b"unauthorized: send 'Authorization: Bearer <token>'\n", "text/plain",
                                  {"WWW-Authenticate": 'Bearer realm="meat3"'})
            with self.hub.lock:
                meta, values = self.hub.meta, self.hub.values
            return self._send(200, prometheus_text(meta, values).encode(), "text/plain; version=0.0.4; charset=utf-8")
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
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= 4096:
            return self._send(413 if n > 4096 else 400, b"bad request body", "text/plain", {"Connection": "close"})
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
    ssl_ctx: Optional[ssl.SSLContext] = None

    def __init__(self, addr, handler):
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        super().__init__(addr, handler)

    def finish_request(self, request, client_address):
        # TLS handshake here, in the connection's own thread with a timeout: wrapping the listening
        # socket would run it inside accept(), where one silent client blocks everyone
        if self.ssl_ctx is not None:
            request.settimeout(Handler.timeout)
            try:
                tls = self.ssl_ctx.wrap_socket(request, server_side=True)
            except (OSError, ssl.SSLError):
                return
            try:
                super().finish_request(tls, client_address)
            finally:
                tls.close()                  # shutdown_request() only closes the raw socket
            return
        super().finish_request(request, client_address)


def make_server(hub: Hub, listen: str, port: int, token: Optional[str], cert: str = "", key: str = "",
                metrics_public: bool = False, allowed_hosts=()) -> Server:
    handler = type("BoundHandler", (Handler,), {"hub": hub, "token": token, "secure": bool(cert),
                                                "metrics_public": metrics_public,
                                                "allowed_hosts": tuple(allowed_hosts or ())})
    srv = Server((listen, port), handler)
    if cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key or None)
        srv.ssl_ctx = ctx
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
        try:
            gen_token(args.token_file)
        except (OSError, ValueError) as e:
            print(f"meat3-web: {e} (delete the file to make a new token)", file=sys.stderr)
            return 1
        print(f"token in {args.token_file}")
        return 0
    if args.url:
        try:
            tok = read_token(args.token_file)
        except (OSError, ValueError) as e:
            print(f"meat3-web: can't read the token ({e}); try sudo", file=sys.stderr)
            return 1
        local = args.listen in ("127.0.0.1", "::1", "localhost")
        for ip in (["localhost"] if local else host_ips()):
            print(f"{scheme}://{ip}:{args.port}/login?token={tok}")
        return 0

    token = None
    if w.get("auth", True) and not args.no_auth:
        try:
            token = read_token(args.token_file)
        except (OSError, ValueError) as e:
            print(f"meat3-web: no login token ({e}). Run: sudo meat3-web --gen-token", file=sys.stderr)
            return 1
    try:
        interval = float(config.get().get("poll_interval_s") or 1.0)
    except (TypeError, ValueError):
        interval = 1.0
    hub = Hub(args.socket, w.get("history_s", 900), interval)
    hub.start()
    srv = make_server(hub, args.listen, args.port, token, w.get("cert", ""), w.get("key", ""),
                      bool(w.get("metrics_public", False)), w.get("allowed_hosts") or ())
    shown = f"[{args.listen}]" if ":" in args.listen else args.listen
    print(f"meat3-web: {scheme}://{shown}:{args.port}/ (read-only; "
          f"{'token login' if token else 'NO login'}; daemon {args.socket})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    hub.stop()
    return 0
