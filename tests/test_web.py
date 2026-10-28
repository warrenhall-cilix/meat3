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


class TestWeb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="meat3-web-"))
        sock = str(cls.tmp / "mock.sock")
        cls.mock = subprocess.Popen([sys.executable, "-m", "meat3.core.mockd", "--socket", sock,
                                     "--osd-file", str(cls.tmp / "osd.txt"), "--rate", "0.5"],
                                    cwd=str(ROOT), stdout=subprocess.DEVNULL)
        t0 = time.monotonic()
        while not os.path.exists(sock):
            assert time.monotonic() - t0 < 10, "mockd did not start"
            time.sleep(0.05)
        cls.hub = web.Hub(sock, 120)
        cls.hub.interval = 0.3
        cls.hub.start()
        cls.srv = web.make_server(cls.hub, "127.0.0.1", 0, TOKEN)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        t0 = time.monotonic()
        while len(cls.hub.ts) < 3:
            assert time.monotonic() - t0 < 15, "web hub got no snapshots"
            time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.hub.stop()
        cls.mock.terminate()
        cls.mock.wait(5)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, method, path, cookie=True, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = dict(headers or {})
        if cookie:
            h["Cookie"] = f"{web.COOKIE}={TOKEN}"
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

    def test_login_sets_cookie(self):
        r, _ = self.req("GET", f"/login?token={TOKEN}", cookie=False)
        self.assertEqual(r.status, 303)
        ck = r.getheader("Set-Cookie")
        self.assertIn(f"{web.COOKIE}={TOKEN}", ck)
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
        src = Path(web.__file__).read_text()
        for cmd in ("apply", "osd_set", "osd_toggle", "reset_stats", "update_start", "update_check"):
            self.assertNotIn(f'"cmd": "{cmd}"', src, cmd)

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
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o640)
        self.assertEqual(web.read_token(p), a)


if __name__ == "__main__":
    unittest.main()
