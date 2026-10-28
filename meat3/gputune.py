"""meat3-gputune: automatic undervolt finder for AMD Radeon RX 7000/9000 (overdrive GFX voltage offset).

    sudo meat3-gputune                          # you run a game/benchmark; tuner steps the offset
    sudo meat3-gputune --load "vkcube --c 999999" --step-time 300
    sudo meat3-gputune --status | --apply | --save-boot | --reset

How it works: the GFX voltage offset (pp_od_clk_voltage, via meat3d) is stepped more negative,
10 mV at a time, while the GPU is under full load. A step only counts when the GPU really was busy
(average GFX activity >= --min-load %). A step fails on an amdgpu error in the kernel log (ring or
job timeout, GPU reset, page fault, hang), when the GPU stops reporting, or when the --load command
exits with an error. The last passing offset plus a safety margin is the result.

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


def test_step(d, gpu: str, step_time: int, min_load: float, km, load: LoadRunner, log,
              sleep=time.sleep, clock=time.time) -> tuple[Optional[bool], str]:
    """(True, '') pass, (False, why) fail, (None, why) not counted (GPU wasn't loaded)."""
    act_id = f"{gpu}.gm.average_gfx_activity"
    samples: list[float] = []
    t0 = clock()
    while clock() - t0 < step_time:
        why = load.ensure()
        if why:
            return False, why
        bad = km.bad_lines()
        if bad:
            return False, "kernel: " + bad[0]
        try:
            s = d.sensors()
        except (OSError, ValueError) as e:
            return False, f"daemon unreachable ({e})"
        a = s.get(act_id, {}).get("value")
        if a is None and act_id in s:
            return False, "GPU stopped reporting (reset/hang?)"
        if a is not None:
            samples.append(float(a))
        sleep(2)
    bad = km.bad_lines()
    if bad:
        return False, "kernel: " + bad[0]
    if samples and sum(samples) / len(samples) < min_load:
        return None, f"GPU only {sum(samples) / len(samples):.0f}% busy (need {min_load:.0f}%)"
    return True, ""


def record_crash(st: dict) -> Optional[str]:
    cur = st.pop("current", None)
    if not cur:
        return None
    st["fail"] = cur["value"] if st.get("fail") is None else max(st["fail"], cur["value"])
    st["fail_reason"] = "system hung/rebooted/was interrupted during this step"
    save_state(st)
    return f"previous run stopped while testing {cur['value']} mV: marked unstable"


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
    st.pop("result", None)
    save_state(st)
    start = st["initial"]
    v = st["pass"] - args.step if st.get("pass") is not None else start
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
    print(f"== {gpu}: GFX voltage offset from {v} mV down to {floor} mV, {args.step} mV per step, "
          f"{args.step_time} s per step")
    try:
        while v >= floor and (st.get("fail") is None or v > st["fail"]):
            st["current"] = {"value": v, "since": time.time()}
            save_state(st)
            r = d.call({"cmd": "apply", "id": gpu_ctl, "value": v})
            if not r.get("ok"):
                print(f"   {v:5d}: driver rejected ({r.get('msg')}); stopping here")
                st.pop("current", None)
                break
            t0 = clock()
            ok, why = test_step(d, gpu, args.step_time, args.min_load, km, load, print, sleep, clock)
            st.pop("current", None)
            if stop["flag"]:
                print("interrupted; this step is not counted")
                save_state(st)
                return 130
            if ok is None:
                print(f"   {v:5d}: not counted - {why}; repeating")
                save_state(st)
                continue
            if not ok:
                st["fail"], st["fail_reason"] = v, why
                print(f"   {v:5d}: FAIL - {why}")
                save_state(st)
                break
            st["pass"] = v
            print(f"   {v:5d}: PASS ({clock() - t0:.0f} s)")
            save_state(st)
            v -= args.step
        p = st.get("pass")
        st["result"] = None if p is None else (min(0, p + args.margin) if p < 0 else p)
        save_state(st)
    finally:
        load.stop()
        km.close()
        try:
            d.call({"cmd": "apply", "id": gpu_ctl, "value": start})
        except Exception:
            pass
    print()
    show_status(st)
    print("\nNext: sudo meat3-gputune --apply   (play for a while), then --save-boot to keep it")
    return 0


def threading_main() -> bool:
    import threading
    return threading.current_thread() is threading.main_thread()


def show_status(st: dict) -> None:
    if not st:
        print("no tuning results yet")
        return
    if st.get("current"):
        print(f"!! a step was in progress ({st['current']['value']} mV) - it will be marked unstable on the next run")
    print(f"{st.get('control')}  started {st.get('started')}  params {st.get('params')}")
    print(f"start {st.get('initial')} mV, last pass {st.get('pass')} mV, first fail {st.get('fail')} mV "
          f"{('(' + st['fail_reason'][:70] + ')') if st.get('fail_reason') else ''}")
    print(f"recommended: {st.get('result')} mV")


def apply_result(st: dict, d=None) -> int:
    if st.get("result") is None:
        print("no result to apply")
        return 1
    d = d or Daemon()
    r = d.call({"cmd": "apply", "id": st["control"], "value": st["result"]})
    print(f"{st['control']} = {st['result']} mV: {r.get('msg')}")
    return 0 if r.get("ok") else 1


def save_boot(st: dict) -> int:
    if st.get("result") is None:
        print("no result to save")
        return 1
    from meat3.core import config
    cfg = config.load()
    aos = dict(cfg.get("apply_on_start") or {})
    aos[st["control"]] = st["result"]
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
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--save-boot", action="store_true")
    g.add_argument("--reset", action="store_true")
    args = ap.parse_args(argv)
    if os.geteuid() != 0:
        print("run as root (sudo meat3-gputune): needs /dev/kmsg and /var/lib/meat3")
        return 2
    if args.step < 1 or args.floor > 0:
        print("--step must be >= 1 and --floor <= 0")
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
