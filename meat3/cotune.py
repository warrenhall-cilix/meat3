"""meat3-cotune: per-core Curve Optimizer tuner for AMD Ryzen (Zen 3/4/5 with ryzen_smu).

    sudo meat3-cotune                       # tune every core (quick: 60 s per step)
    sudo meat3-cotune --cores 0,4 --floor -40 --step-time 300
    sudo meat3-cotune --status              # show results / crash record so far
    sudo meat3-cotune --apply               # apply the saved results now
    sudo meat3-cotune --save-boot           # make meat3d apply the results at every boot
    sudo meat3-cotune --reset               # forget results (does not touch CO)

How it works: one core at a time, its CO margin is stepped more negative (others stay at their
starting values) and the core is stressed *alone* (single thread at full boost, plus a bursty
phase for boost transitions) with stress-ng result verification. A step fails on a verification
error, a non-zero stress-ng exit, or a kernel machine-check / segfault / GPF message. The last
passing value plus a safety margin is the result.

Crash safety: before every step the state file records which core/value is under test (fsync).
If the machine hangs or reboots, SMU CO resets to the BIOS values, and the next run finds the
in-progress record, marks that value unstable and continues. CO is applied through meat3d (the
daemon owns slot mapping and range checks).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from typing import Optional

STATE = "/var/lib/meat3/cotune.json"
SOCK = "/run/meat3/meat3.sock"
CTRL = "cpu0.smu.co_core{k}"
KMSG_BAD = re.compile(r"mce:|machine check|hardware error|segfault|general protection|"
                      r"\bMCE\b|watchdog: BUG: soft lockup|rcu_preempt detected stalls", re.I)
HEAVY_METHODS = ["matrixprod", "fft", "int64", "double", "prime", "int128decimal64", "jmp", "crc16"]


# ----------------------------------------------------------------------------- daemon client
class Daemon:
    def __init__(self, path: str = SOCK):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(10)
        self.s.connect(path)
        self.f = self.s.makefile("rw")

    def call(self, req: dict) -> dict:
        self.f.write(json.dumps(req) + "\n")
        self.f.flush()
        return json.loads(self.f.readline())

    def controls(self) -> dict:
        snap = self.call({"cmd": "snapshot"})
        return {c["id"]: c for c in snap.get("controls", [])}

    def sensors(self) -> dict:
        snap = self.call({"cmd": "snapshot"})
        return {x["id"]: x for x in snap.get("sensors", [])}

    def set_co(self, k: int, v: int) -> None:
        r = self.call({"cmd": "apply", "id": CTRL.format(k=k), "value": v})
        if not r.get("ok"):
            raise RuntimeError(f"CO core {k} = {v} rejected: {r.get('msg')}")


# ----------------------------------------------------------------------------- state
def load_state() -> dict:
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE)
    dfd = os.open(os.path.dirname(STATE), os.O_RDONLY)
    try:
        os.fsync(dfd)               # the in-progress record must survive a hard hang
    finally:
        os.close(dfd)


# ----------------------------------------------------------------------------- topology
def core_threads() -> list[list[int]]:
    """Physical cores in the same order meat3's backend numbers them (core k = CO core k)."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from meat3.backends._cpu_common import Topology
    return Topology("/sys").cores


# ----------------------------------------------------------------------------- stress + checks
class Kmsg:
    def __init__(self):
        self.fd = os.open("/dev/kmsg", os.O_RDONLY | os.O_NONBLOCK)
        os.lseek(self.fd, 0, os.SEEK_END)

    def bad_lines(self) -> list[str]:
        out = []
        while True:
            try:
                rec = os.read(self.fd, 8192).decode(errors="replace")
            except BlockingIOError:
                break
            except OSError:
                continue
            if not rec:
                break
            msg = rec.split(";", 1)[-1].strip()
            if KMSG_BAD.search(msg):
                out.append(msg[:200])
        return out

    def close(self):
        os.close(self.fd)


def stress(cpu: int, seconds: int, phase: str, method: str) -> tuple[bool, str]:
    if phase == "heavy":
        args = ["--cpu", "1", "--cpu-method", method, "--verify"]
    else:   # bursty: 35% duty cycle, many idle->boost transitions
        args = ["--cpu", "1", "--cpu-method", method, "--verify", "--cpu-load", "35"]
    cmd = ["stress-ng", *args, "--taskset", str(cpu), "-t", f"{seconds}s", "--metrics-brief"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 60)
    except subprocess.TimeoutExpired:
        return False, "stress-ng hung (timeout)"
    out = (r.stdout + r.stderr)
    # stress-ng logs errors at level "fail:" and summarises "failed: N" (N > 0 = verification errors)
    fails = [ln.strip() for ln in out.splitlines()
             if re.search(r"\bfail:|failed: *[1-9]", ln)]
    if r.returncode != 0:
        return False, f"stress-ng exit {r.returncode}: " + ("; ".join(fails[:2]) or out.strip()[-160:])
    if fails:
        return False, "; ".join(fails[:2])
    return True, ""


def test_step(cpu: int, step_time: int, km: Kmsg, log) -> tuple[bool, str]:
    """Heavy phases cycle through methods; then one bursty phase. ~step_time seconds total."""
    heavy_t = max(10, int(step_time * 0.75))
    burst_t = max(5, step_time - heavy_t)
    per = max(5, heavy_t // 3)
    t0 = time.time()
    i = int(t0) % len(HEAVY_METHODS)
    while time.time() - t0 < heavy_t:
        m = HEAVY_METHODS[i % len(HEAVY_METHODS)]
        ok, why = stress(cpu, per, "heavy", m)
        bad = km.bad_lines()
        if bad:
            return False, "kernel: " + bad[0]
        if not ok:
            return False, f"{m}: {why}"
        i += 1
    ok, why = stress(cpu, burst_t, "burst", "fft")
    bad = km.bad_lines()
    if bad:
        return False, "kernel: " + bad[0]
    if not ok:
        return False, f"bursty fft: {why}"
    return True, ""


# ----------------------------------------------------------------------------- tuning
def record_crash(st: dict) -> Optional[str]:
    cur = st.pop("current", None)
    if not cur:
        return None
    c = st.setdefault("cores", {}).setdefault(str(cur["core"]), {})
    c["fail"] = cur["value"]
    c["fail_reason"] = "system hung/rebooted/was interrupted during this step"
    save_state(st)
    return f"previous run stopped while testing core {cur['core']} at {cur['value']}: marked unstable"


def tune(args) -> int:
    d = Daemon()
    ctl = d.controls()
    cores = core_threads()
    if CTRL.format(k=0) not in ctl or not ctl[CTRL.format(k=0)].get("available"):
        print("per-core Curve Optimizer is not available (needs ryzen_smu and meat3d with amd_smu_controls).")
        return 2
    st = load_state()
    note = record_crash(st)
    if note:
        print("!!", note)
    st.setdefault("started", time.strftime("%Y-%m-%d %H:%M:%S"))
    st.setdefault("initial", {str(k): ctl[CTRL.format(k=k)].get("value") or 0 for k in range(len(cores))})
    st["params"] = {"floor": args.floor, "step": args.step, "step_time": args.step_time, "margin": args.margin}
    save_state(st)
    todo = [int(x) for x in args.cores.split(",")] if args.cores else list(range(len(cores)))
    # best silicon first (CPPC ranking) so an early result shows what the chip can do
    if not args.cores:
        sens = d.sensors()
        rank = {k: sens.get(f"cpu0.core{k}.cppc_rank", {}).get("value") for k in todo}
        todo.sort(key=lambda k: -(rank.get(k) or 0))
    km = Kmsg()
    stop = {"flag": False}

    def on_sig(*_):
        stop["flag"] = True
    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)
    try:
        for k in todo:
            c = st.setdefault("cores", {}).setdefault(str(k), {})
            if args.retune:
                c.clear()
            if c.get("done"):
                print(f"core {k}: already done (pass {c.get('pass')}, fail {c.get('fail')}); --retune to redo")
                continue
            cpu = cores[k][0]
            start = int(st["initial"].get(str(k), 0) or 0)
            v = c["pass"] - args.step if c.get("pass") is not None else start
            print(f"== core {k} (cpu {cpu}): from {v} down to {args.floor}, {args.step} per step, "
                  f"{args.step_time} s per step")
            c.setdefault("pass", None)
            while v >= args.floor and (c.get("fail") is None or v > c["fail"]):
                st["current"] = {"core": k, "value": v, "since": time.time()}
                save_state(st)
                d.set_co(k, v)
                t0 = time.time()
                ok, why = test_step(cpu, args.step_time, km, print)
                d.set_co(k, start)                          # back to start between steps
                st.pop("current", None)
                if stop["flag"]:
                    print("interrupted; this step is not counted")
                    save_state(st)
                    return 130
                if not ok:
                    c["fail"], c["fail_reason"] = v, why
                    print(f"   {v:4d}: FAIL - {why}")
                    save_state(st)
                    break
                c["pass"] = v
                print(f"   {v:4d}: PASS ({time.time() - t0:.0f} s)")
                save_state(st)
                v -= args.step
            c["done"] = True
            if c.get("pass") is not None:
                c["result"] = min(0, c["pass"] + args.margin) if c["pass"] < 0 else c["pass"]
            else:
                c["result"] = None                          # even the starting value failed
            save_state(st)
            print(f"   core {k}: best stable {c.get('pass')}, recommended {c.get('result')} "
                  f"(margin +{args.margin})")
    finally:
        km.close()
        for k in range(len(cores)):
            try:
                d.set_co(k, int(st["initial"].get(str(k), 0)))
            except Exception:
                pass
    print()
    show_status(st)
    print("\nNext: sudo meat3-cotune --apply   (then --verify-all, and --save-boot to keep it)")
    return 0


def results(st: dict) -> dict[int, int]:
    return {int(k): c["result"] for k, c in st.get("cores", {}).items() if c.get("result") is not None}


def show_status(st: dict) -> None:
    if not st:
        print("no tuning results yet")
        return
    if st.get("current"):
        cur = st["current"]
        print(f"!! a step was in progress (core {cur['core']} at {cur['value']}) - it will be marked unstable "
              f"on the next run")
    print(f"started {st.get('started')}  params {st.get('params')}")
    print(f"{'core':>4} {'start':>6} {'pass':>6} {'fail':>6} {'result':>7}  note")
    for k in sorted(st.get("cores", {}), key=int):
        c = st["cores"][k]
        print(f"{k:>4} {st.get('initial', {}).get(k, 0):>6} {str(c.get('pass')):>6} {str(c.get('fail')):>6} "
              f"{str(c.get('result')):>7}  {c.get('fail_reason', '')[:70]}")


def apply_results(st: dict) -> int:
    res = results(st)
    if not res:
        print("no results to apply")
        return 1
    d = Daemon()
    for k, v in sorted(res.items()):
        d.set_co(k, v)
        print(f"core {k}: CO {v}")
    return 0


def verify_all(minutes: float) -> int:
    n = os.cpu_count() or 1
    print(f"all-core verification: {n} threads, {minutes:g} min ...")
    km = Kmsg()
    try:
        r = subprocess.run(["stress-ng", "--cpu", str(n), "--cpu-method", "all", "--verify",
                            "-t", f"{int(minutes * 60)}s"], capture_output=True, text=True,
                           timeout=minutes * 60 + 120)
        bad = km.bad_lines()
    finally:
        km.close()
    if r.returncode != 0 or bad:
        print("FAIL:", bad[:1] or (r.stdout + r.stderr).strip()[-300:])
        return 1
    print("PASS")
    return 0


def save_boot(st: dict) -> int:
    res = results(st)
    if not res:
        print("no results to save")
        return 1
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from meat3.core import config
    cfg = config.load()
    aos = dict(cfg.get("apply_on_start") or {})
    for k, v in res.items():
        aos[CTRL.format(k=k)] = v
    cfg["apply_on_start"] = aos
    config.save(cfg)
    try:
        os.unlink(config.BOOT_APPLY_MARKER)
    except OSError:
        pass
    print(f"saved to {config.PATH} -> apply_on_start; meat3d applies it at start (skipped automatically "
          f"if a previous boot crashed within {config.BOOT_APPLY_GRACE_S // 60} min of applying it)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="meat3-cotune", description=__doc__.split("\n\n")[0])
    ap.add_argument("--cores", help="comma-separated core numbers (default: all, best CPPC rank first)")
    ap.add_argument("--floor", type=int, default=-30, help="most negative value to try (default -30, min -50)")
    ap.add_argument("--step", type=int, default=5, help="CO step (default 5)")
    ap.add_argument("--step-time", type=int, default=60, help="seconds of testing per step (default 60)")
    ap.add_argument("--margin", type=int, default=3, help="safety margin added to the last pass (default 3)")
    ap.add_argument("--retune", action="store_true", help="redo cores that are already done")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--verify-all", type=float, metavar="MIN", nargs="?", const=10.0)
    g.add_argument("--save-boot", action="store_true")
    g.add_argument("--reset", action="store_true")
    args = ap.parse_args(argv)
    if os.geteuid() != 0:
        print("run as root (sudo meat3-cotune): needs /dev/kmsg and the state file in /var/lib/meat3")
        return 2
    if args.floor < -50 or args.step < 1:
        print("--floor must be >= -50 and --step >= 1")
        return 2
    st = load_state()
    if args.status:
        show_status(st)
        return 0
    if args.reset:
        try:
            os.unlink(STATE)
        except OSError:
            pass
        print("results cleared")
        return 0
    if args.apply:
        return apply_results(st)
    if args.verify_all is not None:
        return verify_all(args.verify_all)
    if args.save_boot:
        return save_boot(st)
    return tune(args)


if __name__ == "__main__":
    sys.exit(main())
