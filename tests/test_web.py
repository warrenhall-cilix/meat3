"""Web UI: read-only server against the mock daemon (login, API, history, no write endpoints)."""
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from meat3.web import server as web  # noqa: E402

TOKEN = "test-token-0123456789abcdef"
SENT: list = []                       # every daemon command the web server sends during these tests
_orig_call = web.Client.call


def _recording_call(self, req):
    SENT.append(req.get("cmd"))
    return _orig_call(self, req)


def wait_for(cond, timeout=15, msg="timed out"):
    t0 = time.monotonic()
    while not cond():
        assert time.monotonic() - t0 < timeout, msg
        time.sleep(0.05)


class TestWeb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="meat3-web-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls.sock = sock = str(cls.tmp / "mock.sock")
        cls.mock = subprocess.Popen([sys.executable, "-m", "meat3.core.mockd", "--socket", sock,
                                     "--osd-file", str(cls.tmp / "osd.txt"), "--rate", "0.5"],
                                    cwd=str(ROOT), stdout=subprocess.DEVNULL)
        cls.addClassCleanup(lambda: (cls.mock.terminate(), cls.mock.wait(5)))
        wait_for(lambda: os.path.exists(sock), 10, "mockd did not start")
        web.Client.call = _recording_call
        cls.addClassCleanup(setattr, web.Client, "call", _orig_call)
        cls.hub = web.Hub(sock, 120, interval=0.3)
        cls.hub.start()
        cls.addClassCleanup(lambda: (cls.hub.stop(), cls.hub.client.close()))
        cls.srv = web.make_server(cls.hub, "127.0.0.1", 0, TOKEN)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.addClassCleanup(lambda: (cls.srv.shutdown(), cls.srv.server_close()))
        wait_for(lambda: len(cls.hub.ts) >= 3, 15, "web hub got no snapshots")

    def req(self, method, path, cookie=True, body=None, headers=None, port=None):
        c = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=5)
        h = dict(headers or {})
        if cookie:
            h["Cookie"] = f"{web.COOKIE}={web.session_value(TOKEN)}"
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def test_login_required(self):
        r, _ = self.req("GET", "/api/values", cookie=False)
        self.assertEqual(r.status, 401)
        r, body = self.req("GET", "/", cookie=False)
        self.assertEqual(r.status, 200)
        self.assertIn(b'action="/login"', body)
        r, _ = self.req("GET", "/app.js", cookie=False)
        self.assertEqual(r.status, 401)
        r, _ = self.req("GET", "/api/values", cookie=False, headers={"Cookie": f"{web.COOKIE}=wrong"})
        self.assertEqual(r.status, 401)
        r, _ = self.req("GET", "/api/values", cookie=False, headers={"Cookie": f"{web.COOKIE}={TOKEN}"})
        self.assertEqual(r.status, 401)                   # the raw token is not a session cookie

    def test_login_sets_cookie(self):
        r, _ = self.req("GET", f"/login?token={TOKEN}", cookie=False)
        self.assertEqual(r.status, 303)
        ck = r.getheader("Set-Cookie")
        self.assertIn(f"{web.COOKIE}={web.session_value(TOKEN)}", ck)
        self.assertNotIn(TOKEN, ck)                       # cookie isn't the bearer secret
        self.assertIn("HttpOnly", ck)
        self.assertIn("SameSite=Strict", ck)
        r, _ = self.req("POST", "/login", cookie=False, body=f"token={TOKEN}",
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(r.status, 303)
        r, body = self.req("POST", "/login", cookie=False, body="token=nope",
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(r.status, 403)
        self.assertIn(b"Wrong token", body)

    def test_read_only(self):
        for path in ("/api/apply", "/api/values", "/api/osd", "/api/update_start"):
            r, _ = self.req("POST", path, body="{}")
            self.assertEqual(r.status, 405, path)
        for path in ("/", "/api/meta", "/api/values", "/api/history?ids=nv0.clk.gpc", "/metrics"):
            self.req("GET", path, headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertTrue(SENT)
        self.assertEqual(set(SENT), {"hello", "snapshot"})   # only reads; not even update_status

    def test_bad_request_bodies(self):
        for cl, code in (("-1", 400), ("abc", 400), ("99999", 413)):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            c.putrequest("POST", "/login")
            c.putheader("Content-Length", cl)
            c.endheaders()
            r = c.getresponse()
            self.assertEqual(r.status, code, cl)
            c.close()

    def test_host_check_without_login(self):
        srv = web.make_server(self.hub, "127.0.0.1", 0, None)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            port = srv.server_address[1]
            r, _ = self.req("GET", "/api/values", cookie=False, port=port, headers={"Host": "evil.example:8733"})
            self.assertEqual(r.status, 421)                  # DNS rebinding name
            for host in (f"127.0.0.1:{port}", "localhost", "[::1]:8733", web.socket.gethostname()):
                r, _ = self.req("GET", "/api/values", cookie=False, port=port, headers={"Host": host})
                self.assertEqual(r.status, 200, host)
        finally:
            srv.shutdown()
            srv.server_close()
        me = web.socket.gethostname().split(".")[0]
        self.assertTrue(web.host_allowed(f"{me}.lan:8733"))
        self.assertFalse(web.host_allowed("evil.lan"))
        self.assertTrue(web.host_allowed("grafana.home.arpa", ("grafana.home.arpa",)))

    def test_accepts_gzip(self):
        self.assertTrue(web.accepts_gzip("gzip, deflate, br"))
        self.assertFalse(web.accepts_gzip("gzip;q=0, deflate"))
        self.assertFalse(web.accepts_gzip(""))

    def test_tls_idle_client_does_not_block(self):
        import ssl
        if not shutil.which("openssl"):
            self.skipTest("openssl not installed")
        cert, key = str(self.tmp / "c.pem"), str(self.tmp / "k.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                        "-days", "1", "-subj", "/CN=localhost"], check=True, capture_output=True)
        srv = web.make_server(self.hub, "127.0.0.1", 0, TOKEN, cert, key)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        idle = web.socket.create_connection(("127.0.0.1", srv.server_address[1]))   # says nothing
        try:
            ctx = ssl._create_unverified_context()
            c = http.client.HTTPSConnection("127.0.0.1", srv.server_address[1], timeout=4, context=ctx)
            c.request("GET", "/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
            self.assertEqual(c.getresponse().status, 200)
            c.close()
        finally:
            idle.close()
            srv.shutdown()
            srv.server_close()

    def test_poller_survives_errors_and_detects_stale_daemon(self):
        hub = web.Hub(self.sock, 120, interval=0.5)
        boom = {"n": 0}

        def bad_poll():
            boom["n"] += 1
            raise TypeError("unexpected")
        hub.poll_once = bad_poll
        hub.start()
        try:
            wait_for(lambda: boom["n"] >= 2, 5, "poller died after the first error")
            self.assertFalse(hub.values["connected"])
            self.assertIn("TypeError", hub.values["error"])
        finally:
            hub.stop()
            hub.client.close()
        hub = web.Hub(self.sock, 120)
        frozen = {"ok": True, "t": 123.0, "sensors": [{"id": "a", "value": 1.0}]}
        hub.client.call = lambda req: {"ok": True} if req["cmd"] == "hello" else frozen
        hub.poll_once()
        self.assertTrue(hub.values["connected"])
        for _ in range(hub.STALE_POLLS):
            hub.poll_once()
        self.assertFalse(hub.values["connected"])
        self.assertEqual(len(hub.ts), 1)                    # no duplicate history points

    def test_meta_values_history(self):
        r, body = self.req("GET", "/api/meta")
        self.assertEqual(r.status, 200)
        meta = json.loads(body)
        ids = [s["id"] for s in meta["sensors"]]
        self.assertIn("nv0.clk.gpc", ids)
        self.assertEqual(set(meta["sensors"][0]), set(web.META_KEYS))
        r, body = self.req("GET", "/api/values")
        v = json.loads(body)
        self.assertTrue(v["connected"])
        self.assertEqual(v["rev"], meta["rev"])
        row = v["v"]["nv0.clk.gpc"]
        self.assertEqual(len(row), 5)                      # value, min, max, avg, severity
        self.assertIsInstance(row[0], (int, float))
        titles = [p["title"] for p in v["overview"]]
        self.assertTrue(any("RTX 5080" in t for t in titles), titles)
        gpu = next(p for p in v["overview"] if "RTX 5080" in p["title"])
        self.assertTrue(any(r.get("id") == "nv0.clk.gpc" for r in gpu["rows"]), gpu)
        self.assertTrue(v["host"])
        r, body = self.req("GET", "/api/history?ids=nv0.clk.gpc,nope&s=60")
        h = json.loads(body)
        self.assertGreaterEqual(len(h["t"]), 3)
        self.assertEqual(len(h["series"]["nv0.clk.gpc"]), len(h["t"]))
        self.assertNotIn("nope", h["series"])

    def test_static_and_headers(self):
        for path, ctype in (("/", "text/html"), ("/app.js", "text/javascript"), ("/app.css", "text/css"),
                            ("/icon.svg", "image/svg+xml")):
            r, body = self.req("GET", path)
            self.assertEqual(r.status, 200, path)
            self.assertTrue(r.getheader("Content-Type").startswith(ctype), path)
            self.assertIn("frame-ancestors 'none'", r.getheader("Content-Security-Policy"))
        r, body = self.req("GET", "/api/meta", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(r.getheader("Content-Encoding"), "gzip")

    def test_metrics(self):
        r, _ = self.req("GET", "/metrics", cookie=False)
        self.assertEqual(r.status, 401)
        self.assertIn("Bearer", r.getheader("WWW-Authenticate"))
        r, _ = self.req("GET", "/metrics", cookie=False, headers={"Authorization": "Bearer nope"})
        self.assertEqual(r.status, 401)
        r, body = self.req("GET", "/metrics", cookie=False, headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(r.status, 200)
        self.assertTrue(r.getheader("Content-Type").startswith("text/plain; version=0.0.4"))
        text = body.decode()
        self.assertIn("meat3_up 1", text)
        line = next(l for l in text.splitlines() if 'id="nv0.clk.gpc"' in l)
        self.assertTrue(line.startswith("meat3_frequency_hertz{"), line)
        self.assertGreater(float(line.rsplit(" ", 1)[1]), 1e8)          # MHz -> Hz
        self.assertIn('device="GPU 0: NVIDIA GeForce RTX 5080"', line)
        import re
        for l in text.splitlines():                                     # exposition format sanity
            if not l.startswith("#"):
                self.assertRegex(l, r'^meat3_[a-z_]+(\{([a-z]+="([^"\\]|\\.)*",?)+\})? -?[0-9.e+-]+$')
        types = [l.split()[2] for l in text.splitlines() if l.startswith("# TYPE")]
        self.assertEqual(len(types), len(set(types)))                   # each family declared once

    def test_metrics_stale_when_disconnected(self):
        txt = web.prometheus_text({"sensors": [{"id": "a", "group": "G", "label": "l", "unit": "W"}]},
                                  {"connected": False, "v": {"a": [5, 0, 0, 0, 0]}})
        self.assertIn("meat3_up 0", txt)
        self.assertNotIn('id="a"', txt)

    def test_nan_is_null(self):
        self.assertIsNone(web._num(float("nan")))
        self.assertIsNone(web._num(float("inf")))
        self.assertEqual(web._num(1.5), 1.5)

    def test_token_file(self):
        p = str(self.tmp / "tok" / "web.token")
        a = web.gen_token(p)
        self.assertGreaterEqual(len(a), 24)
        self.assertEqual(web.gen_token(p), a)              # kept, not regenerated
        self.assertEqual(os.stat(p).st_mode & 0o007, 0)     # never world-readable
        self.assertEqual(web.read_token(p), a)


if __name__ == "__main__":
    unittest.main()
