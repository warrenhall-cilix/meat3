"""meat3-bin --validate: prove the clocks the screening found, with Prime95 and y-cruncher, in manual mode.

Starts from each core's highest passing clock in `--at-voltage V --manual` (the screening) and runs, on
that core alone (every other core held low, voltage fixed at V):

  p95-sse     Prime95 torture, small FFTs (4-64K, in L1/L2), SSE only: highest boost, finds the most
  p95-avx2    same FFTs, AVX2 + FMA3
  p95-avx512  same FFTs, AVX-512: most current per core, most voltage sag
  p95-l3      Prime95 large FFTs (448K-8M, ~4-64 MB): lives in L3; matters for the V-Cache CCD
  ycruncher   y-cruncher's Zen 5 build (24-ZN5), component stress tests, results checked

A failure (a wrong result, a tool error, a machine check / segfault / GPF / lockup in the kernel log,
or a crash) steps that core down `--val-step` MHz and starts its phases again. Results are saved after
every phase; after a crash the next run marks the clock unstable and carries on, as the screening does.

Then the whole chip: each CCD runs at the lowest validated clock of its cores (cores in a CCD share
a clock source; a slower core can only run that clock or <= ~89% of it), every thread loaded with
y-cruncher, then Prime95 AVX2. A failure steps the CCD with the hotter/weaker core down.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Callable, Optional

PHASES = ["p95-sse", "p95-avx2", "p95-avx512", "p95-l3", "ycruncher"]
YC_BIN = "/usr/lib/y-cruncher/Binaries/24-ZN5 ~ Komari"
YC_TESTS = ["BKT", "BBP", "SFTv4", "SNT", "SVT", "FFTv4", "N63", "VT3"]
P95_FAIL = re.compile(r"FATAL ERROR|TORTURE TEST FAILED|Hardware failure detected|ILLEGAL SUMOUT|SUM\(INPUTS\) != "
                      r"SUM\(OUTPUTS\)|ROUND ?OFF|rounding was|error|Error", re.I)
P95_PASS = re.compile(r"Self-test \S+ passed|Torture Test completed|passed!", re.I)
YC_FAIL = re.compile(r"error|failed|incorrect|mismatch|exception|crash", re.I)


# ----------------------------------------------------------------------------- tool runners
def p95_config(mode: str) -> str:
    avx, avx2, avx512 = {"p95-sse": (0, 0, 0), "p95-avx2": (1, 1, 0), "p95-avx512": (1, 1, 1),
                         "p95-l3": (1, 1, 0)}[mode]
    lo, hi = (448, 8192) if mode == "p95-l3" else (4, 64)
    return "\n".join([
        "V24OptionsConverted=1", "V30OptionsConverted=1", "StressTester=1", "UsePrimenet=0",
        "RollingAverageIsFromV27=1", "ExitOnX=1",
        "CpuSupportsSSE=1", "CpuSupportsSSE2=1", f"CpuSupportsAVX={avx}", f"CpuSupportsAVX2={avx2}",
        f"CpuSupportsFMA3={avx2}", f"CpuSupportsAVX512F={avx512}",
        "NumWorkers=1", "NumCores=1", "CoresPerTest=1", "TortureHyperthreading=0",
        "EnableSetAffinity=0", "EnableSetPriority=0",
        "TortureMem=0", "TortureTime=1", f"MinTortureFFT={lo}", f"MaxTortureFFT={hi}",
        "ResultsFileTimestampInterval=60", ""])


def run_p95(mode: str, cpus: list[int], seconds: int, threads: int = 1) -> tuple[bool, str]:
    d = tempfile.mkdtemp(prefix="meat3-p95-")
    try:
        cfg = p95_config(mode)
        if threads > 1:
            cfg = cfg.replace("NumCores=1", f"NumCores={threads}")
        with open(os.path.join(d, "prime.txt"), "w") as f:
            f.write(cfg)
        cmd = ["taskset", "-c", ",".join(map(str, cpus)), "mprime", "-t", f"-W{d}"]
        p = subprocess.Popen(cmd, cwd=d, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            out, _ = p.communicate(timeout=seconds)
        except subprocess.TimeoutExpired:
            p.terminate()
            try:
                out, _ = p.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
                out, _ = p.communicate()
            ran_full = True
        else:
            ran_full = False                   # exited on its own: torture tests don't, unless something broke
        log = out or ""
        for name in ("results.txt", "results.bench.txt"):
            try:
                with open(os.path.join(d, name)) as f:
                    log += "\n" + f.read()
            except OSError:
                pass
        bad = [ln.strip() for ln in log.splitlines() if P95_FAIL.search(ln)]
        if bad:
            return False, bad[0][:160]
        if not ran_full:
            return False, f"mprime exited early ({p.returncode}): " + log.strip()[-160:]
        return True, f"{len(P95_PASS.findall(log))} FFT sizes passed"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def yc_config(cpus: list[int], seconds_per_test: int, mem_bytes: int) -> str:
    tests = "\n".join(f'            "{t}"' for t in YC_TESTS)
    return "\n".join(["{", '    Action : "StressTest"', "    StressTest : {", '        AllocateLocally : "true"',
                      f"        LogicalCores : [{' '.join(map(str, cpus))}]", f"        TotalMemory : {mem_bytes}",
                      f"        SecondsPerTest : {seconds_per_test}", "        SecondsTotal : 0",
                      '        StopOnError : "true"', "        Tests : [", tests, "        ]", "    }", "}", ""])


def run_ycruncher(cpus: list[int], seconds: int, mem_bytes: Optional[int] = None) -> tuple[bool, str]:
    exe = os.path.join(YC_BIN, "y-cruncher")
    if not os.path.exists(exe):
        exe = "y-cruncher"
    d = tempfile.mkdtemp(prefix="meat3-yc-")
    try:
        per = max(10, seconds // len(YC_TESTS))
        mem = mem_bytes or (256 << 20) * max(1, len(cpus))
        with open(os.path.join(d, "stress.cfg"), "w") as f:
            f.write(yc_config(cpus, per, mem))
        p = subprocess.Popen([exe, "skip-warnings", "config", os.path.join(d, "stress.cfg")], cwd=d,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            out, _ = p.communicate(timeout=per * len(YC_TESTS) + 60)
        except subprocess.TimeoutExpired:
            p.kill()
            out, _ = p.communicate()
            return False, "y-cruncher didn't finish in time (hung?)"
        lines = [ln for ln in (out or "").splitlines() if ln.strip()]
        bad = [ln.strip() for ln in lines if YC_FAIL.search(ln) and "0 error" not in ln.lower()]
        if p.returncode != 0 or bad:
            return False, (bad[0] if bad else f"y-cruncher exit {p.returncode}: " + " ".join(lines[-2:]))[:160]
        return True, f"{len(YC_TESTS)} tests passed"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def tools_missing() -> list[str]:
    miss = []
    if not shutil.which("mprime"):
        miss.append("mprime (paru -S mprime-bin)")
    if not (os.path.exists(os.path.join(YC_BIN, "y-cruncher")) or shutil.which("y-cruncher")):
        miss.append("y-cruncher (paru -S y-cruncher)")
    return miss


# ----------------------------------------------------------------------------- the validation
class Validator:
    """`run_phase(phase, cpus, seconds) -> (ok, why)`; `kmsg_bad() -> [lines]`; `oc` = DaemonOc-like;
    `measure(k, cpu)` -> {'clk', 'temp', ...} while loaded; `save()` persists res."""

    def __init__(self, res: dict, tag: str, sysfs, oc, run_phase: Callable, kmsg_bad: Callable,
                 save: Callable, measure: Callable, phase_s: int = 300, step: int = 25,
                 others_mhz: int = 3000, log: Callable = print):
        self.res, self.tag, self.sys, self.oc = res, tag, sysfs, oc
        self.run_phase, self.kmsg_bad, self.save, self.measure = run_phase, kmsg_bad, save, measure
        self.phase_s, self.step, self.others, self.log = phase_s, step, others_mhz, log
        m = res.setdefault("map", {})
        self.screen = m.get("at_voltage", {}).get(tag, {})
        self.v = m.setdefault("validate", {}).setdefault(tag, {"cores": {}, "ccds": {}})

    def recover(self) -> Optional[str]:
        cur = self.v.pop("current", None)
        if not cur:
            return None
        if cur.get("ccd") is not None:
            c = self.v["ccds"].setdefault(str(cur["ccd"]), {})
            c.setdefault("fails", []).append(cur["mhz"])
        else:
            c = self.v["cores"].setdefault(str(cur["core"]), {"phases": {}})
            c.setdefault("fails", []).append(cur["mhz"])
            c["mhz"] = cur["mhz"] - self.step
            c["phases"] = {}
        self.save()
        where = f"CCD{cur['ccd']}" if cur.get("ccd") is not None else f"core {cur['core']}"
        return f"the last run stopped during {cur['phase']} on {where} at {cur['mhz']} MHz: marked unstable"

    def start_mhz(self, k: int) -> Optional[int]:
        from meat3.cpumap import at_voltage_result
        r = at_voltage_result(self.screen.get(str(k), {}))
        return r.get("target")

    def _check(self, ok: bool, why: str) -> tuple[bool, str]:
        bad = self.kmsg_bad()
        if bad:
            return False, "kernel: " + bad[0]
        return ok, why

    def core(self, k: int) -> None:
        c = self.v["cores"].setdefault(str(k), {"phases": {}})
        if c.get("done"):
            return
        if c.get("mhz") is None:
            c["mhz"] = self.start_mhz(k)
            if c["mhz"] is None:
                self.log(f"   core {k}: no screening result; skipped")
                return
        cpu = self.sys.cores[k][0]
        while True:
            f = c["mhz"]
            self.oc.freq_core(k, f)
            m = self.measure(k, cpu)
            if m.get("clk") is None or abs(m["clk"] - f) > 75:
                self.log(f"   core {k}: asked {f} MHz, runs {m.get('clk') or 0:.0f}; stopping this core")
                c["error"] = f"clock didn't follow ({m.get('clk')})"
                break
            failed = None
            for ph in PHASES:
                if c["phases"].get(ph) == "pass":
                    continue
                self.v["current"] = {"core": k, "phase": ph, "mhz": f}
                self.save()
                t0 = time.time()
                ok, why = self._check(*self.run_phase(ph, [cpu], self.phase_s))
                self.v.pop("current", None)
                if not ok:
                    failed = (ph, why)
                    break
                c["phases"][ph] = "pass"
                self.log(f"   core {k} @ {f} MHz  {ph:10} pass ({time.time() - t0:.0f} s; {why})")
                self.save()
            if failed is None:
                c["done"], c["validated_mhz"] = True, f
                self.log(f"== core {k}: validated at {f} MHz")
                self.save()
                break
            c.setdefault("fails", []).append(f)
            c.setdefault("fail_log", []).append(f"{f} MHz {failed[0]}: {failed[1]}")
            self.log(f"   core {k} @ {f} MHz  {failed[0]:10} FAIL - {failed[1]}")
            c["mhz"], c["phases"] = f - self.step, {}
            self.save()
        self.oc.freq_core(k, self.others)

    def ccd_clock(self, ccd: dict) -> Optional[int]:
        vals = [self.v["cores"].get(str(k), {}).get("validated_mhz") for k in ccd["cores"]]
        vals = [x for x in vals if x]
        return min(vals) if len(vals) == len(ccd["cores"]) else None

    def all_core(self, all_phase_s: int) -> None:
        a = self.v["ccds"]
        clocks = {}
        for ccd in self.sys.ccds:
            st = a.setdefault(str(ccd["ccd"]), {})
            if st.get("mhz") is None:
                st["mhz"] = self.ccd_clock(ccd)
            if st["mhz"] is None:
                self.log(f"   CCD{ccd['ccd']}: not every core validated; all-core test skipped")
                return
            clocks[ccd["ccd"]] = st
        if all(st.get("done") for st in clocks.values()):
            return
        cpus = sorted(c for cl in self.sys.cores for c in cl)
        while True:
            for n, st in clocks.items():
                ccd = next(c for c in self.sys.ccds if c["ccd"] == n)
                for k in ccd["cores"]:
                    self.oc.freq_core(k, st["mhz"])
            self.log("== all cores: " + ", ".join(f"CCD{n} {st['mhz']} MHz" for n, st in clocks.items()))
            failed = None
            for ph in ("ycruncher", "p95-avx2"):
                if all(st.get("phases", {}).get(ph) == "pass" for st in clocks.values()):
                    continue
                self.v["current"] = {"ccd": min(clocks), "phase": "all-core " + ph, "mhz": clocks[min(clocks)]["mhz"]}
                self.save()
                ok, why = self._check(*self.run_phase(ph, cpus, all_phase_s))
                self.v.pop("current", None)
                if not ok:
                    failed = (ph, why)
                    break
                for st in clocks.values():
                    st.setdefault("phases", {})[ph] = "pass"
                self.log(f"   all cores {ph:10} pass ({why})")
                self.save()
            if failed is None:
                for st in clocks.values():
                    st["done"], st["validated_mhz"] = True, st["mhz"]
                self.save()
                return
            # step down the CCD whose clock is highest relative to its single-core results (least margin)
            n = max(clocks, key=lambda n: clocks[n]["mhz"])
            clocks[n].setdefault("fails", []).append(clocks[n]["mhz"])
            self.log(f"   all cores {failed[0]} FAIL - {failed[1]}; CCD{n} down {self.step} MHz")
            clocks[n]["mhz"] -= self.step
            for st in clocks.values():
                st["phases"] = {}
            self.save()

    def run(self, cores: list[int], volt: float, all_phase_s: int) -> None:
        self.oc.mode(True)
        try:
            self.oc.freq_all(self.others)
            self.oc.vid(volt)
            for k in cores:
                self.core(k)
            self.all_core(all_phase_s)
        finally:
            self.oc.mode(False)


def run_phase(phase: str, cpus: list[int], seconds: int) -> tuple[bool, str]:
    if phase == "ycruncher":
        return run_ycruncher(cpus, seconds)
    return run_p95(phase, cpus, seconds, threads=len(cpus))


def report_lines(res: dict, ccd: dict) -> list[str]:
    L = []
    for tag, v in ((res.get("map") or {}).get("validate") or {}).items():
        cores = v.get("cores", {})
        vals = {k: cores.get(str(k), {}).get("validated_mhz") for k in ccd["cores"]}
        if not any(vals.values()):
            continue
        L.append(f"   validated at {tag} (Prime95 SSE/AVX2/AVX-512/L3 + y-cruncher, per core):")
        L.append("       " + "  ".join(f"c{k} {x}" if x else f"c{k} —" for k, x in vals.items()))
        st = v.get("ccds", {}).get(str(ccd["ccd"]), {})
        if st.get("validated_mhz"):
            L.append(f"       whole chip loaded: this CCD holds {st['validated_mhz']} MHz")
    return L
