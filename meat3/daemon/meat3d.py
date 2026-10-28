"""meat3d - root daemon: polls all hardware backends, serves the GUI/OSD over a Unix socket.

Run: python3 -m meat3.daemon [--socket /run/meat3/meat3.sock] [--once]
Protocol: see PROTOCOL.md.
"""
from __future__ import annotations

import argparse
import grp
import importlib
import inspect
import json
import os
import signal
import pkgutil
import pwd
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import traceback

from .. import backends as backends_pkg
from ..core import config, fmt
from ..core.updater import Updater
from ..osd.mangohud import parse_hotkey
from ..core.model import Backend

VERSION = "0.1"
RUN_DIR = "/run/meat3"
OSD_TXT = os.path.join(RUN_DIR, "osd.txt")
WRITE_GROUP = "meat3"


# ----------------------------------------------------------------------------- backends
def discover_backends() -> list[Backend]:
    found: list[Backend] = []
    for mod in pkgutil.iter_modules(backends_pkg.__path__):
        if mod.name.startswith("_"):
            continue
        try:
            m = importlib.import_module(f"{backends_pkg.__name__}.{mod.name}")
        except Exception as e:
            print(f"meat3d: backend module {mod.name} failed to import: {e}")
            continue
        for _, cls in inspect.getmembers(m, inspect.isclass):
            if issubclass(cls, Backend) and cls is not Backend and not inspect.isabstract(cls) \
                    and cls.__module__ == m.__name__ and not getattr(cls, "abstract_base", False):
                try:
                    inst = cls.detect()
                    for b in inst:
                        print(f"meat3d: + {b.kind:5} {b.name}  ({cls.__module__}.{cls.__name__})")
                    found.extend(inst)
                except Exception:
                    print(f"meat3d: {cls.__name__}.detect() failed:\n{traceback.format_exc()}")
    order = {"cpu": 0, "gpu": 1, "board": 2}
    found.sort(key=lambda b: (order.get(b.kind, 9), b.name))
    return found


def load_modules():
    for mod in ("msr", "amd_uncore", "ryzen_smu", "i2c-dev"):
        subprocess.run(["modprobe", "-q", mod], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ----------------------------------------------------------------------------- state
class State:
    def __init__(self, backends: list[Backend]):
        self.backends = backends
        self.lock = threading.RLock()
        self.sensors: list[dict] = []
        self.controls: list[dict] = []
        self.ctl_owner: dict[str, Backend] = {}
        self.stats: dict[str, list] = {}         # id -> [min, max, sum, n]
        self.t = 0.0
        self.poll_ms = 0.0
        cfg = config.get()
        self.osd = dict(cfg["osd"])

    def poll(self):
        t0 = time.time()
        rows = []
        for b in self.backends:
            try:
                rows.extend(s.to_dict() for s in b.sensors())
            except Exception as e:
                rows.append({"id": f"err.{b.name}", "group": f"{b.name}/Errors", "label": "backend error",
                             "value": None, "unit": "", "decimals": 0, "hint": str(e)})
        with self.lock:
            for r in rows:
                v = r.get("value")
                st = self.stats.get(r["id"])
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    if st is None:
                        st = self.stats[r["id"]] = [v, v, 0.0, 0]
                    st[0] = min(st[0], v); st[1] = max(st[1], v); st[2] += v; st[3] += 1
                if st:
                    r["min"], r["max"], r["avg"] = st[0], st[1], (st[2] / st[3] if st[3] else None)
                else:
                    r["min"] = r["max"] = r["avg"] = None
            self.sensors = rows
            self.t = t0
        self.poll_ms = (time.time() - t0) * 1000
        try:
            self.eval_guards()
        except Exception as e:
            print(f"meat3d: guard evaluation failed: {e}")
        self.write_osd()

    def eval_guards(self):
        """Safety rules from config['guards']; trips apply a control (e.g. drop the power limit)."""
        import fnmatch
        guards = config.get().get("guards", [])
        if not hasattr(self, "guard_hits"):
            self.guard_hits, self.guard_log = {}, []
        for gi, g in enumerate(guards):
            if not g.get("enabled", True):
                continue
            hot = [s for s in self.sensors if fnmatch.fnmatch(s["id"], g["sensor"])
                   and isinstance(s.get("value"), (int, float)) and s["value"] > g["above"]]
            n = self.guard_hits.get(gi, 0) + 1 if hot else 0
            self.guard_hits[gi] = n
            if n == int(g.get("count", 1)):
                worst = max(hot, key=lambda s: s["value"])
                msgs = []
                ctl_by_id = {c["id"]: c for c in self.controls}
                for cid, b in list(self.ctl_owner.items()):
                    if fnmatch.fnmatch(cid, g["control"]):
                        val = g["value"]
                        if val in ("min", "max"):
                            val = ctl_by_id.get(cid, {}).get(val)
                        if val is None:
                            msgs.append(f"{cid}: no {g['value']} known"); continue
                        ok, msg = b.apply(cid, val)
                        msgs.append(f"{cid}={val} -> {'ok' if ok else 'FAILED: ' + msg}")
                entry = {"t": time.time(), "guard": g["name"],
                         "trigger": f"{worst['label']} = {worst['value']} {worst.get('unit', '')} > {g['above']}",
                         "actions": msgs}
                self.guard_log = (self.guard_log + [entry])[-50:]
                print(f"meat3d: GUARD TRIP {entry}")
                if msgs:
                    self.refresh_controls()

    def refresh_controls(self):
        ctls, owner = [], {}
        for b in self.backends:
            try:
                for c in b.controls():
                    ctls.append(c.to_dict()); owner[c.id] = b
            except Exception as e:
                print(f"meat3d: {b.name}.controls() failed: {e}")
        with self.lock:
            self.controls, self.ctl_owner = ctls, owner

    def write_osd(self):
        if not self.osd.get("enabled"):
            text = ""
        else:
            by_id = {s["id"]: s for s in self.sensors}
            lines = []
            for sid in self.osd.get("items", []):          # exactly one line per item, in order
                s = by_id.get(sid)
                lines.append(fmt.osd_line(s) if s else f"{sid}: —")
            text = "\n".join(lines) + ("\n" if lines else "")
        tmp = OSD_TXT + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(text)
            os.chmod(tmp, 0o644)
            os.replace(tmp, OSD_TXT)
        except OSError:
            pass

    def save_osd(self):
        cfg = config.get()
        cfg["osd"] = dict(self.osd)
        try:
            config.save(cfg)
        except OSError as e:
            print(f"meat3d: could not save config: {e}")

    def toggle_osd(self) -> bool:
        with self.lock:
            self.osd["enabled"] = not self.osd.get("enabled")
            en = self.osd["enabled"]
        self.save_osd()
        self.write_osd()
        mh = shutil.which("mangohudctl")
        if mh:   # best effort: toggle any MangoHud instances too
            subprocess.Popen([mh, "toggle", "hud"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"meat3d: OSD {'on' if en else 'off'}")
        return en


# ----------------------------------------------------------------------------- server
def peer_uid(sock) -> int:
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", creds)[1]
    except OSError:
        return -1


def peer_can_write(sock) -> bool:
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", creds)
    except OSError:
        return False
    if uid == 0:
        return True
    try:
        g = grp.getgrnam(WRITE_GROUP)
        user = pwd.getpwuid(uid).pw_name
        return gid == g.gr_gid or user in g.gr_mem
    except KeyError:
        return False


UPDATE_EVERY = 6 * 3600       # re-check GitHub at most this often when a client asks for status


class Handler(socketserver.StreamRequestHandler):
    state: State = None
    updater: Updater = Updater()

    def handle(self):
        can_write = peer_can_write(self.request)
        self.uid = peer_uid(self.request)
        for line in self.rfile:
            try:
                req = json.loads(line)
                resp = self.dispatch(req, can_write)
            except Exception as e:
                resp = {"ok": False, "msg": f"error: {e}"}
            self.wfile.write((json.dumps(resp, default=float) + "\n").encode())
            self.wfile.flush()

    def dispatch(self, req: dict, can_write: bool) -> dict:
        st, cmd = self.state, req.get("cmd")
        if cmd == "hello":
            return {"ok": True, "version": VERSION, "host": socket.gethostname(), "can_write": can_write,
                    "backends": [{"kind": b.kind, "name": b.name} for b in st.backends]}
        if cmd == "snapshot":
            with st.lock:
                return {"ok": True, "t": st.t, "poll_ms": round(st.poll_ms, 1),
                        "sensors": st.sensors, "controls": st.controls}
        if cmd == "guards":
            return {"ok": True, "guards": config.get().get("guards", []),
                    "log": list(getattr(st, "guard_log", []))}
        if cmd == "osd_get":
            with st.lock:
                return {"ok": True, "enabled": bool(st.osd.get("enabled")), "items": list(st.osd.get("items", [])),
                        "hotkey": "+".join(st.osd.get("hotkey", [])), "file": OSD_TXT}
        if cmd == "update_status":
            up = self.updater
            if not up.checking and time.time() - up.state["checked"] > UPDATE_EVERY:
                up.check()
            return {"ok": True, **up.status(), "can_update": up.may_start(getattr(self, "uid", -1))}
        if not can_write:
            return {"ok": False, "msg": f"permission denied (join group '{WRITE_GROUP}')"}
        if cmd == "update_check":
            self.updater.check()
            return {"ok": True}
        if cmd == "update_start":
            if not self.updater.may_start(getattr(self, "uid", -1)):
                return {"ok": False, "msg": "only the owner of the meat3 git checkout can update"}
            ok, msg = self.updater.start()
            print(f"meat3d: update_start -> {ok} {msg}")
            return {"ok": ok, "msg": msg}
        if cmd == "apply":
            cid = req.get("id")
            b = st.ctl_owner.get(cid)
            if b is None:
                return {"ok": False, "msg": f"unknown control {cid}"}
            ok, msg = b.apply(cid, req.get("value"))
            print(f"meat3d: apply {cid}={req.get('value')!r} -> {ok} {msg}")
            st.refresh_controls()
            return {"ok": ok, "msg": msg}
        if cmd == "reset_stats":
            with st.lock:
                st.stats.clear()
            return {"ok": True}
        if cmd == "osd_set":
            with st.lock:
                if "enabled" in req:
                    st.osd["enabled"] = bool(req["enabled"])
                if "items" in req:
                    st.osd["items"] = [str(x) for x in req["items"]]
            if "hotkey" in req:
                try:
                    keys = parse_hotkey(req["hotkey"])
                except ValueError as e:
                    return {"ok": False, "msg": f"hotkey: {e}"}
                with st.lock:
                    st.osd["hotkey"] = keys
                print(f"meat3d: hotkey -> {'+'.join(keys)}")
            st.save_osd(); st.write_osd()
            return {"ok": True}
        if cmd == "osd_toggle":
            return {"ok": True, "enabled": st.toggle_osd()}
        return {"ok": False, "msg": f"unknown cmd {cmd}"}


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


# ----------------------------------------------------------------------------- hotkey
def hotkey_thread(state: State):
    try:
        import evdev
        from evdev import ecodes
    except ImportError:
        print("meat3d: python-evdev missing; hotkey disabled")
        return
    import selectors
    sel = selectors.DefaultSelector()
    devs = {}
    combo, codes = None, []

    def arm():
        """(Re)read the combo from state; the GUI/TUI can change it at runtime (osd_set hotkey)."""
        nonlocal combo, codes
        combo = list(state.osd.get("hotkey") or ["KEY_RIGHTCTRL", "KEY_F12"])
        codes = [ecodes.ecodes[k] for k in combo if k in ecodes.ecodes]
        for d in list(devs.values()):
            try:
                sel.unregister(d)
                d.close()
            except (KeyError, ValueError, OSError):
                pass
        devs.clear()
        down.clear()

    def scan():
        if not codes:
            return
        for path in evdev.list_devices():
            if path in devs:
                continue
            try:
                d = evdev.InputDevice(path)
                keys = d.capabilities().get(ecodes.EV_KEY, [])
                if all(c in keys for c in codes):
                    devs[path] = d
                    sel.register(d, selectors.EVENT_READ)
            except OSError:
                pass

    down = set()
    arm()
    scan()
    print(f"meat3d: hotkey {'+'.join(combo)} on {len(devs)} keyboard(s)")
    last_scan = time.time()
    while True:
        if list(state.osd.get("hotkey") or []) != combo:
            arm()
            scan()
            last_scan = time.time()
            print(f"meat3d: hotkey now {'+'.join(combo)} on {len(devs)} keyboard(s)")
        for key, _ in sel.select(timeout=1):
            d = key.fileobj
            try:
                for ev in d.read():
                    if ev.type != ecodes.EV_KEY:
                        continue
                    if ev.value == 1:
                        down.add(ev.code)
                        if codes and ev.code == codes[-1] and all(c in down for c in codes):
                            state.toggle_osd()
                    elif ev.value == 0:
                        down.discard(ev.code)
            except OSError:   # device unplugged
                sel.unregister(d); devs.pop(d.path, None)
        if time.time() - last_scan > 10:
            scan(); last_scan = time.time()


# ----------------------------------------------------------------------------- apply_on_start
def _clear_marker() -> None:
    try:
        os.unlink(config.BOOT_APPLY_MARKER)
    except OSError:
        pass


def apply_on_start(state) -> bool:
    """Apply config 'apply_on_start' once, guarded against crash loops (see config.BOOT_APPLY_MARKER)."""
    items = config.get().get("apply_on_start") or {}
    if not items:
        return False
    if os.path.exists(config.BOOT_APPLY_MARKER):
        print(f"meat3d: NOT applying apply_on_start: the previous start applied it and did not run "
              f"{config.BOOT_APPLY_GRACE_S} s or stop cleanly (crash?). Fix the values, then delete "
              f"{config.BOOT_APPLY_MARKER} or re-save (meat3-cotune --save-boot).")
        return False
    os.makedirs(os.path.dirname(config.BOOT_APPLY_MARKER), exist_ok=True)
    with open(config.BOOT_APPLY_MARKER, "w") as f:
        f.write(f"{time.time()}\n")
        f.flush()
        os.fsync(f.fileno())
    for cid, val in items.items():
        b = state.ctl_owner.get(cid)
        ok, msg = b.apply(cid, val) if b is not None else (False, "unknown control")
        print(f"meat3d: apply_on_start {cid}={val}: {'ok' if ok else 'FAILED'} {msg}")
    t = threading.Timer(config.BOOT_APPLY_GRACE_S, _clear_marker)
    t.daemon = True
    t.start()
    return True


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(prog="meat3d")
    ap.add_argument("--socket", default=os.path.join(RUN_DIR, "meat3.sock"))
    ap.add_argument("--once", action="store_true", help="poll once, print sensors, exit")
    ap.add_argument("--no-hotkey", action="store_true")
    args = ap.parse_args()

    cfg = config.load()
    if cfg.get("amd_smu_controls", True):
        os.environ.setdefault("MEAT3_EXPERIMENTAL_SMU", "1")   # gate read by backends/amd_smu.py
    if os.geteuid() == 0:
        load_modules()
    backends = discover_backends()
    state = State(backends)
    state.refresh_controls()

    if args.once:
        state.poll(); time.sleep(1.0); state.poll()
        for s in state.sensors:
            v = s["value"]
            print(f"{s['group']:<55} {s['label']:<42} {'—' if v is None else round(v, s.get('decimals', 0))} {s['unit']}")
        print(f"\n{len(state.sensors)} sensors, {len(state.controls)} controls, poll {state.poll_ms:.0f} ms")
        return

    os.makedirs(RUN_DIR, exist_ok=True)
    os.chmod(RUN_DIR, 0o755)
    if os.path.exists(args.socket):
        os.unlink(args.socket)
    Handler.state = state
    srv = Server(args.socket, Handler)
    try:
        os.chown(args.socket, 0, grp.getgrnam(WRITE_GROUP).gr_gid)
        os.chmod(args.socket, 0o660)
    except (KeyError, PermissionError):
        os.chmod(args.socket, 0o666)   # dev mode: anyone may read; writes still checked per peer
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if not args.no_hotkey:
        threading.Thread(target=hotkey_thread, args=(state,), daemon=True).start()
    print(f"meat3d {VERSION}: {len(backends)} backends, socket {args.socket}")
    applied = apply_on_start(state)
    if applied:
        state.refresh_controls()

    def on_term(*_):
        if applied:
            _clear_marker()          # clean stop: the boot-apply values did not crash this boot
        for b in backends:
            try:
                b.close()
            except Exception:
                pass
        os._exit(0)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    interval = float(config.get().get("poll_interval_s", 1.0))
    n = 0
    while True:
        t0 = time.time()
        state.poll()
        n += 1
        if n % 5 == 0:
            state.refresh_controls()
        time.sleep(max(0.05, interval - (time.time() - t0)))


if __name__ == "__main__":
    main()
