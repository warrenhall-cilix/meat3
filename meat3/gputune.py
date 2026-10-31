"""meat3-gputune: automatic undervolt finder for AMD Radeon RX 7000/9000 (overdrive GFX voltage offset).

    sudo meat3-gputune                          # you run a game/benchmark; tuner steps the offset
    sudo meat3-gputune --load "vkcube --c 999999" --step-time 300
    sudo meat3-gputune --clock                  # then push the GFX clock offset up at that voltage
    sudo meat3-gputune --status | --apply | --save-boot | --reset

How it works: the GFX voltage offset (pp_od_clk_voltage, via meat3d) is stepped more negative,
10 mV at a time, while the GPU is under full load. A step only counts when the GPU really was busy
(average GFX activity >= --min-load %). A step fails on an amdgpu error in the kernel log (ring or
job timeout, GPU reset, page fault, hang), when the GPU stops reporting, or when the --load command
exits with an error. The last passing offset plus a safety margin is the result. Every step records
the average GFX clock, board power, PPT throttle duty and peak hotspot, so --status shows what the
undervolt actually bought.

--clock adds a second phase: with the undervolt applied, the GFX clock offset is raised 25 MHz at a
time (same pass/fail rules) and the last passing offset minus a margin becomes the clock result;
--apply and --save-boot then set both.

On RDNA4 a lower voltage offset lets the card hold higher clocks inside the same power limit, so a
good undervolt is usually faster as well as cooler. Use a demanding, real load: an unstable undervolt
often survives light scenes.

Crash safety: before every step the state file records the offset under test (fsync). If the machine
hangs or reboots, the driver comes back at stock and the next run marks that offset unstable.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from meat3.cotune import Daemon, Kmsg  # noqa: E402

STATE = "/var/lib/meat3/gputune.json"
PARAM = "oc.od.vddgfx_offset"
KMSG_GPU_BAD = re.compile(
    r"amdgpu.*(ring \S+ timeout|job timeout|GPU reset|gpu reset begin|soft recover|page fault|"
    r"PROTECTION_FAULT|GPU hang|IB test failed|\*ERROR\*.*(timeout|hang))|"
    r"\[drm\].*(GPU HANG|hang)", re.I)


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
        os.fsync(dfd)
    finally:
        os.close(dfd)


class GpuKmsg(Kmsg):
    """Kernel log watcher for amdgpu failures (and the CPU-side ones Kmsg already knows)."""

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
            if KMSG_GPU_BAD.search(msg):
                out.append(msg[:200])
        return out


def pick_gpu(ctl: dict, want: Optional[int]) -> Optional[str]:
    """Control id of the voltage offset on the requested (or first) AMD GPU that has it."""
    ids = sorted(k for k, c in ctl.items() if k.endswith("." + PARAM) and c.get("available"))
    if want is not None:
        ids = [k for k in ids if k.startswith(f"amdgpu{want}.")]
    return ids[0] if ids else None


class LoadRunner:
    """Keeps --load running; reports a failure if it exits non-zero."""

    def __init__(self, cmd: Optional[str]):
        self.cmd = shlex.split(cmd) if cmd else None
        self.p: Optional[subprocess.Popen] = None

    def ensure(self) -> Optional[str]:
        if not self.cmd:
            return None
        if self.p is not None:
            rc = self.p.poll()
            if rc is None:
                return None
            self.p = None
            if rc != 0:
                return f"load command exited with {rc}"
        self.p = subprocess.Popen(self.cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return None

    def stop(self):
        if self.p and self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(10)
            except subprocess.TimeoutExpired:
                self.p.kill()
        self.p = None


STATS = {"mhz": "gm.average_gfxclk_frequency", "w": "gm.average_socket_power",
         "hot": "gm.temperature_hotspot", "ppt_duty": "smu14.throttle.duty.power"}


def test_step(d, gpu: str, step_time: int, min_load: float, km, load: LoadRunner, log,
              sleep=time.sleep, clock=time.time) -> tuple[Optional[bool], str, dict]:
    """(True, '', stats) pass, (False, why, stats) fail, (None, why, stats) not counted (GPU wasn't loaded).
    stats: averages over the step of GFX activity %, GFX clock, board power, PPT duty; max hotspot."""
    act_id = f"{gpu}.gm.average_gfx_activity"
    samples: list[float] = []
    acc: dict[str, list[float]] = {k: [] for k in STATS}
    t0 = clock()

    def stats() -> dict:
        out = {"load": round(sum(samples) / len(samples), 1) if samples else None}
        for k, vals in acc.items():
            if not vals:
                out[k] = None
            elif k == "hot":
                out[k] = round(max(vals), 1)
            else:
                out[k] = round(sum(vals) / len(vals), 1 if k != "mhz" else 0)
        return out

    while clock() - t0 < step_time:
        why = load.ensure()
        if why:
            return False, why, stats()
        bad = km.bad_lines()
        if bad:
            return False, "kernel: " + bad[0], stats()
        try:
            s = d.sensors()
        except (OSError, ValueError) as e:
            return False, f"daemon unreachable ({e})", stats()
        a = s.get(act_id, {}).get("value")
        if a is None and act_id in s:
            return False, "GPU stopped reporting (reset/hang?)", stats()
        if a is not None:
            samples.append(float(a))
            for k, sid in STATS.items():
                v = s.get(f"{gpu}.{sid}", {}).get("value")
                if v is not None:
                    acc[k].append(float(v))
        sleep(2)
    bad = km.bad_lines()
    if bad:
        return False, "kernel: " + bad[0], stats()
    if samples and sum(samples) / len(samples) < min_load:
        return None, f"GPU only {sum(samples) / len(samples):.0f}% busy (need {min_load:.0f}%)", stats()
    return True, "", stats()


PHASES = {"volt": {"prefix": "", "unit": "mV", "label": "GFX voltage offset"},
          "clock": {"prefix": "clock_", "unit": "MHz", "label": "GFX clock offset"}}


def record_crash(st: dict) -> Optional[str]:
    cur = st.pop("current", None)
    if not cur:
        return None
    ph = PHASES[cur.get("kind", "volt")]
    k = ph["prefix"] + "fail"
    v = cur["value"]
    # voltage sweeps go down (a crash at v means anything <= v is bad), clock sweeps go up
    st[k] = v if st.get(k) is None else (max(st[k], v) if ph["prefix"] == "" else min(st[k], v))
    st[ph["prefix"] + "fail_reason"] = "system hung/rebooted/was interrupted during this step"
    save_state(st)
    return f"previous run stopped while testing {v} {ph['unit']}: marked unstable"


def fmt_stats(x: Optional[dict]) -> str:
    if not x:
        return ""
    parts = []
    if x.get("mhz") is not None:
        parts.append(f"{x['mhz']:.0f} MHz")
    if x.get("w") is not None:
        parts.append(f"{x['w']:.0f} W")
    if x.get("hot") is not None:
        parts.append(f"hot {x['hot']:.0f} °C")
    if x.get("ppt_duty") is not None:
        parts.append(f"PPT {x['ppt_duty']:.0f}%")
    if x.get("load") is not None:
        parts.append(f"{x['load']:.0f}% busy")
    return "  [" + ", ".join(parts) + "]" if parts else ""


def sweep(kind: str, st: dict, d, ctl_id: str, gpu: str, values: list[int], args, km, load, stop,
          sleep, clock) -> bool:
    """Test `values` in order under load; records <prefix>pass/fail/steps in st. False if interrupted."""
    ph = PHASES[kind]
    pre, unit = ph["prefix"], ph["unit"]
    steps = st.setdefault(pre + "steps", {})
    i = 0
    while i < len(values):
        v = values[i]
        st["current"] = {"value": v, "since": time.time(), "kind": kind}
        save_state(st)
        r = d.call({"cmd": "apply", "id": ctl_id, "value": v})
        if not r.get("ok"):
            print(f"   {v:5d}: driver rejected ({r.get('msg')}); stopping here")
            st.pop("current", None)
            break
        t0 = clock()
        ok, why, stats = test_step(d, gpu, args.step_time, args.min_load, km, load, print, sleep, clock)
        st.pop("current", None)
        if stop["flag"]:
            print("interrupted; this step is not counted")
            save_state(st)
            return False
        if ok is None:
            print(f"   {v:5d}: not counted - {why}; repeating")
            save_state(st)
            continue
        steps[str(v)] = dict(stats, ok=bool(ok))
        if not ok:
            st[pre + "fail"], st[pre + "fail_reason"] = v, why
            print(f"   {v:5d}: FAIL - {why}{fmt_stats(stats)}")
            save_state(st)
            break
        st[pre + "pass"] = v
        print(f"   {v:5d}: PASS ({clock() - t0:.0f} s){fmt_stats(stats)}")
        save_state(st)
        i += 1
    return True


def tune(args, d=None, km=None, load=None, sleep=time.sleep, clock=time.time) -> int:
    d = d or Daemon()
    ctl = d.controls()
    gpu_ctl = pick_gpu(ctl, args.gpu)
    if not gpu_ctl:
        print("no AMD GPU with an available GFX voltage offset (RDNA3/RDNA4 overdrive; "
              "needs amdgpu.ppfeaturemask with overdrive and meat3d running as root)")
        return 2
    c = ctl[gpu_ctl]
    gpu = gpu_ctl.split(".")[0]
    floor = max(args.floor, int(c.get("min") or args.floor))
    do_clock = bool(getattr(args, "clock", False))
    clk_ctl = f"{gpu}.oc.od.sclk_offset"
    cc = ctl.get(clk_ctl)
    if do_clock and not (cc and cc.get("available")):
        print(f"--clock: no available GFX clock offset control ({clk_ctl}) on this GPU")
        return 2
    st = load_state()
    if st.get("control") not in (None, gpu_ctl) or args.retune:
        st = {}
    note = record_crash(st)
    if note:
        print("!!", note)
    st["control"] = gpu_ctl
    st.setdefault("started", time.strftime("%Y-%m-%d %H:%M:%S"))
    st.setdefault("initial", int(c.get("value") or 0))
    st["params"] = {"floor": floor, "step": args.step, "step_time": args.step_time, "margin": args.margin,
                    "min_load": args.min_load, "load": args.load}
    if do_clock:
        st["clock_control"] = clk_ctl
        st.setdefault("clock_initial", int(cc.get("value") or 0))
        st["params"].update({"clock_step": args.clock_step, "clock_max": args.clock_max,
                             "clock_margin": args.clock_margin})
    start = st["initial"]
    km = km or GpuKmsg()
    load = load or LoadRunner(args.load)
    stop = {"flag": False}

    def on_sig(*_):
        stop["flag"] = True
    if threading_main():
        signal.signal(signal.SIGINT, on_sig)
        signal.signal(signal.SIGTERM, on_sig)
    if not args.load:
        print("Start a demanding game or benchmark now (full GPU load, looping), then leave it running.\n"
              "Steps only count while the GPU is at least "
              f"{args.min_load:.0f}% busy. Ctrl+C stops (the current step is not counted).")
    rc = 0
    try:
        # ---- phase 1: voltage offset, downwards ----
        if st.get("result") is None or not do_clock:
            st.pop("result", None)
            save_state(st)
            v0 = st["pass"] - args.step if st.get("pass") is not None else start
            vals = [v for v in range(v0, floor - 1, -args.step) if st.get("fail") is None or v > st["fail"]]
            print(f"== {gpu}: GFX voltage offset from {v0} mV down to {floor} mV, {args.step} mV per step, "
                  f"{args.step_time} s per step")
            if not sweep("volt", st, d, gpu_ctl, gpu, vals, args, km, load, stop, sleep, clock):
                return 130
            p = st.get("pass")
            st["result"] = None if p is None else (min(0, p + args.margin) if p < 0 else p)
            save_state(st)
        else:
            print(f"== {gpu}: keeping the undervolt result {st['result']} mV (use --retune to redo it)")
        # ---- phase 2: clock offset, upwards, at the undervolt ----
        if do_clock and st.get("result") is not None:
            st.pop("clock_result", None)
            save_state(st)
            r = d.call({"cmd": "apply", "id": gpu_ctl, "value": st["result"]})
            if not r.get("ok"):
                print(f"cannot apply the undervolt result for the clock phase: {r.get('msg')}")
                return 1
            top = min(args.clock_max, int(cc.get("max") or args.clock_max))
            c0 = st["clock_pass"] + args.clock_step if st.get("clock_pass") is not None \
                else st["clock_initial"] + args.clock_step
            vals = [v for v in range(c0, top + 1, args.clock_step)
                    if st.get("clock_fail") is None or v < st["clock_fail"]]
            print(f"== {gpu}: GFX clock offset from {c0} MHz up to {top} MHz at {st['result']} mV, "
                  f"{args.clock_step} MHz per step, {args.step_time} s per step")
            if not sweep("clock", st, d, clk_ctl, gpu, vals, args, km, load, stop, sleep, clock):
                return 130
            cp = st.get("clock_pass")
            st["clock_result"] = None if cp is None else max(st["clock_initial"], cp - args.clock_margin)
            save_state(st)
    finally:
        load.stop()
        km.close()
        for cid, val in ((gpu_ctl, start), (clk_ctl, st.get("clock_initial"))):
            if val is None or (cid == clk_ctl and not do_clock):
                continue
            try:
                d.call({"cmd": "apply", "id": cid, "value": val})
            except Exception:
                pass
    print()
    show_status(st)
    print("\nNext: sudo meat3-gputune --apply   (play for a while), then --save-boot to keep it")
    return rc


def threading_main() -> bool:
    import threading
    return threading.current_thread() is threading.main_thread()


def show_status(st: dict) -> None:
    if not st:
        print("no tuning results yet")
        return
    if st.get("current"):
        cur = st["current"]
        print(f"!! a step was in progress ({cur['value']} {PHASES[cur.get('kind', 'volt')]['unit']}) - "
              "it will be marked unstable on the next run")
    print(f"{st.get('control')}  started {st.get('started')}  params {st.get('params')}")
    print(f"voltage: start {st.get('initial')} mV, last pass {st.get('pass')} mV, first fail {st.get('fail')} mV "
          f"{('(' + st['fail_reason'][:70] + ')') if st.get('fail_reason') else ''}")
    print(f"recommended: {st.get('result')} mV")
    if st.get("clock_control"):
        print(f"clock:   start {st.get('clock_initial')} MHz, last pass {st.get('clock_pass')} MHz, "
              f"first fail {st.get('clock_fail')} MHz "
              f"{('(' + st['clock_fail_reason'][:70] + ')') if st.get('clock_fail_reason') else ''}")
        print(f"recommended: {st.get('clock_result')} MHz")
    for kind, ph in PHASES.items():
        steps = st.get(ph["prefix"] + "steps") or {}
        if steps:
            print(f"\n{ph['label']} steps ({ph['unit']}): value  result  clock  power  hotspot  PPT-duty  busy")
            for k in sorted(steps, key=lambda x: int(x), reverse=(kind == "volt")):
                x = steps[k]
                f = lambda key, fmt: (fmt % x[key]) if x.get(key) is not None else "-"   # noqa: E731
                print(f"   {int(k):6d}  {'PASS' if x.get('ok') else 'FAIL'}  {f('mhz', '%5.0f'):>6} "
                      f"{f('w', '%4.0f W'):>7} {f('hot', '%3.0f °C'):>7} {f('ppt_duty', '%3.0f%%'):>8} "
                      f"{f('load', '%3.0f%%'):>5}")


def _results(st: dict) -> list[tuple[str, int, str]]:
    out = []
    if st.get("result") is not None:
        out.append((st["control"], st["result"], "mV"))
    if st.get("clock_control") and st.get("clock_result") is not None:
        out.append((st["clock_control"], st["clock_result"], "MHz"))
    return out


def apply_result(st: dict, d=None) -> int:
    res = _results(st)
    if not res:
        print("no result to apply")
        return 1
    d = d or Daemon()
    rc = 0
    for cid, val, unit in res:
        r = d.call({"cmd": "apply", "id": cid, "value": val})
        print(f"{cid} = {val} {unit}: {r.get('msg')}")
        rc = rc or (0 if r.get("ok") else 1)
    return rc


def save_boot(st: dict) -> int:
    res = _results(st)
    if not res:
        print("no result to save")
        return 1
    from meat3.core import config
    cfg = config.load()
    aos = dict(cfg.get("apply_on_start") or {})
    for cid, val, _u in res:
        aos[cid] = val
    cfg["apply_on_start"] = aos
    config.save(cfg)
    print(f"saved to {config.PATH} -> apply_on_start (skipped automatically if a boot crashed "
          f"within {config.BOOT_APPLY_GRACE_S // 60} min of applying it)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="meat3-gputune", description=__doc__.split("\n\n")[0])
    ap.add_argument("--gpu", type=int, help="amdgpu index (default: first GPU with a voltage offset)")
    ap.add_argument("--floor", type=int, default=-200, help="most negative offset to try in mV (default -200)")
    ap.add_argument("--step", type=int, default=10, help="mV per step (default 10)")
    ap.add_argument("--step-time", type=int, default=300, help="seconds of load per step (default 300)")
    ap.add_argument("--margin", type=int, default=15, help="mV added back to the last pass (default 15)")
    ap.add_argument("--min-load", type=float, default=85.0, help="minimum average GFX activity %% (default 85)")
    ap.add_argument("--load", help="command that loads the GPU (restarted if it exits; non-zero exit = fail)")
    ap.add_argument("--retune", action="store_true", help="forget previous results and start over")
    ap.add_argument("--clock", action="store_true",
                    help="after the undervolt (or with an existing result), raise the GFX clock offset step by "
                         "step at that voltage and find the highest stable offset")
    ap.add_argument("--clock-step", type=int, default=25, help="MHz per clock step (default 25)")
    ap.add_argument("--clock-max", type=int, default=400, help="highest clock offset to try in MHz (default 400)")
    ap.add_argument("--clock-margin", type=int, default=25, help="MHz taken off the last passing clock (default 25)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--save-boot", action="store_true")
    g.add_argument("--reset", action="store_true")
    args = ap.parse_args(argv)
    if os.geteuid() != 0:
        print("run as root (sudo meat3-gputune): needs /dev/kmsg and /var/lib/meat3")
        return 2
    if args.step < 1 or args.floor > 0 or args.clock_step < 1 or args.clock_max < 0:
        print("--step and --clock-step must be >= 1, --floor <= 0, --clock-max >= 0")
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
        return apply_result(st)
    if args.save_boot:
        return save_boot(st)
    return tune(args)


if __name__ == "__main__":
    sys.exit(main())
