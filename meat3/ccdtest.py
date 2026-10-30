"""meat3-bin --allcore V: the highest clock each CCD holds with all its cores loaded, at a fixed voltage.

This is the number an all-core manual overclock runs at (cores in a CCD share one clock source, so the
clock is set per CCD). In manual mode, voltage fixed at V:

  per CCD   the other CCD held at 3000 MHz; every core of this CCD set to the same clock, from
            --cc-start in --cc-step steps (half a step after the first failure). Each step: check
            the cores run it, then y-cruncher (Zen 5), Prime95 AVX2 and Prime95 AVX-512 on every
            thread of the CCD, --cc-phase-s each. Lowest delivered voltage, peak temperature and
            package power are sampled during each test.
  chip      both CCDs at their results, all threads, y-cruncher then Prime95 AVX2 (--cc-chip-s
            each); a failure steps the faster CCD down 25 MHz.

A failure is a wrong result, a tool error, a machine check / segfault / GPF / lockup in the kernel log,
a crash (recorded before each step, marked on the next run), or manual mode being switched off under
the test (the overtemp guard): that last one is recorded as a thermal limit, never a pass.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

CCD_PHASES = ["ycruncher", "p95-avx2", "p95-avx512"]
CHIP_PHASES = ["ycruncher", "p95-avx2"]
OTHERS_MHZ = 3000


class Sampler:
    """Samples the daemon every few seconds during a test: lowest VDDCR, peak temperature/power, mean clock."""

    def __init__(self, sensors: Callable[[], dict], cores: list[int], prefix: str = "cpu0", every: float = 5.0,
                 groups: Optional[dict] = None):
        self.sensors, self.cores, self.p, self.every = sensors, cores, prefix, every
        self.groups = groups or {}                     # name -> cores: mean clock per group (per CCD)
        self.rows: list[dict] = []
        self._stop = threading.Event()
        self._t: Optional[threading.Thread] = None

    def _one(self) -> None:
        try:
            s = self.sensors()
        except Exception:
            return
        clks = [s.get(f"{self.p}.core{k}.clk_eff") for k in self.cores]
        clks = [c for c in clks if c]
        grp = {}
        for name, cores in self.groups.items():
            cs = [s.get(f"{self.p}.core{k}.clk_eff") for k in cores]
            cs = [c for c in cs if c]
            grp[name] = sum(cs) / len(cs) if cs else None
        self.rows.append({"vddcr": s.get(f"{self.p}.pmt.vddcr_cpu"), "tctl": s.get(f"{self.p}.k10temp.temp1"),
                          "ppt": s.get(f"{self.p}.pmt.ppt"), "clk": sum(clks) / len(clks) if clks else None,
                          "oc": s.get("_oc_mode"), "grp": grp})

    def _loop(self) -> None:
        while not self._stop.wait(self.every):
            self._one()

    def __enter__(self):
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        if self._t:
            self._t.join(timeout=self.every + 5)
        self._one()

    def summary(self) -> dict:
        g = lambda k: [r[k] for r in self.rows if r.get(k) is not None]
        ccd = {}
        for name in self.groups:
            xs = [r["grp"].get(name) for r in self.rows[1:] or self.rows if r.get("grp", {}).get(name)]
            ccd[name] = sum(xs) / len(xs) if xs else None    # skip the first sample: load still ramping
        return {"vddcr_min": min(g("vddcr")) if g("vddcr") else None, "tctl_max": max(g("tctl")) if g("tctl") else None,
                "ppt_max": max(g("ppt")) if g("ppt") else None,
                "clk_mean": sum(g("clk")) / len(g("clk")) if g("clk") else None, "ccd_clk": ccd}


class CcdTest:
    """oc: mode/vid/freq_all/freq_core (DaemonOc); oc_active() -> bool reads the daemon's manual-mode state;
    run_phase(phase, cpus, seconds); kmsg_bad(); measure(cores, cpus) -> {'clk', ...};
    sampler(cores) -> Sampler-like context manager; save()."""

    def __init__(self, res: dict, volt: float, sysfs, oc, oc_active: Callable, run_phase: Callable,
                 kmsg_bad: Callable, measure: Callable, sampler: Callable, save: Callable,
                 start: int = 5000, step: int = 100, phase_s: int = 180, chip_s: int = 600,
                 log: Callable = print):
        self.res, self.volt, self.sys, self.oc, self.oc_active = res, volt, sysfs, oc, oc_active
        self.run_phase, self.kmsg_bad, self.measure, self.sampler, self.save = run_phase, kmsg_bad, measure, sampler, save
        self.start, self.step, self.phase_s, self.chip_s, self.log = start, step, phase_s, chip_s, log
        self.tag = f"{volt:.3f}V"
        self.r = res.setdefault("map", {}).setdefault("allcore", {}).setdefault(self.tag, {"ccds": {}, "chip": {}})

    # ---- state
    def recover(self) -> Optional[str]:
        cur = self.r.pop("current", None)
        if not cur:
            return None
        if cur.get("chip"):
            st = self.r["chip"]
            st.setdefault("fails", []).append(dict(cur["clocks"]))
            n = max(cur["clocks"], key=lambda x: cur["clocks"][x])
            st.setdefault("clocks", dict(cur["clocks"]))[n] = cur["clocks"][n] - 25
            st["phases"] = {}
            where = "the whole-chip test"
        else:
            st = self.r["ccds"].setdefault(str(cur["ccd"]), {"passes": []})
            st["fail"], st["fail_reason"] = cur["mhz"], "system hung/rebooted/was interrupted during this step"
            where = f"CCD{cur['ccd']} at {cur['mhz']} MHz"
        self.save()
        return f"the last run stopped during {where} ({cur.get('phase')}): marked unstable"

    def _cpus(self, ccd: dict) -> list[int]:
        return sorted(c for k in ccd["cores"] for c in self.sys.cores[k])

    def _set_ccd(self, ccd: dict, mhz: int) -> None:
        for k in ccd["cores"]:
            self.oc.freq_core(k, mhz)

    def _phases(self, phases: list[str], cpus: list[int], cores: list[int], seconds: int, cur: dict,
                done: dict) -> tuple[Optional[tuple], dict]:
        """Run the phases not yet passed; (failure or None, combined samples)."""
        worst = {}
        for ph in phases:
            if done.get(ph) == "pass":
                continue
            self.r["current"] = dict(cur, phase=ph)
            self.save()
            t0 = time.time()
            with self.sampler(cores) as smp:
                ok, why = self.run_phase(ph, cpus, seconds)
            self.r.pop("current", None)
            bad = self.kmsg_bad()
            sm = smp.summary()
            for key, fn in (("vddcr_min", min), ("tctl_max", max), ("ppt_max", max)):
                if sm.get(key) is not None:
                    worst[key] = fn(worst.get(key, sm[key]), sm[key])
            if not self.oc_active():
                return ("thermal", f"manual mode was switched off during {ph} (overtemp guard?), "
                                   f"peak {sm.get('tctl_max') or 0:.0f} °C"), worst
            if bad:
                return (ph, "kernel: " + bad[0]), worst
            if not ok:
                return (ph, why), worst
            done[ph] = "pass"
            self.log(f"      {ph:10} pass ({time.time() - t0:.0f} s; {why}; VDDCR min {sm.get('vddcr_min') or 0:.3f} V, "
                     f"{sm.get('tctl_max') or 0:.0f} °C, {sm.get('ppt_max') or 0:.0f} W)")
            self.save()
        return None, worst

    # ---- one CCD
    def ccd(self, ccd: dict) -> None:
        st = self.r["ccds"].setdefault(str(ccd["ccd"]), {"passes": []})
        if st.get("done"):
            return
        cpus = self._cpus(ccd)
        f = (st["passes"][-1]["mhz"] + (self.step if st.get("fail") is None else self.step // 2)) \
            if st["passes"] else self.start
        self.log(f"== CCD{ccd['ccd']} ({ccd.get('role') or 'CCD'}), all {len(cpus)} threads at {self.volt:.3f} V, "
                 f"from {f} MHz")
        for other in self.sys.ccds:
            if other is not ccd:
                self._set_ccd(other, OTHERS_MHZ)
        while True:
            if st.get("fail") is not None and f >= st["fail"]:
                break
            cur = {"ccd": ccd["ccd"], "mhz": f}
            self.r["current"] = dict(cur, phase="clock check")
            self.save()
            self._set_ccd(ccd, f)
            m = self.measure(ccd["cores"], cpus)
            self.r.pop("current", None)
            if m.get("clk") is None or abs(m["clk"] - f) > 75:
                st["ceiling"] = f
                self.log(f"   {f} MHz: cores run {m.get('clk') or 0:.0f} MHz instead; stopping")
                break
            self.log(f"   {f} MHz (cores run {m['clk']:.0f})")
            done: dict = {}
            failed, worst = self._phases(CCD_PHASES, cpus, ccd["cores"], self.phase_s, cur, done)
            if failed:
                st["fail"], st["fail_reason"] = f, f"{failed[0]}: {failed[1]}"
                if failed[0] == "thermal":
                    st["thermal"] = True
                self.log(f"   {f} MHz: FAIL - {failed[0]}: {failed[1]}")
                self.save()
                if failed[0] != "thermal" and st["passes"] and f - st["passes"][-1]["mhz"] > self.step // 2:
                    f = st["passes"][-1]["mhz"] + self.step // 2
                    continue
                break
            st["passes"].append({"mhz": f, "clk": m["clk"], **worst})
            self.save()
            f += self.step if st.get("fail") is None else self.step // 2
        self._set_ccd(ccd, OTHERS_MHZ)
        st["done"] = True
        st["result_mhz"] = st["passes"][-1]["mhz"] if st["passes"] else None
        self.log(f"== CCD{ccd['ccd']}: {st['result_mhz']} MHz all-core at {self.volt:.3f} V")
        self.save()

    # ---- whole chip
    def chip(self) -> None:
        st = self.r["chip"]
        if st.get("done"):
            return
        res = {str(c["ccd"]): self.r["ccds"].get(str(c["ccd"]), {}).get("result_mhz") for c in self.sys.ccds}
        if not all(res.values()):
            self.log("   not every CCD has a result; whole-chip test skipped")
            return
        clocks = st.setdefault("clocks", dict(res))
        cpus = sorted(c for cl in self.sys.cores for c in cl)
        cores = list(range(len(self.sys.cores)))
        while True:
            for c in self.sys.ccds:
                self._set_ccd(c, clocks[str(c["ccd"])])
            self.log("== whole chip: " + ", ".join(f"CCD{n} {mhz} MHz" for n, mhz in clocks.items()))
            failed, worst = self._phases(CHIP_PHASES, cpus, cores, self.chip_s, {"chip": True, "clocks": dict(clocks)},
                                         st.setdefault("phases", {}))
            if failed is None:
                st.update(done=True, result=dict(clocks), **worst)
                self.save()
                return
            st.setdefault("fails", []).append(dict(clocks))
            self.log(f"   whole chip FAIL - {failed[0]}: {failed[1]}")
            if failed[0] == "thermal":
                st.update(done=True, thermal=True)
                self.save()
                return
            n = max(clocks, key=lambda x: clocks[x])
            clocks[n] -= 25
            st["phases"] = {}
            self.save()

    def run(self, ccds: list[dict]) -> None:
        self.oc.mode(True)
        try:
            self.oc.freq_all(OTHERS_MHZ)
            self.oc.vid(self.volt)
            for c in ccds:
                self.ccd(c)
            self.chip()
        finally:
            self.oc.mode(False)


def report_lines(res: dict, ccd: dict) -> list[str]:
    L = []
    for tag, r in ((res.get("map") or {}).get("allcore") or {}).items():
        st = r.get("ccds", {}).get(str(ccd["ccd"]), {})
        if not st.get("passes") and not st.get("fail"):
            continue
        last = st["passes"][-1] if st.get("passes") else {}
        L.append(f"   all-core at {tag}: {st.get('result_mhz') or (last.get('mhz') or '—')} MHz"
                 + (f" (failed {st['fail']}: {st.get('fail_reason', '')[:70]})" if st.get("fail") else "")
                 + (f"; under load VDDCR {last['vddcr_min']:.3f} V, {last.get('tctl_max') or 0:.0f} °C, "
                    f"{last.get('ppt_max') or 0:.0f} W" if last.get("vddcr_min") else ""))
        chip = r.get("chip", {})
        if chip.get("result"):
            L.append(f"       whole chip loaded: {chip['result'].get(str(ccd['ccd']))} MHz on this CCD"
                     + (f" ({chip.get('tctl_max') or 0:.0f} °C, {chip.get('ppt_max') or 0:.0f} W)" if chip.get("tctl_max") else ""))
    return L
