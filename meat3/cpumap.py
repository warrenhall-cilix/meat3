"""Static voltage/frequency map for meat3-bin --map: the lowest stable voltage of every core at fixed clocks.

For each core and each target clock:
  1. the frequency limit of that core is calibrated until the core really runs at the target
     (amd-pstate's limit isn't in real MHz);
  2. Curve Optimizer is stepped more negative for that core only. At each step the voltage the core
     actually runs at is measured, then the core runs the stress suite alone;
  3. the lowest measured voltage that passed is the result. Stock voltage minus that = margin.

The clock is held by the frequency limit, so Curve Optimizer only moves the voltage: the result is
a voltage at a known clock, comparable between cores, CCDs and chips. The CCDs' clock ceilings
(Fmax) don't get in the way, and nothing has to be set in the BIOS.

Stress suite per step (single thread, on the core under test):
  * light scalar loads (int32, fibonacci, collatz, queens) with result checks: they boost highest
    and find instability heavy loads miss;
  * heavy loads (matrixprod, fft, int128decimal64, ...) with result checks;
  * idle/wake: short loads separated by idle gaps, because Curve Optimizer failures often happen
    when a core wakes from a deep C-state;
  * a bursty phase (35% duty cycle) for load transitions.
A step fails on a wrong result, a stress-ng error, or a machine check / segfault / GPF / lockup
in the kernel log. Each step is written to disk before it runs: after a hang or reboot the next
run marks that step unstable and carries on (Curve Optimizer and frequency limits reset at boot).
"""
from __future__ import annotations

import statistics
import time
from typing import Callable, Optional

LIGHT_METHODS = ["int32", "fibonacci", "collatz", "queens"]
HEAVY_METHODS = ["matrixprod", "fft", "int128decimal64", "double", "int64", "prime"]
MAP_CLOCKS = "4500,5000,5300,max"
CO_PROBE = -15                 # mapping check: this CO on one core must lower that core's voltage
CO_PROBE_MIN_DROP_V = 0.015


def parse_clocks(text: str) -> list:
    out = []
    for t in (text or "").split(","):
        t = t.strip().lower()
        if t == "max":
            out.append("max")
        elif t:
            out.append(int(t))
    return out


def stress_suite(cpu: int, step_time: int, stress: Callable, kmsg_bad: Callable, sleep: Callable = time.sleep,
                 log: Callable = print) -> tuple[bool, str]:
    """~step_time seconds: light 35%, heavy 35%, idle/wake 15%, bursty 15%. (ok, reason)."""
    light_t, heavy_t = max(6, int(step_time * 0.35)), max(6, int(step_time * 0.35))
    wake_t, burst_t = max(4, int(step_time * 0.15)), max(4, int(step_time * 0.15))

    def check(ok, why, what):
        bad = kmsg_bad()
        if bad:
            return False, "kernel: " + bad[0]
        if not ok:
            return False, f"{what}: {why}"
        return True, ""

    for methods, total in ((LIGHT_METHODS, light_t), (HEAVY_METHODS, heavy_t)):
        per = max(3, total // 2)
        start = int(time.time()) % len(methods)
        for i in range(2):
            m = methods[(start + i) % len(methods)]
            ok, why = check(*stress(cpu, per, "heavy", m), m)
            if not ok:
                return False, why
    t_end = time.time() + wake_t                  # wake from idle repeatedly
    i = 0
    while time.time() < t_end:
        sleep(1.5)
        ok, why = check(*stress(cpu, 1, "heavy", LIGHT_METHODS[i % len(LIGHT_METHODS)]), "idle/wake")
        if not ok:
            return False, why
        i += 1
        if i > 50:
            break
    return check(*stress(cpu, burst_t, "burst", "fft"), "bursty fft")


class Mapper:
    """Runs the map for one chip. `binner` gives measure()/sys/restore(); `apply_co(k, v)` sets one core's CO;
    `suite(cpu)` runs the stress suite; `save()` persists res (fsync'd)."""

    def __init__(self, binner, res: dict, apply_co: Callable, suite: Callable, save: Callable,
                 step: int = 5, floor: int = -40, log: Callable = print):
        self.b, self.res, self.apply_co, self.suite, self.save = binner, res, apply_co, suite, save
        self.step, self.floor, self.log = step, floor, log
        self.m = res.setdefault("map", {"points": {}, "cap_ratio": {}})

    # ---- state
    def recover(self) -> Optional[str]:
        cur = self.m.pop("current", None)
        if not cur:
            return None
        p = self.m["points"].setdefault(cur["key"], {"passes": []})
        p["fail"], p["fail_reason"] = cur["co"], "system hung/rebooted/was interrupted during this step"
        self.save()
        return f"the last run stopped while testing core {cur['key']} at CO {cur['co']}: marked unstable"

    # ---- clock control
    def cpus_of(self, k: int) -> list[int]:
        return self.b.sys.cores[k]

    def quick_clock(self, k: int) -> Optional[float]:
        old = self.b.sample_s
        self.b.sample_s = 3
        try:
            return self.b.measure([k], [self.cpus_of(k)[0]]).get("clk")
        finally:
            self.b.sample_s = old

    def calibrate(self, k: int, target) -> tuple[Optional[int], Optional[float]]:
        """Frequency limit (kHz) at which core k runs `target` MHz (+-25), and the clock it runs."""
        cpus = self.cpus_of(k)
        top = max(self.b.orig_max[c] or 0 for c in cpus)
        if target == "max":
            self.b.sys.set_max_khz(cpus, top)
            return top, self.quick_clock(k)
        ccd = next((c["ccd"] for c in self.b.sys.ccds if k in c["cores"]), 0)
        ratio = self.m["cap_ratio"].get(str(ccd), 1.0)         # limit kHz per real MHz, learnt per CCD
        cap, clk = int(target * 1000 * ratio), None
        for _ in range(6):
            cap = min(cap, top)
            self.b.sys.set_max_khz(cpus, cap)
            clk = self.quick_clock(k)
            if clk is None:
                return None, None
            if abs(clk - target) <= 25:
                self.m["cap_ratio"][str(ccd)] = cap / 1000 / clk
                return cap, clk
            if cap >= top and clk < target:
                return None, clk                                 # can't reach the target clock
            cap = int(cap * target / clk)
        return cap, clk

    # ---- checks
    def check_mapping(self, probes: list[int], target: int = 4500) -> tuple[bool, str]:
        """Prove CO core k moves core k: voltage at a fixed clock must drop with CO_PROBE."""
        for k in probes:
            cap, clk = self.calibrate(k, target)
            if cap is None:
                return False, f"core {k}: couldn't hold {target} MHz"
            cpu = self.cpus_of(k)[0]
            try:
                v0 = self.b.measure([k], [cpu]).get("volt")
                self.apply_co(k, CO_PROBE)
                v1 = self.b.measure([k], [cpu]).get("volt")
            finally:
                self.apply_co(k, 0)
                self.b.restore()
            if v0 is None or v1 is None:
                return False, f"core {k}: no voltage reading"
            self.log(f"   mapping check core {k}: {v0:.3f} V at CO 0, {v1:.3f} V at CO {CO_PROBE}")
            if v0 - v1 < CO_PROBE_MIN_DROP_V:
                return False, (f"core {k}: CO {CO_PROBE} lowered its voltage by only {(v0 - v1) * 1000:.0f} mV; "
                               f"the Curve Optimizer core numbering may not match this chip")
        return True, ""

    # ---- the map
    def run(self, cores: list[int], clocks: list) -> None:
        for target in clocks:
            for k in cores:
                key = f"{k}@{target}"
                p = self.m["points"].setdefault(key, {"passes": []})
                if p.get("done"):
                    continue
                cap, clk = self.calibrate(k, target)
                if cap is None:
                    p.update(done=True, unreachable=True, clk=clk)
                    self.log(f"   core {k}: can't reach {target} MHz (runs {clk or 0:.0f}); skipped")
                    self.save()
                    continue
                cpu = self.cpus_of(k)[0]
                v = p["passes"][-1]["co"] - self.step if p["passes"] else 0
                self.log(f"== core {k} at {target if target == 'max' else str(target) + ' MHz'} "
                         f"(runs {clk:.0f} MHz): CO from {v} down to {self.floor}")
                try:
                    while v >= self.floor and (p.get("fail") is None or v > p["fail"]):
                        self.m["current"] = {"key": key, "co": v, "since": time.time()}
                        self.save()
                        self.apply_co(k, v)
                        meas = self.b.measure([k], [cpu])
                        ok, why = self.suite(cpu)
                        self.apply_co(k, 0)
                        self.m.pop("current", None)
                        if not ok:
                            p["fail"], p["fail_reason"] = v, why
                            self.log(f"   CO {v:4d}: FAIL - {why}")
                            self.save()
                            break
                        p["passes"].append({"co": v, "volt": meas.get("volt"), "clk": meas.get("clk"),
                                            "temp": meas.get("temp"), "power": meas.get("power")})
                        self.log(f"   CO {v:4d}: pass at {meas.get('clk') or 0:.0f} MHz, {meas.get('volt') or 0:.3f} V")
                        self.save()
                        v -= self.step
                finally:
                    self.apply_co(k, 0)
                    self.b.restore()
                p["done"] = True
                self.save()


# ----------------------------------------------------------------------------- results
def point_result(p: dict) -> dict:
    """stock V (CO 0), lowest stable V, margin (mV), clock, best CO."""
    passes = [x for x in p.get("passes", []) if x.get("volt")]
    if not passes:
        return {}
    stock = next((x for x in passes if x["co"] == 0), None)
    best = min(passes, key=lambda x: x["volt"])
    return {"stock_v": stock["volt"] if stock else None, "min_v": best["volt"], "co": best["co"],
            "clk": statistics.mean(x["clk"] for x in passes if x.get("clk")) if any(x.get("clk") for x in passes) else None,
            "margin_mv": (stock["volt"] - best["volt"]) * 1000 if stock else None,
            "floor_hit": p.get("fail") is None}


def ccd_map(res: dict, ccd: dict) -> dict:
    """target -> {'cores': {k: point_result}, 'worst_min_v', 'mean_min_v', 'mean_margin', 'worst_margin'}."""
    pts = (res.get("map") or {}).get("points", {})
    targets = []
    for key in pts:
        t = key.split("@", 1)[1]
        t = t if t == "max" else int(t)
        if t not in targets:
            targets.append(t)
    targets.sort(key=lambda t: (t == "max", 0 if t == "max" else t))
    out = {}
    for t in targets:
        cores = {k: point_result(pts.get(f"{k}@{t}", {})) for k in ccd["cores"]}
        cores = {k: r for k, r in cores.items() if r}
        if not cores:
            continue
        mins = [r["min_v"] for r in cores.values()]
        margins = [r["margin_mv"] for r in cores.values() if r.get("margin_mv") is not None]
        out[t] = {"cores": cores, "worst_min_v": max(mins), "mean_min_v": statistics.mean(mins),
                  "mean_margin": statistics.mean(margins) if margins else None,
                  "worst_margin": min(margins) if margins else None,
                  "clk": statistics.mean(r["clk"] for r in cores.values() if r.get("clk"))
                  if any(r.get("clk") for r in cores.values()) else None}
    return out


def report_lines(res: dict, ccd: dict) -> list[str]:
    m = ccd_map(res, ccd)
    if not m:
        return []
    L = ["   static V/F map: lowest stable voltage per core (margin below stock)"]
    for t, d in m.items():
        name = "max boost" if t == "max" else f"{t} MHz"
        cells = "  ".join(f"c{k} {r['min_v']:.3f}" + (f" (-{r['margin_mv']:.0f})" if r.get("margin_mv") else "")
                          + ("*" if r.get("floor_hit") else "") for k, r in d["cores"].items())
        L.append(f"     {name:>10} (runs {d['clk'] or 0:.0f}): worst core {d['worst_min_v']:.3f} V, "
                 f"mean {d['mean_min_v']:.3f} V, mean margin {d['mean_margin'] or 0:.0f} mV")
        L.append(f"       {cells}")
    if any(r.get("floor_hit") for d in m.values() for r in d["cores"].values()):
        L.append("       * still stable at the CO floor: the real minimum is lower")
    return L
