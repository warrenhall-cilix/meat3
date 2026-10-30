"""meat3-bin --co-allcore: PBO + Curve Optimizer per CCD, tuned for all-core load, then checked per core.

Precision Boost stays on (no manual mode). Under a sustained all-core load a Ryzen 9000 is usually held
back by its voltage limit, not power or temperature; negative Curve Optimizer lowers the voltage each
clock needs, so the same limit allows a higher clock.

  limits   PPT/TDC/EDC raised for the test (--pbo-ppt/--pbo-tdc/--pbo-edc) so power isn't the limit;
           stock values are put back at the end
  sweep    one CCD at a time (the other at 0, or at its result once found): every core of the CCD at
           the same CO, 0, -step, ...; at each step the whole chip runs y-cruncher then Prime95 AVX2
           (--co-phase-s each) and the per-CCD all-core clocks, power, temperature and VDDCR are logged.
           The first failure ends that CCD's sweep; the result is the last passing value.
  check    each core alone, at the chosen values, runs the quick stress suite (light, heavy, idle/wake,
           bursty): negative CO usually fails at single-thread boost, not all-core. A failing core puts
           its CCD's value up by `step`, then that CCD's cores are checked again.
The step about to run is saved before CO is applied; after a crash the next run marks it unstable.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

PHASES = ["ycruncher", "p95-avx2"]


class CoAllcore:
    """apply_co(core, value); set_limits(dict)/stock_limits; run_phase(phase, cpus, s); kmsg_bad();
    sampler(cores) -> context manager with summary(); clocks() -> {ccd: mean all-core MHz} read during
    load; suite(cpu) -> (ok, why) for the single-core check; save()."""

    def __init__(self, res: dict, sysfs, apply_co: Callable, set_limits: Callable, test_limits: dict,
                 stock_limits: dict, run_phase: Callable, kmsg_bad: Callable, sampler: Callable,
                 suite: Callable, save: Callable, step: int = 5, floor: int = -40, phase_s: int = 300,
                 log: Callable = print):
        self.res, self.sys, self.apply_co, self.set_limits = res, sysfs, apply_co, set_limits
        self.test_limits, self.stock_limits = test_limits, stock_limits
        self.run_phase, self.kmsg_bad, self.sampler, self.suite, self.save = run_phase, kmsg_bad, sampler, suite, save
        self.step, self.floor, self.phase_s, self.log = step, floor, phase_s, log
        self.r = res.setdefault("map", {}).setdefault("co_allcore", {"ccds": {}, "steps": [], "check": {}})

    # ---- state
    def recover(self) -> Optional[str]:
        cur = self.r.pop("current", None)
        if not cur:
            return None
        if cur.get("check_core") is not None:
            n = str(cur["ccd"])
            st = self.r["ccds"].setdefault(n, {})
            st["co"] = min(0, cur["co"] + self.step)
            st.setdefault("check_fails", []).append({"core": cur["check_core"], "co": cur["co"],
                                                     "why": "system hung/rebooted during the single-core check"})
            self.r["check"] = {}
        else:
            st = self.r["ccds"].setdefault(str(cur["ccd"]), {"passes": []})
            st["fail"], st["fail_reason"] = cur["co"], "system hung/rebooted/was interrupted during this step"
        self.save()
        return f"the last run stopped at CCD{cur['ccd']} CO {cur['co']} ({cur.get('phase')}): marked unstable"

    def _apply_ccd(self, ccd: dict, v: int) -> None:
        for k in ccd["cores"]:
            self.apply_co(k, v)

    def _values(self) -> dict:
        return {str(c["ccd"]): self.r["ccds"].get(str(c["ccd"]), {}).get("co", 0) for c in self.sys.ccds}

    def _apply_all(self, values: dict) -> None:
        for c in self.sys.ccds:
            self._apply_ccd(c, values[str(c["ccd"])])

    # ---- all-core sweep
    def load_step(self, values: dict, cur: dict) -> tuple[Optional[str], dict]:
        cpus = sorted(c for cl in self.sys.cores for c in cl)
        out = {}
        for ph in PHASES:
            self.r["current"] = dict(cur, phase=ph)
            self.save()
            with self.sampler(list(range(len(self.sys.cores)))) as smp:
                ok, why = self.run_phase(ph, cpus, self.phase_s)
            self.r.pop("current", None)
            bad = self.kmsg_bad()
            sm = smp.summary()
            out[ph] = sm
            if bad:
                return f"{ph}: kernel: {bad[0]}", out
            if not ok:
                return f"{ph}: {why}", out
            self.log(f"      {ph:10} pass; " + "  ".join(f"CCD{n} {mhz:.0f} MHz" for n, mhz in (sm.get("ccd_clk") or {}).items())
                     + f"  {sm.get('ppt_max') or 0:.0f} W  {sm.get('tctl_max') or 0:.0f} °C  VDDCR {sm.get('vddcr_min') or 0:.3f} V")
            self.save()
        return None, out

    def sweep(self, ccd: dict) -> None:
        n = str(ccd["ccd"])
        st = self.r["ccds"].setdefault(n, {"passes": []})
        if st.get("swept"):
            return
        v = st["passes"][-1]["co"] - self.step if st["passes"] else 0
        self.log(f"== CCD{n} ({ccd.get('role') or 'CCD'}): Curve Optimizer from {v} down to {self.floor}, all-core load")
        while v >= self.floor and (st.get("fail") is None or v > st["fail"]):
            values = self._values()
            values[n] = v
            cur = {"ccd": ccd["ccd"], "co": v}
            self.r["current"] = dict(cur, phase="apply")
            self.save()
            self._apply_all(values)
            self.log(f"   CCD{n} CO {v} (" + ", ".join(f"CCD{k} {x}" for k, x in values.items()) + ")")
            failed, out = self.load_step(values, cur)
            if failed:
                st["fail"], st["fail_reason"] = v, failed
                self.log(f"   CCD{n} CO {v}: FAIL - {failed}")
                self.save()
                break
            st["passes"].append({"co": v, "values": dict(values), "load": out})
            st["co"] = v
            self.save()
            v -= self.step
        st["swept"] = True
        st.setdefault("co", 0)
        self._apply_ccd(ccd, st["co"])
        self.log(f"== CCD{n}: {st['co']} under all-core load")
        self.save()

    # ---- single-core check
    def check(self) -> None:
        while True:
            values = self._values()
            self._apply_all(values)
            failed_ccd = None
            for c in self.sys.ccds:
                n = str(c["ccd"])
                for k in c["cores"]:
                    key = f"{k}@{values[n]}"
                    if self.r["check"].get(key) == "pass":
                        continue
                    if values[n] == 0:
                        self.r["check"][key] = "pass"
                        continue
                    self.r["current"] = {"ccd": c["ccd"], "co": values[n], "check_core": k, "phase": "single-core check"}
                    self.save()
                    ok, why = self.suite(self.sys.cores[k][0])
                    bad = self.kmsg_bad()
                    self.r.pop("current", None)
                    if bad:
                        ok, why = False, "kernel: " + bad[0]
                    if not ok:
                        st = self.r["ccds"][n]
                        st.setdefault("check_fails", []).append({"core": k, "co": values[n], "why": why})
                        st["co"] = min(0, values[n] + self.step)
                        self.log(f"   core {k} (CCD{n}) at CO {values[n]}: FAIL - {why}; CCD{n} -> {st['co']}")
                        self.save()
                        failed_ccd = n
                        break
                    self.r["check"][key] = "pass"
                    self.log(f"   core {k} (CCD{n}) at CO {values[n]}: pass")
                    self.save()
                if failed_ccd:
                    break
            if not failed_ccd:
                self.r["result"] = self._values()
                self.save()
                return

    def baseline(self) -> None:
        if self.r.get("baseline"):
            return
        values = {str(c["ccd"]): 0 for c in self.sys.ccds}
        self._apply_all(values)
        self.log("== baseline: Curve Optimizer 0, raised PBO limits, all-core load")
        failed, out = self.load_step(values, {"ccd": -1, "co": 0})
        self.r["baseline"] = {"failed": failed, "load": out}
        self.save()

    def run(self) -> None:
        self.set_limits(self.test_limits)
        try:
            self.baseline()
            for c in self.sys.ccds:
                self.sweep(c)
            self.check()
            self.log("== result: " + ", ".join(f"CCD{n} CO {v}" for n, v in self.r["result"].items()))
        finally:
            self.set_limits(self.stock_limits)


def report_lines(res: dict, ccd: dict) -> list[str]:
    r = (res.get("map") or {}).get("co_allcore") or {}
    st = r.get("ccds", {}).get(str(ccd["ccd"]))
    if not st:
        return []
    n = str(ccd["ccd"])
    base = (((r.get("baseline") or {}).get("load") or {}).get("ycruncher") or {}).get("ccd_clk", {}).get(n)
    best = st["passes"][-1] if st.get("passes") else None
    tuned = ((best or {}).get("load", {}).get("ycruncher") or {}).get("ccd_clk", {}).get(n) if best else None
    L = [f"   PBO + Curve Optimizer (all-core): CCD{n} CO {(r.get('result') or {}).get(n, st.get('co'))}"
         + (f" (all-core load held {best['co']}; failed {st['fail']})" if best and st.get("fail") is not None else "")]
    if base or tuned:
        L.append(f"       y-cruncher all-core clock: {base or 0:.0f} MHz at CO 0 -> {tuned or 0:.0f} MHz tuned")
    for f in st.get("check_fails", []):
        L.append(f"       single-core check: core {f['core']} failed at CO {f['co']} ({f['why'][:60]})")
    return L
