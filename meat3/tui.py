"""meat³ TUI: terminal client for meat3d (curses, stdlib only; works over SSH).

    meat3-tui [--socket PATH] [--interval 1.0]

Views: 1 Overview  2 Sensors  3 Cores  4 Overclock      (Tab / Shift-Tab to cycle)
Keys:  ↑↓ PgUp PgDn Home End move   / filter (Sensors)   Enter set control (Overclock)
       a pin / unpin a sensor on the Overview   g pin as graph, or switch value <-> graph
       x unpin   [ ] reorder (Overview)   o toggle OSD   r reset min/max/avg   q quit
Pinned sensors are kept in ~/.config/meat3/tui.json.
"""
from __future__ import annotations

import argparse
import collections
import curses
import json
import locale
import os
import re
import socket
import time
from typing import Optional

from meat3.core.fmt import NONE_TEXT, format_value

VIEWS = ["Overview", "Sensors", "Cores", "Overclock"]
BLOCKS = " ▏▎▍▌▋▊▉█"
VBLOCKS = " ▁▂▃▄▅▆▇█"
GRAPH_H = 3             # rows per pinned graph
HISTORY = 300           # samples kept per sensor for graphs (5 min at the default 1 s refresh)


def pins_path() -> str:
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "meat3", "tui.json")


def load_pins(path: str) -> list[dict]:
    """[{"id": sensor id, "graph": bool}, ...] from tui.json; [] if missing or unreadable."""
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    pins = d.get("pins") if isinstance(d, dict) else None
    return [{"id": p["id"], "graph": bool(p.get("graph"))} for p in pins or []
            if isinstance(p, dict) and isinstance(p.get("id"), str)]


def _load_cfg(path: str) -> dict:
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cfg(path: str, **fields) -> None:
    d = _load_cfg(path)
    d.update(fields)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, path)


def save_pins(path: str, pins: list[dict]) -> None:
    _save_cfg(path, pins=pins)


THEMES = ("dark", "light")


def load_theme(path: str) -> Optional[str]:
    t = _load_cfg(path).get("theme")
    return t if t in THEMES else None


def save_theme(path: str, theme: str) -> None:
    _save_cfg(path, theme=theme)


def detect_theme(env: Optional[dict] = None) -> str:
    """'light' or 'dark' for the terminal: MEAT3_TUI_THEME wins, then COLORFGBG (set by xterm, rxvt,
    Konsole and others as 'fg;bg' with 7/15 = light background), else dark."""
    env = os.environ if env is None else env
    t = (env.get("MEAT3_TUI_THEME") or "").strip().lower()
    if t in THEMES:
        return t
    fgbg = env.get("COLORFGBG", "")
    if ";" in fgbg:
        bg = fgbg.rsplit(";", 1)[1].strip()
        if bg in ("7", "15") or bg.isdigit() and 7 <= int(bg) <= 15 and bg != "8":
            return "light"
    return "dark"


def graph_rows(vals, width: int, height: int, lo: Optional[float] = None, hi: Optional[float] = None) -> list[str]:
    """Bar chart of the last `width` values, `height` rows tall, newest on the right (None = gap).
    lo/hi fix the scale; otherwise it fits the values shown."""
    vals = list(vals)[-width:] if width > 0 else []
    vals = [None] * (width - len(vals)) + vals
    nums = [v for v in vals if v is not None]
    if lo is None:
        lo = min(nums) if nums else 0.0
    if hi is None:
        hi = max(nums) if nums else 1.0
    if hi - lo < 1e-9:                        # flat line: draw it at half height
        pad = abs(lo) * 0.1 or 1.0
        lo, hi = lo - pad, hi + pad
    levels = height * 8
    lv = [None if v is None else max(1, min(levels, round((v - lo) / (hi - lo) * levels))) for v in vals]
    return ["".join(" " if l is None else VBLOCKS[max(0, min(8, l - (height - 1 - r) * 8))] for l in lv)
            for r in range(height)]

# ----------------------------------------------------------------------------- data


class Client:
    def __init__(self, path: str):
        self.path = path
        self.sock: Optional[socket.socket] = None
        self.f = None

    def call(self, req: dict) -> dict:
        if self.sock is None:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(self.path)
            self.sock, self.f = s, s.makefile("rw")
        try:
            self.f.write(json.dumps(req) + "\n")
            self.f.flush()
            line = self.f.readline()
            if not line:
                raise OSError("daemon closed the connection")
            return json.loads(line)
        except (OSError, ValueError):
            self.close()
            raise

    def close(self):
        for x in (self.f, self.sock):
            try:
                if x:
                    x.close()          # file first: otherwise its finaliser flushes into a dead socket
            except OSError:
                pass
        self.sock = self.f = None


TEMP_RULES = [("memory junction", 90, 100), ("gddr", 90, 100), ("hot spot", 90, 100), ("hotspot", 90, 100),
              ("junction", 90, 100), ("vrm", 90, 105), ("tctl", 85, 95), ("tdie", 85, 95), ("ccd", 85, 95),
              ("core", 85, 95), ("l3", 85, 95)]


def severity(s: dict, v=None) -> int:
    """0 normal, 1 warn, 2 crit (same rules as the GUI)."""
    v = s.get("value") if v is None else v
    if v is None:
        return 0
    label, sid, unit = s.get("label", "").lower(), s.get("id", ""), s.get("unit", "")
    if "throttl" in (label + sid + s.get("group", "").lower()) and v and "idle" not in label:
        return 2
    if unit == "A" and re.search(r"\.pin\d\.a$", sid):
        return 2 if v > 9 else 1 if v > 8 else 0
    if unit == "°C" and not any(w in label for w in ("limit", "threshold", "target", "delta", "max operating")):
        lo, hi = 80, 90
        for kw, a, r in TEMP_RULES:
            if kw in f"{label} {sid}":
                lo, hi = a, r
                break
        return 2 if v >= hi else 1 if v >= lo else 0
    return 0


def fv(s: Optional[dict], key: str = "value") -> str:
    if not s:
        return NONE_TEXT
    return format_value(s.get(key), s.get("decimals", 0), s.get("unit", ""))


# Overview headline rows per device kind: (sensor id regex after "<prefix>.", short label, bar max)
# bar max: number, "@<id suffix>" (another sensor's value), or None (no bar)
HEADLINES = {
    "nv": [(r"clk\.gpc$", "Core clock", "@clk.gpc_max"), (r"(u|util|load)\.gpu$", "Load", 100),
           (r"mem\.pct$", "VRAM used", 100), (r"clk\.mem_eff$", "Memory", None),
           (r"clk\.xbar$", "XBAR", None), (r"v\.nvvdd$", "NVVDD", None), (r"v\.msvdd$", "MSVDD", None),
           (r"t\.gpu$", "GPU temp", 100), (r"t\.hotspot$", "Hotspot", 110), (r"t\.mem$", "GDDR temp", 110),
           (r"pwr\.board_real$", "Power", "@pwr.limit_real"),
           (r"pins\.a$", "12V-2x6 total", 55.0), (r"pins\.max_a$", "Worst pin", 9.5),
           (r"fan\d\.pct$", "Fan", 100)],
    "amdgpu": [(r"gm\.(average|current)_gfxclk_frequency$", "GFX clock", None),
               (r"gm\.(average|current)_uclk_frequency$", "Memory clock", None),
               (r"hwmon\.in0$", "VDDGFX", None),
               (r"gm\.temperature_(edge|gfx)$", "Edge/GFX temp", 100),
               (r"gm\.temperature_hotspot$", "Hotspot", 110), (r"gm\.temperature_mem$", "Memory temp", 105),
               (r"gm\.(average|current)_socket_power$", "Power", "@hwmon.power1_cap"),
               (r"vrm\.real_power$", "Real power (VRM)", None), (r"vrm\.vgfx\.iout$", "GFX current", 330.0),
               (r"fm\.thr\.tdc_gfx$", "TDC-limited", 100), (r"fm\.thr\.vmax$", "Vmax-limited", 100),
               (r"gm\.average_gfx_activity$", "GFX load", 100), (r"smu14\.throttle\.count$", "Throttling", None),
               (r"hwmon\.fan1$", "Fan", None)],
    "cpu": [(r"clk\.eff_avg$", "Avg clock", "@clk.eff_max"), (r"clk\.eff_max$", "Peak clock", None),
            (r"util\.total$", "Usage", 100), (r"k10temp\.temp1$|temp\.tctl$", "Tctl", 95),
            (r"cores\.temp_max$", "Hottest core", 95), (r"pmt\.ppt$", "PPT", "@pmt.ppt_limit"),
            (r"rapl\.intel_rapl_0$", "Package", None), (r"pmt\.vid$", "Core VID", None),
            (r"pmt\.fclk$", "FCLK", None), (r"pmt\.uclk$", "UCLK", None), (r"pmt\.mclk$", "MCLK", None),
            (r"l3\.ccd\d+\.occupancy_pct$", "L3 in use", 100), (r"l3\.ccx\d+\.hit_rate$", "L3 hit rate", 100),
            (r"l3app\.game\.occupancy$", "Game L3", None), (r"mem\.bw_total$", "DRAM bandwidth", None),
            (r"pipe\.ipc$", "IPC", None), (r"pipe\.stall_be$", "Back-end bound", 100),
            (r"pipe\.stall_fe$", "Front-end bound", 100)],
}


def prefix_kind(prefix: str) -> str:
    for k in ("amdgpu", "nv", "cpu"):
        if re.match(rf"^{k}\d+$", prefix):
            return k
    return "net" if prefix == "net" else "other"


def overview_devices(order: list[str], sensors: dict) -> list[tuple[str, str]]:
    """(prefix, title) per device, GPUs first. Shared by the TUI and the web UI."""
    seen, out = set(), []
    for sid in order:
        p = sid.split(".", 1)[0]
        if p not in seen:
            seen.add(p)
            out.append((p, sensors[sid].get("group", p).split("/")[0]))
    rank = {"nv": 0, "amdgpu": 1, "cpu": 2, "net": 3, "other": 4}
    return sorted(out, key=lambda d: rank[prefix_kind(d[0])])


def overview_rows(p: str, order: list[str], sensors: dict) -> list[tuple]:
    """Headline rows of one device: (label, sensor, bar max) or ('pins'/'cores', [sensors], None)."""
    kind = prefix_kind(p)
    rows = []
    if kind == "net":
        # per connected interface: download/upload (bar = link speed), Wi-Fi signal; then TCP totals
        for sid in order:
            m = re.match(r"net\.(.+)\.link$", sid)
            if not m or not sensors[sid].get("value") or m.group(1) == "total":
                continue
            ip, name = f"net.{m.group(1)}", sensors[sid].get("group", "").split("/")[-1].split(":")[0]
            speed = (sensors.get(f"{ip}.speed") or {}).get("value")
            for key, arrow in (("rx", "↓"), ("tx", "↑")):
                s = sensors.get(f"{ip}.{key}")
                if s and s.get("value") is not None:
                    rows.append((f"{name} {arrow}", s, speed))
            s = sensors.get(f"{ip}.wifi.signal")
            if s and s.get("value") is not None:
                rows.append((f"{name} signal", s, None))
        for sid, label in (("net.tcp.established", "TCP connections"), ("net.tcp.retrans", "TCP retransmits")):
            s = sensors.get(sid)
            if s and s.get("value") is not None:
                rows.append((label, s, None))
        return rows[:10]
    if kind in HEADLINES:
        for rx, label, bmax in HEADLINES[kind]:
            for sid in order:
                if sid.startswith(p + ".") and re.search(rx, sid[len(p) + 1:]):
                    s = sensors[sid]
                    if s.get("value") is None:
                        continue
                    mx = None
                    if isinstance(bmax, (int, float)):
                        mx = bmax
                    elif isinstance(bmax, str):
                        if bmax == "@pwr.limit_real":
                            lim, raw = sensors.get(f"{p}.pwr.limit"), sensors.get(f"{p}.pwr.board")
                            if lim and raw and lim.get("value") and raw.get("value"):
                                mx = lim["value"] * s["value"] / raw["value"]
                        else:
                            o = sensors.get(f"{p}.{bmax[1:]}")
                            mx = o.get("value") if o else None
                    lab = label
                    m = re.search(r"(ccd|ccx|fan)(\d+)", sid)
                    if m and sum(1 for x in order if x.startswith(p + ".") and re.search(rx, x[len(p) + 1:])) > 1:
                        lab = f"{label} {m.group(2)}"
                    rows.append((lab, s, mx))
        if kind == "nv":
            pins = [sensors.get(f"{p}.pin{k}.a") for k in range(1, 7)]
            if any(x and x.get("value") is not None for x in pins):
                rows.append(("pins", pins, None))
        if kind == "cpu":
            cores = sorted({int(m.group(1)) for sid in order
                            for m in [re.match(rf"{p}\.core(\d+)\.clk_eff$", sid)] if m})
            if cores:
                rows.append(("cores", [sensors.get(f"{p}.core{k}.clk_eff") for k in cores], None))
    else:
        for sid in order:
            s = sensors[sid]
            if sid.startswith(p + ".") and s.get("unit") in ("°C", "RPM") and s.get("value") \
                    and (s["unit"] != "°C" or -20 < s["value"] < 150):     # skip unconnected probes
                rows.append((s["label"][:16], s, 100 if s["unit"] == "°C" else None))
            if len(rows) >= 8:
                break
    return rows


# ----------------------------------------------------------------------------- UI


class Tui:
    def __init__(self, scr, client: Client, interval: float, pins_file: Optional[str] = None,
                 theme: str = "auto"):
        self.scr = scr
        self.c = client
        self.interval = interval
        self.pins_file = pins_file or pins_path()
        self.pins = load_pins(self.pins_file)
        self.theme = theme if theme in THEMES else (load_theme(self.pins_file) or detect_theme())
        self.hist: dict[str, collections.deque] = {}
        self.view = 0
        self.sensors: dict[str, dict] = {}
        self.order: list[str] = []
        self.controls: list[dict] = []
        self.poll_ms = None
        self.hello: dict = {}
        self.osd: Optional[bool] = None
        self.err = ""
        self.msg, self.msg_t = "", 0.0
        self.scroll = {v: 0 for v in range(len(VIEWS))}
        self.sel = {v: 0 for v in range(len(VIEWS))}
        self.filter = ""
        self.last_poll = 0.0
        self.upd: dict = {}
        self.upd_t = 0.0
        self._colors()

    # ---- colours
    def _colors(self):
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        rich = curses.COLORS >= 256
        if self.theme == "light":
            pairs = {  # name: (fg 256, fg 8) chosen for a white/light background
                "accent": (166, curses.COLOR_RED), "dim": (243, curses.COLOR_BLACK), "ok": (28, curses.COLOR_GREEN),
                "warn": (130, curses.COLOR_MAGENTA), "crit": (160, curses.COLOR_RED), "title": (16, curses.COLOR_BLACK),
                "bar": (166, curses.COLOR_RED), "bar2": (25, curses.COLOR_BLUE), "head": (231, curses.COLOR_WHITE),
            }
            head_bg = (166, curses.COLOR_RED)
        else:
            pairs = {  # name: (fg 256, fg 8)
                "accent": (209, curses.COLOR_RED), "dim": (245, curses.COLOR_WHITE), "ok": (114, curses.COLOR_GREEN),
                "warn": (221, curses.COLOR_YELLOW), "crit": (203, curses.COLOR_RED), "title": (255, curses.COLOR_WHITE),
                "bar": (173, curses.COLOR_RED), "bar2": (75, curses.COLOR_CYAN), "head": (16, curses.COLOR_BLACK),
            }
            head_bg = (209, curses.COLOR_RED)
        self.cp = {}
        for i, (name, (c256, c8)) in enumerate(pairs.items(), start=1):
            if name == "head":
                curses.init_pair(i, c256 if rich else c8, head_bg[0] if rich else head_bg[1])
            else:
                curses.init_pair(i, c256 if rich else c8, bg)
            self.cp[name] = curses.color_pair(i)
        self.sev_attr = {0: 0, 1: self.cp["warn"] | curses.A_BOLD, 2: self.cp["crit"] | curses.A_BOLD}

    # ---- drawing helpers
    def put(self, y, x, text, attr=0, maxw=None):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        text = str(text)
        lim = w - x - (1 if y == h - 1 else 0)
        if maxw is not None:
            lim = min(lim, maxw)
        if lim <= 0:
            return
        try:
            self.scr.addstr(y, x, text[:lim], attr)
        except curses.error:
            pass

    def bar(self, y, x, width, frac, attr):
        if width <= 0 or frac is None:
            return
        frac = max(0.0, min(1.0, frac))
        cells = frac * width
        full = int(cells)
        part = BLOCKS[int((cells - full) * 8)] if full < width else ""
        self.put(y, x, "█" * full + part, attr)
        self.put(y, x + full + len(part), "·" * (width - full - len(part)), self.cp["dim"])

    def box(self, y, x, h, w, title):
        if h < 2 or w < 4:
            return
        self.put(y, x, "╭" + "─" * (w - 2) + "╮", self.cp["dim"])
        for i in range(1, h - 1):
            self.put(y + i, x, "│", self.cp["dim"])
            self.put(y + i, x + w - 1, "│", self.cp["dim"])
        self.put(y + h - 1, x, "╰" + "─" * (w - 2) + "╯", self.cp["dim"])
        self.put(y, x + 2, f" {title} ", self.cp["accent"] | curses.A_BOLD, w - 4)

    # ---- data
    def poll(self):
        try:
            if not self.hello:
                self.hello = self.c.call({"cmd": "hello"})
            snap = self.c.call({"cmd": "snapshot"})
            osd = self.c.call({"cmd": "osd_get"})
            self.err = ""
        except (OSError, ValueError) as e:
            self.err = f"daemon unreachable ({self.c.path}): {e}"
            self.hello = {}
            return
        self.order = []
        for s in snap.get("sensors", []):
            self.sensors[s["id"]] = s
            self.order.append(s["id"])
            v = s.get("value")      # every sensor, so a newly pinned graph has history straight away
            self.hist.setdefault(s["id"], collections.deque(maxlen=HISTORY)).append(
                v if isinstance(v, (int, float)) and not isinstance(v, bool) else None)
        self.controls = snap.get("controls", [])
        self.poll_ms = snap.get("poll_ms")
        self.osd = osd.get("enabled")
        if time.time() - self.upd_t > (3 if self.upd.get("running") or self.upd.get("checking") else 30):
            self.upd_t = time.time()
            try:
                r = self.c.call({"cmd": "update_status"})
                self.upd = r if r.get("ok") else {}
            except (OSError, ValueError):
                self.upd = {}

    def hotkey_key(self):
        try:
            cur = self.c.call({"cmd": "osd_get"}).get("hotkey", "")
            raw = self.prompt(f"OSD hotkey (now {cur.replace('KEY_', '')}), e.g. RIGHTCTRL+F12: ")
            if not raw or not raw.strip():
                return
            r = self.c.call({"cmd": "osd_set", "hotkey": raw.strip()})
            if not r.get("ok"):
                self.flash(str(r.get("msg", "")))
                return
            from meat3.osd import mangohud
            new = mangohud.parse_hotkey(raw.strip())
            mh = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                              "meat3", "MangoHud.conf")
            same = mangohud.set_toggle_key(mh, "+".join(new))
            self.flash(f"hotkey now {'+'.join(k[4:] for k in new)}"
                       + ("; MangoHud config updated too (applies live)" if same else ""))
        except (OSError, ValueError) as e:
            self.flash(f"hotkey: {e}")

    def update_key(self):
        u = self.upd
        try:
            if not u:
                self.flash("this daemon has no updater (update once by hand: packaging/update.sh)")
            elif u.get("running"):
                self.flash("updating… " + ((u.get("log") or [""])[-1])[:150])
            elif u.get("error"):
                self.c.call({"cmd": "update_check"})
                self.flash(f"update check: {u['error'][:150]} (checking again)")
            elif u.get("behind"):
                commits = u.get("commits") or []
                if not u.get("can_update"):
                    self.flash(f"{u['behind']} updates available; only the owner of the git checkout can install them")
                    return
                if self.prompt(f"{u['behind']} new: {commits[0][8:70] if commits else ''}… install now? type y: ") != "y":
                    return
                r = self.c.call({"cmd": "update_start"})
                self.flash(("" if r.get("ok") else "FAILED  ") + str(r.get("msg", "")))
                self.upd_t = 0.0
            else:
                self.c.call({"cmd": "update_check"})
                self.upd_t = time.time() - 27
                self.flash(f"installed {u.get('installed', '?')}; checking GitHub for updates…")
        except (OSError, ValueError) as e:
            self.flash(f"update: {e}")

    def flash(self, m):
        self.msg, self.msg_t = m, time.time()

    # ---- frame
    def draw(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        head = " meat³ "
        self.put(0, 0, " " * w, self.cp["head"])
        self.put(0, 0, head, self.cp["head"] | curses.A_BOLD)
        x = len(head) + 1
        for i, v in enumerate(VIEWS):
            t = f" {i + 1} {v} "
            self.put(0, x, t, (curses.A_REVERSE | curses.A_BOLD) if i == self.view else self.cp["head"])
            x += len(t) + 1
        u = self.upd
        upd = ("updating… · " if u.get("running") else
               f"⬆ {u['behind']} updates (u) · " if u.get("behind") else "")
        info = f"{upd}{self.hello.get('host', '?')} · poll {self.poll_ms or 0:.0f} ms · OSD {'on' if self.osd else 'off'} · " \
               f"{time.strftime('%H:%M:%S')} "
        self.put(0, max(x, w - len(info)), info, self.cp["head"])
        body_h = h - 2
        keys = {0: ("↑↓ select pin  g graph/value  x unpin  [ ] move  a add  " if self.pins else
                    "a add sensors  ") + "Tab views  o OSD  h hotkey  r reset  u update  T theme  q quit",
                1: "↑↓ PgUp/PgDn move  a pin to Overview  g pin as graph  / filter  Esc clear  o OSD  T theme  q quit",
                2: "↑↓ scroll  o OSD  r reset  u update  T theme  q quit",
                3: "↑↓ select  Enter set value  o OSD  u update  T theme  q quit"}[self.view]
        status = self.msg if self.msg and time.time() - self.msg_t < 6 else keys
        self.put(h - 1, 0, " " * (w - 1), self.cp["dim"])
        self.put(h - 1, 1, status, self.cp["accent"] if status == self.msg else self.cp["dim"])
        if self.err:
            self.put(2, 2, self.err, self.cp["crit"] | curses.A_BOLD)
            self.put(3, 2, "Start it with: sudo systemctl start meat3d   (retrying)", self.cp["dim"])
        elif self.view == 0:
            self.draw_overview(1, body_h)
        elif self.view == 1:
            self.draw_sensors(1, body_h)
        elif self.view == 2:
            self.draw_cores(1, body_h)
        else:
            self.draw_controls(1, body_h)
        self.scr.refresh()

    # ---- view 1: overview
    def _devices(self) -> list[tuple[str, str]]:
        return overview_devices(self.order, self.sensors)

    def _panel_rows(self, p: str) -> list[tuple]:
        return overview_rows(p, self.order, self.sensors)

    def _pin_label(self, sid: str) -> str:
        s = self.sensors.get(sid)
        if not s:
            return sid
        dev = s.get("group", "").split("/")[0].split(":")[0]       # "GPU 0: RTX 5080" -> "GPU 0"
        return f"{dev} · {s.get('label', sid)}" if dev else s.get("label", sid)

    def draw_pins(self, top, height) -> int:
        """Pinned panel across the full width; returns the rows used."""
        h, w = self.scr.getmaxyx()
        self.sel[0] = max(0, min(self.sel[0], len(self.pins) - 1))
        tall = lambda p: 1 + (GRAPH_H if p["graph"] and p["id"] in self.sensors else 0)
        need = 2 + sum(tall(p) for p in self.pins)
        ph = min(need, height)
        if ph < 3:
            return 0
        self.box(top, 0, ph, w - 1, "Pinned")
        y, inner = top + 1, w - 5
        for i, p in enumerate(self.pins):
            rows = tall(p)
            if y + rows > top + ph - 1:
                self.put(top + ph - 1, w - 16, " more below… ", self.cp["dim"])
                break
            s = self.sensors.get(p["id"])
            lab = self._pin_label(p["id"])
            lw = max(16, min(48, inner - 44))
            tw = w - 3 - (17 + lw)                    # room left of the right border
            self.put(y, 2, lab[:lw].ljust(lw), (curses.A_REVERSE if i == self.sel[0] else 0) | self.cp["dim"])
            self.put(y, 3 + lw, fv(s).rjust(12), (self.sev_attr[severity(s)] or self.cp["title"]) if s else self.cp["dim"])
            if not s:
                self.put(y, 17 + lw, "not reported by the daemon right now", self.cp["dim"], tw)
            elif p["graph"]:
                vals = [v for v in self.hist.get(p["id"], ()) if v is not None][-inner:]
                if vals:
                    span = min(len(self.hist.get(p["id"], ())), inner) * self.interval
                    rng = " – ".join(format_value(v, s.get("decimals", 0), s.get("unit", "")) for v in (min(vals), max(vals)))
                    when = f"{span / 60:.0f} min" if span >= 60 else f"{span:.0f} s"
                    self.put(y, 17 + lw, f"{rng}  over the last {when}", self.cp["dim"], tw)
                pct = s.get("unit") == "%"
                g = graph_rows(self.hist.get(p["id"], ()), inner, GRAPH_H, 0.0 if pct else None, 100.0 if pct else None)
                sev = severity(s)
                for r, line in enumerate(g):
                    self.put(y + 1 + r, 3, line, self.sev_attr[sev] if sev else self.cp["bar2"])
            else:
                self.put(y, 17 + lw, f"min {fv(s, 'min')}  max {fv(s, 'max')}  avg {fv(s, 'avg')}", self.cp["dim"], tw)
            y += rows
        return ph

    def draw_overview(self, top, height):
        h, w = self.scr.getmaxyx()
        if self.pins:
            used = self.draw_pins(top, max(0, height - 4))
            top, height = top + used, height - used
        devs = [(p, t, self._panel_rows(p)) for p, t in self._devices()]
        devs = [d for d in devs if d[2]]
        cols = 2 if w >= 110 else 1
        colw = (w - 1) // cols
        ys = [top] * cols
        for p, title, rows in devs:
            ph = len(rows) + 2
            c = min(range(cols), key=lambda i: ys[i])
            y, x = ys[c], c * colw
            if y + ph > top + height + 1:
                ph = top + height + 1 - y
                if ph < 4:
                    continue
            self.box(y, x, ph, colw - 1, title)
            inner = colw - 5
            for i, (label, s, mx) in enumerate(rows[: ph - 2]):
                yy = y + 1 + i
                if label == "pins":
                    self.put(yy, x + 2, "Pins", self.cp["dim"])
                    xx = x + 2 + 16
                    for k, ps in enumerate(s, start=1):
                        v = ps.get("value") if ps else None
                        t = f"{k}:{'—' if v is None else f'{v:4.1f}'}A "
                        self.put(yy, xx, t, self.sev_attr[severity(ps)] if ps else self.cp["dim"])
                        xx += len(t)
                    continue
                if label == "cores":
                    self.put(yy, x + 2, "Cores", self.cp["dim"])
                    xx = x + 2 + 16
                    vals = [c.get("value") if c else None for c in s]
                    top_v = max([v for v in vals if v] or [1.0])
                    for v in vals:
                        ch = "▁▂▃▄▅▆▇█"[min(7, int(8 * (v or 0) / max(top_v, 1.0)))] if v else " "
                        self.put(yy, xx, ch * 2, self.cp["bar2"])
                        xx += 3
                    self.put(yy, xx + 1, f"peak {top_v:.0f} MHz", self.cp["dim"])
                    continue
                self.put(yy, x + 2, label[:15], self.cp["dim"])
                val = fv(s)
                self.put(yy, x + 18, val.rjust(12), self.sev_attr[severity(s)] or self.cp["title"])
                bw = inner - 30
                if mx and bw > 4:
                    sev = severity(s)
                    attr = self.sev_attr[sev] if sev else self.cp["bar"]
                    self.bar(yy, x + 32, bw, (s.get("value") or 0) / mx, attr)
            ys[c] = y + ph

    # ---- view 2: sensors
    def _sensor_lines(self) -> list[tuple]:
        flt = self.filter.lower()
        lines, last = [], None
        for sid in self.order:
            s = self.sensors[sid]
            if flt and flt not in f"{s.get('label', '')} {sid} {s.get('group', '')}".lower():
                continue
            g = s.get("group", "")
            if g != last:
                dev, prev_dev = g.split("/")[0], (last or "").split("/")[0]
                if dev != prev_dev and "/" in g:
                    lines.append(("g", dev))           # device header before its first subgroup
                lines.append(("g", g))
                last = g
            lines.append(("s", s))
        return lines

    def draw_sensors(self, top, height):
        h, w = self.scr.getmaxyx()
        lines = self._sensor_lines()
        cw = 13
        lw = max(20, w - 4 * cw - 3)
        hdr = "Sensor".ljust(lw) + "".join(t.rjust(cw) for t in ("Current", "Min", "Max", "Avg"))
        if self.filter:
            hdr = f"Sensor  [filter: {self.filter}]".ljust(lw) + hdr[lw:]
        self.put(top, 1, hdr, self.cp["dim"] | curses.A_UNDERLINE)
        n = height - 1
        # the cursor moves over sensor rows only; keep it on screen
        srows = [i for i, ln in enumerate(lines) if ln[0] == "s"]
        self.sel[1] = max(0, min(self.sel[1], len(srows) - 1))
        cur = srows[self.sel[1]] if srows else -1
        if cur >= 0:
            if cur < self.scroll[1] + 1:
                self.scroll[1] = max(0, cur - 1)          # also show the group header above
            elif cur >= self.scroll[1] + n:
                self.scroll[1] = cur - n + 1
        self.scroll[1] = max(0, min(self.scroll[1], max(0, len(lines) - n)))
        pinned = {p["id"]: p["graph"] for p in self.pins}
        for i, ln in enumerate(lines[self.scroll[1]: self.scroll[1] + n]):
            y = top + 1 + i
            if ln[0] == "g":
                parts = ln[1].split("/")
                self.put(y, 1, ("  " * (len(parts) - 1)) + parts[-1], self.cp["accent"] | curses.A_BOLD
                         if len(parts) == 1 else self.cp["accent"])
                continue
            s = ln[1]
            depth = s.get("group", "").count("/") + 1
            if self.scroll[1] + i == cur:
                self.put(y, 0, " " * (w - 1), curses.A_REVERSE)
            mark = {True: "▁▃▅", False: "•"}.get(pinned.get(s["id"]), "")
            self.put(y, 1 + 2 * depth, s.get("label", "")[: lw - 2 * depth - 5] + (f" {mark}" if mark else ""),
                     curses.A_REVERSE if self.scroll[1] + i == cur else 0)
            sev = self.sev_attr[severity(s)]
            for j, key in enumerate(("value", "min", "max", "avg")):
                a = sev if key == "value" else (self.sev_attr[severity(s, s.get(key))] or self.cp["dim"])
                self.put(y, 1 + lw + j * cw, fv(s, key).rjust(cw),
                         (a or self.cp["title"]) | (curses.A_REVERSE if self.scroll[1] + i == cur else 0))
        if len(lines) > n:
            pos = f" {self.scroll[1] + 1}-{min(len(lines), self.scroll[1] + n)} of {len(lines)} "
            self.put(h - 1, w - len(pos) - 1, pos, self.cp["dim"])

    # ---- view 3: cores
    def draw_cores(self, top, height):
        h, w = self.scr.getmaxyx()
        y = top
        prefixes = sorted({m.group(1) for sid in self.order for m in [re.match(r"^(.+)\.core\d+\.clk_eff$", sid)] if m})
        cols = [("Eff clock", ["core{k}.clk_eff"]), ("Active", ["core{k}.clk_active"]),
                ("Volt", ["pmt.core{k}.volt", "core{k}.volt"]), ("Temp", ["pmt.core{k}.temp", "core{k}.temp"]),
                ("Power", ["core{k}.power", "pmt.core{k}.power"]), ("Usage", ["core{k}.util", "core{k}.usage"]),
                ("CC6", ["pmt.core{k}.cc6"]), ("CPPC", ["core{k}.cppc_rank"])]
        for p in prefixes:
            cores = sorted({int(m.group(1)) for sid in self.order for m in [re.match(rf"{re.escape(p)}\.core(\d+)\.clk_eff$", sid)] if m})
            title = self.sensors[f"{p}.core{cores[0]}.clk_eff"].get("group", p).split("/")[0]
            occ = [self.sensors.get(f"{p}.l3.ccd{i}.occupancy") for i in range(8)]
            l3 = "  ".join(f"CCD{i} L3 {o['value']:.1f} MB" for i, o in enumerate(occ) if o and o.get("value") is not None)
            self.put(y, 1, title, self.cp["accent"] | curses.A_BOLD)
            self.put(y, 2 + len(title), ("· " + l3) if l3 else "", self.cp["dim"])
            y += 1
            present = [(n, tp) for n, tp in cols if any(f"{p}.{t.format(k=cores[0])}" in self.sensors for t in tp)]
            cw = max(10, min(14, (w - 10) // max(1, len(present) + 1)))
            self.put(y, 1, "Core".ljust(8) + "".join(n.rjust(cw) for n, _ in present) + "  clock", self.cp["dim"] | curses.A_UNDERLINE)
            y += 1
            effs = [self.sensors.get(f"{p}.core{k}.clk_eff", {}).get("value") or 0 for k in cores]
            peak = max(effs + [1.0])
            for k in cores:
                if y >= top + height:
                    break
                self.put(y, 1, f"Core {k}".ljust(8), self.cp["title"])
                x = 9
                for n, tp in present:
                    s = next((self.sensors[f"{p}.{t.format(k=k)}"] for t in tp if f"{p}.{t.format(k=k)}" in self.sensors), None)
                    a = self.sev_attr[severity(s)] if s else 0
                    self.put(y, x, fv(s).rjust(cw), a or 0)
                    x += cw
                eff = self.sensors.get(f"{p}.core{k}.clk_eff", {}).get("value") or 0
                self.bar(y, x + 2, max(0, w - x - 4), eff / peak, self.cp["bar2"])
                y += 1
            y += 1
            apps = []
            for sid in self.order:
                m = re.match(rf"{re.escape(p)}\.l3app\.([a-z0-9_]+)\.occupancy$", sid)
                if m and m.group(1) not in ("game",):
                    s = self.sensors[sid]
                    if s.get("value") is not None:
                        apps.append((s["value"], s["label"].removesuffix(" L3 in use"),
                                     self.sensors.get(f"{p}.l3app.{m.group(1)}.cpu", {}).get("value")))
            if apps and y < top + height - 2:
                game = self.sensors.get(f"{p}.l3app.game.occupancy")
                gname = (game or {}).get("hint", "").removeprefix("game: ").split(" (")[0] \
                    if game and game.get("value") is not None else ""
                self.put(y, 1, "L3 by app", self.cp["accent"] | curses.A_BOLD)
                if gname:
                    self.put(y, 12, f"· game: {gname} {game['value']:.1f} MB", self.cp["dim"])
                y += 1
                rs = self.sensors.get(f"{p}.l3.reserve.active")
                if rs is not None and y < top + height - 2:
                    st = rs.get("label", "").removeprefix("L3 reservation: ")
                    if rs.get("value"):
                        g = self.sensors.get(f"{p}.l3.reserve.game_mb", {}).get("value")
                        b = self.sensors.get(f"{p}.l3.reserve.bg_mb", {}).get("value")
                        txt = f"L3 reserved {st.removeprefix('active ')}" + (
                            f": game {g:.0f} MB, everything else {b:.0f} MB" if g is not None and b is not None else "")
                        self.put(y, 3, txt[:w - 4], self.cp["ok"] | curses.A_BOLD)
                    else:
                        self.put(y, 3, f"L3 reservation: {st}"[:w - 4], self.cp["dim"])
                    y += 1
                size = None
                for sid, s in self.sensors.items():
                    m = re.search(r"% of (\d+) MiB", s.get("label", "")) if sid.startswith(f"{p}.l3.ccd") else None
                    if m:
                        size = (size or 0) + int(m.group(1)) * 1.048576
                top_mb = size or max(a[0] for a in apps)
                for mb, name, cpu in sorted(apps, reverse=True):
                    if y >= top + height:
                        break
                    self.put(y, 3, name[:28].ljust(30), self.cp["title"])
                    self.put(y, 33, f"{mb:6.1f} MB", 0)
                    self.put(y, 44, "" if cpu is None else f"cpu {cpu:4.1f}%", self.cp["dim"])
                    self.bar(y, 56, max(0, w - 60), mb / top_mb, self.cp["bar"])
                    y += 1
            y += 1

    # ---- view 4: controls
    def draw_controls(self, top, height):
        h, w = self.scr.getmaxyx()
        ctl = self.controls
        if not ctl:
            self.put(top + 1, 2, "no controls", self.cp["dim"])
            return
        self.sel[3] = max(0, min(self.sel[3], len(ctl) - 1))
        lines, last_g, sel_line = [], None, 0
        for i, c in enumerate(ctl):
            g = c.get("group", "").split("/")[0]
            if g != last_g:
                lines.append(("g", g))
                last_g = g
            if i == self.sel[3]:
                sel_line = len(lines)
            lines.append(("c", i))
        n = height
        if sel_line < self.scroll[3]:
            self.scroll[3] = max(0, sel_line - 1)
        elif sel_line >= self.scroll[3] + n:
            self.scroll[3] = sel_line - n + 1
        for row, ln in enumerate(lines[self.scroll[3]: self.scroll[3] + n]):
            y = top + row
            if ln[0] == "g":
                self.put(y, 1, ln[1], self.cp["accent"] | curses.A_BOLD)
                continue
            i = ln[1]
            c = ctl[i]
            selected = i == self.sel[3]
            attr = curses.A_REVERSE if selected else 0
            if not c.get("available"):
                attr |= self.cp["dim"]
            danger = {0: " ", 1: "!", 2: "‼"}.get(c.get("danger", 0), "‼")
            val = c.get("value")
            dec = 3 if isinstance(val, float) and not val.is_integer() else 0
            if c.get("kind") == "curve":
                n = len((val or {}).get("points") or []) if isinstance(val, dict) else 0
                vs = f"{n} pts (GUI)" if n else "—"
            else:
                vs = "—" if val is None else (f"{val}" if c.get("kind") in ("choice", "bool") else
                                             format_value(val, dec, c.get("unit", "")))
            rng = ""
            if c.get("kind") in ("int", "float") and c.get("min") is not None:
                rng = f"[{c['min']:g}..{c['max']:g}]"
            elif c.get("kind") == "choice":
                rng = "/".join(map(str, c.get("choices") or []))[:40]
            line = f" {danger} {c.get('label', c['id'])[:58]:<59}{vs:>14}  {rng}"
            self.put(y, 1, line.ljust(w - 2), attr | (self.cp["warn"] if c.get("danger", 0) >= 2 and not selected else 0))

    # ---- input
    def prompt(self, text: str, initial: str = "") -> Optional[str]:
        h, w = self.scr.getmaxyx()
        buf = initial
        self.scr.timeout(-1)
        curses.curs_set(1)
        try:
            while True:
                self.put(h - 1, 0, " " * (w - 1), 0)
                self.put(h - 1, 1, text + buf, self.cp["accent"] | curses.A_BOLD)
                self.scr.refresh()
                ch = self.scr.get_wch()
                if ch in ("\n", "\r", curses.KEY_ENTER):
                    return buf
                if ch == "\x1b":
                    return None
                if ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    buf = buf[:-1]
                elif isinstance(ch, str) and ch.isprintable():
                    buf += ch
        finally:
            curses.curs_set(0)
            self.scr.timeout(100)

    def edit_control(self):
        if not self.controls:
            return
        c = self.controls[self.sel[3]]
        if not c.get("available"):
            self.flash(f"{c.get('label')}: not available — {c.get('hint', '')[:120]}")
            return
        kind = c.get("kind")
        if kind == "action":
            if self.prompt(f"Run '{c['label']}'? type y: ") != "y":
                return
            value = None
        elif kind == "bool":
            value = not bool(c.get("value"))
            if self.prompt(f"Set {c['label']} to {value}? type y: ") != "y":
                return
        else:
            hint = f"[{c['min']:g}..{c['max']:g} {c.get('unit', '')}]" if kind in ("int", "float") and c.get("min") is not None \
                else ("(" + "/".join(map(str, c.get("choices") or [])) + ")" if kind == "choice" else "")
            raw = self.prompt(f"{c['label']} {hint} = ", "" if c.get("value") is None else str(c.get("value")))
            if raw is None or raw.strip() == "":
                return
            raw = raw.strip()
            if kind == "int":
                try:
                    value = int(float(raw))
                except ValueError:
                    self.flash(f"not a number: {raw}")
                    return
            elif kind == "float":
                try:
                    value = float(raw)
                except ValueError:
                    self.flash(f"not a number: {raw}")
                    return
            else:
                value = raw
            if c.get("danger", 0) >= 2 and self.prompt(f"‼ {c['label']} = {value} is a dangerous setting. type yes: ") != "yes":
                self.flash("cancelled")
                return
        try:
            r = self.c.call({"cmd": "apply", "id": c["id"], "value": value})
            self.flash(("OK  " if r.get("ok") else "FAILED  ") + str(r.get("msg", "")))
            self.poll()
        except (OSError, ValueError) as e:
            self.flash(f"apply failed: {e}")

    # ---- pinned sensors
    def _save_pins(self):
        try:
            save_pins(self.pins_file, self.pins)
        except OSError as e:
            self.flash(f"couldn't save pins to {self.pins_file}: {e}")

    def _cursor_sensor(self) -> Optional[dict]:
        rows = [ln[1] for ln in self._sensor_lines() if ln[0] == "s"]
        return rows[min(self.sel[1], len(rows) - 1)] if rows else None

    def pin_key(self, graph: bool):
        """Sensors view: a = pin/unpin as a value; g = pin as a graph, or switch a pin between value and graph."""
        s = self._cursor_sensor()
        if not s:
            return
        p = next((p for p in self.pins if p["id"] == s["id"]), None)
        name = s.get("label", s["id"])
        if p is None:
            self.pins.append({"id": s["id"], "graph": graph})
            self.flash(f"pinned {name} to the Overview" + (" as a graph" if graph else ""))
        elif graph:
            p["graph"] = not p["graph"]
            self.flash(f"{name}: shown as a {'graph' if p['graph'] else 'value'}")
        else:
            self.pins.remove(p)
            self.flash(f"unpinned {name}")
        self._save_pins()

    def pin_edit(self, ch):
        """Overview: change the selected pin."""
        if not self.pins:
            return
        i = self.sel[0] = max(0, min(self.sel[0], len(self.pins) - 1))
        if ch == "g":
            self.pins[i]["graph"] = not self.pins[i]["graph"]
        elif ch in ("x", curses.KEY_DC):
            self.flash(f"unpinned {self._pin_label(self.pins.pop(i)['id'])}")
            self.sel[0] = max(0, min(i, len(self.pins) - 1))
        elif ch in ("[", "]"):
            j = i + (-1 if ch == "[" else 1)
            if 0 <= j < len(self.pins):
                self.pins[i], self.pins[j] = self.pins[j], self.pins[i]
                self.sel[0] = j
        self._save_pins()

    def key(self, ch) -> bool:
        v = self.view
        if ch in ("q", "Q"):
            return False
        if ch == "T":
            self.theme = "light" if self.theme == "dark" else "dark"
            self._colors()
            try:
                save_theme(self.pins_file, self.theme)
            except OSError:
                pass
            self.flash(f"{self.theme} theme (T toggles; MEAT3_TUI_THEME or --theme to force)")
            return True
        if ch in ("1", "2", "3", "4"):
            self.view = int(ch) - 1
        elif ch == "\t":
            self.view = (v + 1) % len(VIEWS)
        elif ch == curses.KEY_BTAB:
            self.view = (v - 1) % len(VIEWS)
        elif ch == "o":
            try:
                r = self.c.call({"cmd": "osd_toggle"})
                self.flash(f"OSD {'enabled' if r.get('enabled') else 'disabled'}" if r.get("ok") else r.get("msg", ""))
            except (OSError, ValueError) as e:
                self.flash(str(e))
        elif ch == "r":
            try:
                r = self.c.call({"cmd": "reset_stats"})
                self.flash("min/max/avg reset" if r.get("ok") else r.get("msg", ""))
            except (OSError, ValueError) as e:
                self.flash(str(e))
        elif ch == "u":
            self.update_key()
        elif ch == "h":
            self.hotkey_key()
        elif ch in ("a", "g") and v == 1:
            self.pin_key(graph=ch == "g")
        elif ch == "a" and v == 0:
            self.view = 1
            self.flash("pick a sensor: a pins it as a value, g as a graph (/ to search)")
        elif ch in ("g", "x", "[", "]", curses.KEY_DC) and v == 0:
            self.pin_edit(ch)
        elif ch == "/" and v == 1:
            f = self.prompt("filter: ", self.filter)
            if f is not None:
                self.filter, self.scroll[1], self.sel[1] = f, 0, 0
        elif ch == "\x1b" and v == 1:
            self.filter = ""
        elif ch in ("\n", "\r", curses.KEY_ENTER) and v == 3:
            self.edit_control()
        else:
            h, _ = self.scr.getmaxyx()
            page = max(1, h - 4)
            delta = {curses.KEY_UP: -1, "k": -1, curses.KEY_DOWN: 1, "j": 1, curses.KEY_PPAGE: -page,
                     curses.KEY_NPAGE: page, curses.KEY_HOME: -10 ** 6, curses.KEY_END: 10 ** 6}.get(ch)
            if delta is not None:
                if v == 3:
                    self.sel[3] = max(0, min(len(self.controls) - 1, self.sel[3] + delta))
                elif v == 1:
                    self.sel[1] = max(0, self.sel[1] + delta)       # clamped when drawn
                elif v == 0:
                    self.sel[0] = max(0, min(len(self.pins) - 1, self.sel[0] + delta))
                else:
                    self.scroll[v] = max(0, self.scroll[v] + delta)
        return True

    def run(self):
        curses.curs_set(0)
        self.scr.timeout(100)
        self.scr.keypad(True)
        while True:
            now = time.time()
            if now - self.last_poll >= self.interval:
                self.last_poll = now
                self.poll()
            self.draw()
            try:
                ch = self.scr.get_wch()
            except curses.error:
                continue
            if ch == curses.KEY_RESIZE:
                continue
            if not self.key(ch):
                return


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="meat3-tui", description="meat³ terminal UI")
    ap.add_argument("--socket", default=os.environ.get("MEAT3_SOCKET", "/run/meat3/meat3.sock"))
    ap.add_argument("--interval", type=float, default=1.0, help="refresh interval in seconds")
    ap.add_argument("--theme", choices=("auto", "dark", "light"), default="auto",
                    help="colour scheme for the terminal background (auto: saved choice, then MEAT3_TUI_THEME, "
                         "then COLORFGBG, else dark; T toggles at runtime and saves)")
    args = ap.parse_args(argv)
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    client = Client(args.socket)
    try:
        curses.wrapper(lambda scr: Tui(scr, client, max(0.2, args.interval), theme=args.theme).run())
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
