"""meat3-bin: bin an AMD Ryzen CPU per CCD, so two chips can be compared.

    sudo meat3-bin --label A              # fused data, boost clocks, voltage/frequency curve (~15 min, safe)
    sudo meat3-bin --label A --co         # also per-core Curve Optimizer search (hours; uses meat3-cotune)
    sudo meat3-bin --label A --boost      # re-measure boost clocks with the Curve Optimizer results applied
    meat3-bin --report A
    meat3-bin --compare A B

Put chip A in, run it, swap to chip B (same board, BIOS settings, cooler and RAM), run it with
--label B, then --compare. Results are kept in /var/lib/meat3/bin/<label>.json.

What is measured, for each CCD separately (X3D chips: the V-Cache CCD and the frequency CCD):
  * fused data: per-core boost limit (SMU), CPPC preferred-core rank and highest perf, L3 size;
  * boost: the clock each core reaches running one thread on its own, and the clock the CCD holds
    with all its cores loaded, plus voltage, power and temperature at that point;
  * voltage/frequency curve: the frequency limit is lowered step by step (cpufreq) and the voltage
    the core requests is recorded against the clock it actually runs, one core and whole CCD.
    Lower voltage at the same clock = better silicon. This part only ever lowers clocks, so it
    can't make the system unstable;
  * with --co: the most negative stable Curve Optimizer value per core (meat3-cotune), and with
    --boost the clocks reached with those values applied.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import statistics
import subprocess
import sys
import time
from typing import Callable, Optional

STORE = "/var/lib/meat3/bin"
SOCK = "/run/meat3/meat3.sock"
LOAD_METHOD = "matrixprod"      # same load for every point and every chip
TEMP_ABORT_C = 95.0
FIXED_RATIO_NOTE = ("lowering the frequency limit didn't lower the clock: the CPU is probably on a fixed "
                    "multiplier set in the BIOS. Set the BIOS to stock or PBO for the voltage/frequency curve")


# ----------------------------------------------------------------------------- system access
def _read(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _int(path: str) -> Optional[int]:
    v = _read(path)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def _size_mb(text: Optional[str]) -> Optional[float]:
    m = re.fullmatch(r"(\d+)([KMG]?)", text or "")
    if not m:
        return None
    return int(m.group(1)) * {"": 1 / 1048576, "K": 1 / 1024, "M": 1, "G": 1024}[m.group(2)]


class Sysfs:
    """Topology, CCDs and per-CPU frequency limits. sys_root is replaceable for tests."""

    def __init__(self, sys_root: str = "/sys"):
        self.root = sys_root
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from meat3.backends._cpu_common import Topology
        t = Topology(sys_root)
        self.cores: list[list[int]] = t.cores               # core k -> logical cpus; k = CO core k
        self.ccds: list[dict] = []
        for n, (l3id, cpus) in enumerate(sorted(t.l3_domains.items())):
            size = _size_mb(_read(self.cpu(cpus[0], "cache/index3/size")))
            self.ccds.append({"ccd": n, "l3_id": l3id, "l3_mb": size,
                              "cores": sorted({t.core_of_cpu[c] for c in cpus if c in t.core_of_cpu})})
        sizes = {c["l3_mb"] for c in self.ccds if c["l3_mb"]}
        for c in self.ccds:   # V-Cache CCD = the one with more L3; only meaningful when they differ
            c["vcache"] = len(sizes) > 1 and c["l3_mb"] == max(sizes)
            c["role"] = "V-Cache" if c["vcache"] else ("frequency" if len(sizes) > 1 else "")

    def cpu(self, c: int, rel: str) -> str:
        return os.path.join(self.root, f"devices/system/cpu/cpu{c}", rel)

    def khz(self, c: int, name: str) -> Optional[int]:
        return _int(self.cpu(c, f"cpufreq/{name}"))

    def set_max_khz(self, cpus: list[int], khz: int) -> None:
        for c in cpus:
            with open(self.cpu(c, "cpufreq/scaling_max_freq"), "w") as f:
                f.write(str(int(khz)))

    def all_cpus(self) -> list[int]:
        return sorted(c for cl in self.cores for c in cl)

    def ident(self) -> dict:
        info = {}
        for key, rel in (("bios", "class/dmi/id/bios_version"), ("board", "class/dmi/id/board_name"),
                         ("board_vendor", "class/dmi/id/board_vendor")):
            info[key] = _read(os.path.join(self.root, rel))
        return info


def cpu_model() -> dict:
    out = {}
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                k = k.strip()
                if k in ("model name", "microcode") and k not in out:
                    out[k] = v.strip()
    except OSError:
        pass
    return {"model": out.get("model name"), "microcode": out.get("microcode")}


class Daemon:
    def __init__(self, path: str = SOCK):
        import socket
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(10)
        self.s.connect(path)
        self.f = self.s.makefile("rw")

    def call(self, req: dict) -> dict:
        self.f.write(json.dumps(req) + "\n")
        self.f.flush()
        return json.loads(self.f.readline())

    def sensors(self) -> dict:
        return {x["id"]: x for x in self.call({"cmd": "snapshot"}).get("sensors", [])}

    def controls(self) -> dict:
        return {x["id"]: x for x in self.call({"cmd": "snapshot"}).get("controls", [])}


class Load:
    """stress-ng on a set of CPUs, one worker per CPU, until stopped."""

    def __init__(self, cpus: list[int]):
        self.p = subprocess.Popen(["stress-ng", "--cpu", str(len(cpus)), "--cpu-method", LOAD_METHOD,
                                   "--taskset", ",".join(map(str, cpus)), "-t", "600s", "--quiet"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self) -> None:
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(10)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()


# ----------------------------------------------------------------------------- measuring
def _val(sens: dict, *ids: str) -> Optional[float]:
    for i in ids:
        v = (sens.get(i) or {}).get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _median(xs: list) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


class Binner:
    def __init__(self, sysfs: Sysfs, daemon, load: Callable = Load, sleep: Callable = time.sleep,
                 settle_s: float = 4.0, sample_s: int = 8, log: Callable = print):
        self.sys, self.d, self.load, self.sleep = sysfs, daemon, load, sleep
        self.settle_s, self.sample_s, self.log = settle_s, sample_s, log
        sens = daemon.sensors()
        self.p = next((m.group(1) for sid in sens for m in [re.match(r"^(cpu\d*)\.core0\.clk_eff$", sid)] if m), "cpu0")
        self.orig_max = {c: sysfs.khz(c, "scaling_max_freq") for c in sysfs.all_cpus()}

    # ---- helpers
    def restore(self) -> None:
        for c, khz in self.orig_max.items():
            if khz:
                try:
                    self.sys.set_max_khz([c], khz)
                except OSError:
                    pass

    def _core_ids(self, k: int) -> dict:
        p = self.p
        return {"clk": (f"{p}.core{k}.clk_eff", f"{p}.pmt.core{k}.clk"),
                "volt": (f"{p}.pmt.core{k}.volt", f"{p}.core{k}.volt"),
                "power": (f"{p}.core{k}.power", f"{p}.pmt.core{k}.power"),
                "temp": (f"{p}.pmt.core{k}.temp", f"{p}.core{k}.temp")}

    def measure(self, cores: list[int], cpus: list[int]) -> dict:
        """Load `cpus`, then median of clock/voltage (mean over `cores`), power (sum), temperature (max)."""
        ld = self.load(cpus)
        try:
            self.sleep(self.settle_s)
            rows = []
            for _ in range(self.sample_s):
                s = self.d.sensors()
                per = [{key: _val(s, *ids) for key, ids in self._core_ids(k).items()} for k in cores]
                pick = lambda key: [r[key] for r in per if r[key] is not None]
                tctl = _val(s, f"{self.p}.k10temp.temp1", f"{self.p}.temp.tctl", f"{self.p}.pmt.thm")
                rows.append({"clk": statistics.mean(pick("clk")) if pick("clk") else None,
                             "volt": statistics.mean(pick("volt")) if pick("volt") else None,
                             "power": sum(pick("power")) if pick("power") else None,
                             "temp": max(pick("temp") + ([tctl] if tctl is not None else [])) if pick("temp") or tctl else None,
                             "ppt": _val(s, f"{self.p}.pmt.ppt")})
                if tctl is not None and tctl >= TEMP_ABORT_C:
                    raise RuntimeError(f"CPU at {tctl:.0f} °C: stopping (check the cooler)")
                self.sleep(1.0)
        finally:
            ld.stop()
        return {k: _median([r[k] for r in rows]) for k in ("clk", "volt", "power", "temp", "ppt")}

    def best_core(self, ccd: dict, sens: dict) -> int:
        rank = {k: _val(sens, f"{self.p}.core{k}.cppc_rank") for k in ccd["cores"]}
        return max(ccd["cores"], key=lambda k: (rank[k] or 0, -k))

    # ---- phases
    def fused(self) -> dict:
        s = self.d.sensors()
        cores = {}
        for ccd in self.sys.ccds:
            for k in ccd["cores"]:
                cpu0 = self.sys.cores[k][0]
                cores[str(k)] = {"ccd": ccd["ccd"], "cpus": self.sys.cores[k],
                                 "fmax_mhz": _val(s, f"{self.p}.pmt.core{k}.fmax"),
                                 "cppc_rank": _val(s, f"{self.p}.core{k}.cppc_rank"),
                                 "cppc_highest_perf": _val(s, f"{self.p}.core{k}.cppc_highest_perf"),
                                 "cpufreq_max_mhz": (self.sys.khz(cpu0, "cpuinfo_max_freq") or 0) / 1000 or None}
        co = self.d.controls()
        co_now = {str(k): (co.get(f"{self.p}.smu.co_core{k}") or {}).get("value") for k in range(len(self.sys.cores))}
        # the TSC ticks at the P0 clock, normally the base clock, well below every core's fused boost
        # limit. Above the lowest core's limit = a fixed BIOS multiplier
        tsc = _val(s, f"{self.p}.clk.base")
        fm = [c["fmax_mhz"] for c in cores.values() if c["fmax_mhz"]]
        return {"cores": cores, "co_at_start": co_now, "ppt_limit": _val(s, f"{self.p}.pmt.ppt_limit"),
                "tsc_mhz": tsc, "fixed_ratio": bool(tsc and fm and tsc > min(fm) + 20)}

    def boost(self) -> dict:
        """Single-thread clock of every core on its own, and each CCD with all its cores loaded."""
        out = {"single": {}, "ccd": {}}
        for ccd in self.sys.ccds:
            for k in ccd["cores"]:
                self.log(f"   boost: core {k} (CCD{ccd['ccd']}) alone")
                out["single"][str(k)] = self.measure([k], [self.sys.cores[k][0]])
            cpus = [c for k in ccd["cores"] for c in self.sys.cores[k]]
            self.log(f"   boost: CCD{ccd['ccd']} all cores ({len(cpus)} threads)")
            out["ccd"][str(ccd["ccd"])] = self.measure(ccd["cores"], cpus)
        return out

    def vf(self, points: int = 8, low_frac: float = 0.55) -> dict:
        """Voltage against measured clock per CCD, by lowering the frequency limit step by step."""
        out = {}
        sens = self.d.sensors()
        for ccd in self.sys.ccds:
            k = self.best_core(ccd, sens)
            cpus_all = [c for kk in ccd["cores"] for c in self.sys.cores[kk]]
            top = max((self.sys.khz(c, "cpuinfo_max_freq") or 0) for c in cpus_all)
            lo = max(self.sys.khz(c, "cpuinfo_min_freq") or 0 for c in cpus_all)
            caps = sorted({max(lo, int(top * (1 - (1 - low_frac) * i / (points - 1)))) for i in range(points)},
                          reverse=True)
            rec = {"core": k, "single": [], "all": []}
            try:
                for cap in caps:
                    self.sys.set_max_khz(cpus_all, cap)
                    self.log(f"   V/F CCD{ccd['ccd']}: limit {cap / 1000:.0f} MHz")
                    rec["single"].append({"cap": cap / 1000, **self.measure([k], [self.sys.cores[k][0]])})
                    first, now = rec["single"][0], rec["single"][-1]
                    if len(rec["single"]) == 2 and first.get("clk") and now.get("clk") \
                            and now["cap"] < first["cap"] * 0.9 and now["clk"] > first["clk"] * 0.98:
                        rec["note"] = FIXED_RATIO_NOTE
                        self.log("   !! " + FIXED_RATIO_NOTE)
                        rec["single"] = rec["single"][:1]
                        break
                    rec["all"].append({"cap": cap / 1000, **self.measure(ccd["cores"], cpus_all)})
            finally:
                self.restore()
            out[str(ccd["ccd"])] = rec
        return out


# ----------------------------------------------------------------------------- results
def path_for(label: str, store: str = STORE) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", label) or label.startswith("."):
        raise ValueError("label: letters, digits, . _ - only")
    return os.path.join(store, f"{label}.json")


def load(label: str, store: str = STORE) -> dict:
    with open(path_for(label, store)) as f:
        return json.load(f)


def save(res: dict, store: str = STORE) -> None:
    os.makedirs(store, exist_ok=True)
    p = path_for(res["label"], store)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(res, f, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, p)


def volt_at(curve: list[dict], mhz: float) -> Optional[float]:
    """Voltage at `mhz` from a measured curve, interpolated between the two nearest points."""
    pts = sorted((p["clk"], p["volt"]) for p in curve if p.get("clk") and p.get("volt"))
    if not pts or mhz < pts[0][0] - 1 or mhz > pts[-1][0] + 1:
        return None
    for (c0, v0), (c1, v1) in zip(pts, pts[1:]):
        if c0 <= mhz <= c1:
            return v0 if c1 == c0 else v0 + (v1 - v0) * (mhz - c0) / (c1 - c0)
    return pts[0][1] if abs(mhz - pts[0][0]) <= 1 else pts[-1][1]


def ccd_summary(res: dict, ccd: dict) -> dict:
    n = str(ccd["ccd"])
    cores = [str(k) for k in ccd["cores"]]
    fused = res.get("fused", {}).get("cores", {})
    out = {"role": ccd.get("role") or f"CCD{n}", "ccd": ccd["ccd"], "l3_mb": ccd.get("l3_mb")}
    fm = [fused[k]["fmax_mhz"] for k in cores if fused.get(k, {}).get("fmax_mhz")]
    out["fmax_mhz"] = max(fm) if fm else None
    for tag in ("stock", "co"):
        b = res.get("boost", {}).get(tag)
        if not b:
            continue
        single = {k: b["single"].get(k, {}).get("clk") for k in cores}
        good = {k: v for k, v in single.items() if v}
        out[f"{tag}_best_single"] = max(good.values()) if good else None
        out[f"{tag}_best_core"] = int(max(good, key=good.get)) if good else None
        out[f"{tag}_mean_single"] = statistics.mean(good.values()) if good else None
        out[f"{tag}_allcore"] = b["ccd"].get(n, {})
    co = res.get("co", {})
    vals = [co[k]["result"] for k in cores if co.get(k, {}).get("result") is not None]
    if vals:
        out["co_mean"], out["co_best"], out["co_worst"] = statistics.mean(vals), min(vals), max(vals)
    out["vf"] = res.get("vf", {}).get(n)
    return out


def fmt(v, unit="", dec=0, none="—") -> str:
    return none if v is None else f"{v:.{dec}f}{unit}"


def report(res: dict) -> str:
    L = [f"{res['label']}: {res.get('cpu', {}).get('model')}  (microcode {res.get('cpu', {}).get('microcode')}, "
         f"BIOS {res.get('system', {}).get('bios')} on {res.get('system', {}).get('board')})",
         f"measured {res.get('when')}"]
    co_start = {k: v for k, v in res.get("fused", {}).get("co_at_start", {}).items() if v}
    if co_start:
        L.append(f"!! Curve Optimizer was not at 0 when measured: {co_start}")
    if res.get("fused", {}).get("fixed_ratio"):
        L.append(f"!! reference clock {res['fused']['tsc_mhz']:.0f} MHz is above the fused boost limit: the CPU is "
                 f"on a fixed BIOS multiplier. Clocks here are the multiplier, not what the silicon can do; "
                 f"V@1T still compares the cores, since they all run the same clock")
    fused = res.get("fused", {}).get("cores", {})
    for ccd in res.get("ccds", []):
        s = ccd_summary(res, ccd)
        L += ["", f"== CCD{ccd['ccd']} {('(' + s['role'] + ')') if ccd.get('role') else ''}  "
                  f"L3 {fmt(s['l3_mb'], ' MB')}  fused boost limit {fmt(s['fmax_mhz'], ' MHz')}"]
        for tag, name in (("stock", "stock"), ("co", "with CO")):
            if s.get(f"{tag}_best_single") is not None:
                L.append(f"   best frequency ({name}): {fmt(s[f'{tag}_best_single'], ' MHz')} on one core "
                         f"(core {s[f'{tag}_best_core']}), {fmt((s.get(f'{tag}_allcore') or {}).get('clk'), ' MHz')} "
                         f"with every core loaded")
        L.append(f"{'core':>5} {'rank':>5} {'boost lim':>9} {'1T stock':>9} {'1T CO':>8} {'V@1T':>7} {'CO':>5}")
        for k in ccd["cores"]:
            f = fused.get(str(k), {})
            st = res.get("boost", {}).get("stock", {}).get("single", {}).get(str(k), {})
            cb = res.get("boost", {}).get("co", {}).get("single", {}).get(str(k), {})
            co = res.get("co", {}).get(str(k), {}).get("result")
            L.append(f"{k:>5} {fmt(f.get('cppc_rank')):>5} {fmt(f.get('fmax_mhz')):>9} {fmt(st.get('clk')):>9} "
                     f"{fmt(cb.get('clk')):>8} {fmt(st.get('volt'), '', 3):>7} {fmt(co):>5}")
        for tag, name in (("stock", "stock"), ("co", "with CO")):
            a = s.get(f"{tag}_allcore")
            if a:
                L.append(f"   all cores loaded ({name}): {fmt(a.get('clk'), ' MHz')} at {fmt(a.get('volt'), ' V', 3)}, "
                         f"{fmt(a.get('power'), ' W', 1)}, {fmt(a.get('temp'), ' °C')}")
        if s.get("co_mean") is not None:
            L.append(f"   Curve Optimizer: mean {s['co_mean']:.1f}, best {s['co_best']}, worst {s['co_worst']}")
        if s["vf"] and s["vf"].get("note"):
            L.append(f"   voltage/frequency: not measured ({s['vf']['note']})")
        elif s["vf"]:
            L.append(f"   voltage/frequency (core {s['vf']['core']} alone | whole CCD):")
            for a, b in zip(s["vf"]["single"], s["vf"]["all"]):
                L.append(f"     limit {a['cap']:5.0f}: {fmt(a.get('clk'), ' MHz'):>9} {fmt(a.get('volt'), ' V', 3):>8}"
                         f"   | {fmt(b.get('clk'), ' MHz'):>9} {fmt(b.get('volt'), ' V', 3):>8} "
                         f"{fmt(b.get('power'), ' W', 1):>7} {fmt(b.get('temp'), ' °C'):>6}")
    return "\n".join(L)


def _vf_delta(a: Optional[dict], b: Optional[dict], key: str) -> Optional[float]:
    """Mean voltage difference b - a (mV) over the clock range both curves cover."""
    if not a or not b:
        return None
    ca = [p["clk"] for p in a[key] if p.get("clk") and p.get("volt")]
    cb = [p["clk"] for p in b[key] if p.get("clk") and p.get("volt")]
    if not ca or not cb:
        return None
    lo, hi = max(min(ca), min(cb)), min(max(ca), max(cb))
    if hi - lo < 100:
        return None
    pts = [lo + (hi - lo) * i / 10 for i in range(11)]
    d = [(volt_at(b[key], f) - volt_at(a[key], f)) * 1000 for f in pts
         if volt_at(a[key], f) is not None and volt_at(b[key], f) is not None]
    return statistics.mean(d) if d else None


def compare(a: dict, b: dict) -> str:
    L = [f"{a['label']} vs {b['label']}", ""]
    if (a.get("system", {}).get("bios"), a.get("system", {}).get("board")) != \
            (b.get("system", {}).get("bios"), b.get("system", {}).get("board")):
        L.append("!! different board or BIOS between the two runs: the comparison is less reliable")
    for r in (a, b):
        if r.get("fused", {}).get("fixed_ratio"):
            L.append(f"!! {r['label']} was measured on a fixed BIOS multiplier: its clocks don't reflect the silicon")
    roles_a = {c.get("role") or f"CCD{c['ccd']}": c for c in a.get("ccds", [])}
    roles_b = {c.get("role") or f"CCD{c['ccd']}": c for c in b.get("ccds", [])}
    verdicts = []
    for role in [r for r in roles_a if r in roles_b]:
        sa, sb = ccd_summary(a, roles_a[role]), ccd_summary(b, roles_b[role])
        L.append(f"== {role} CCD")
        L.append(f"{'':34}{a['label']:>12}{b['label']:>12}")
        rows = [("fused boost limit", "fmax_mhz", " MHz", 0)]
        for tag, name in (("stock", "stock"), ("co", "with CO")):
            rows += [(f"best 1-core clock ({name})", f"{tag}_best_single", " MHz", 0),
                     (f"mean 1-core clock ({name})", f"{tag}_mean_single", " MHz", 0)]
        rows += [("Curve Optimizer mean", "co_mean", "", 1), ("Curve Optimizer best core", "co_best", "", 0)]
        for name, key, unit, dec in rows:
            if sa.get(key) is None and sb.get(key) is None:
                continue
            L.append(f"  {name:32}{fmt(sa.get(key), unit, dec):>12}{fmt(sb.get(key), unit, dec):>12}")
        for tag, name in (("stock", "stock"), ("co", "with CO")):
            xa, xb = sa.get(f"{tag}_allcore") or {}, sb.get(f"{tag}_allcore") or {}
            if xa or xb:
                L.append(f"  {'all-core clock (' + name + ')':32}{fmt(xa.get('clk'), ' MHz'):>12}{fmt(xb.get('clk'), ' MHz'):>12}")
                L.append(f"  {'  at power / temp':32}{fmt(xa.get('power'), ' W', 0) + ' ' + fmt(xa.get('temp'), '°C'):>12}"
                         f"{fmt(xb.get('power'), ' W', 0) + ' ' + fmt(xb.get('temp'), '°C'):>12}")
        d1, dn = _vf_delta(sa["vf"], sb["vf"], "single"), _vf_delta(sa["vf"], sb["vf"], "all")
        for name, d in (("voltage, 1 core (same clock)", d1), ("voltage, whole CCD (same clock)", dn)):
            if d is not None:
                better = a["label"] if d > 0 else b["label"]
                L.append(f"  {name:32}{b['label']} {'+' if d >= 0 else ''}{d:.0f} mV vs {a['label']}"
                         f"  -> {better} {'needs less' if abs(d) >= 5 else '(within noise)'}")
        # verdict: voltage at the same clock is the silicon quality; CO and clocks back it up
        score = {a["label"]: 0.0, b["label"]: 0.0}
        why = []
        for d, wt, name in ((d1, 1.0, "1-core voltage"), (dn, 1.0, "all-core voltage")):
            if d is not None and abs(d) >= 5:
                score[a["label"] if d > 0 else b["label"]] += wt
                why.append(name)
        for key, wt, name, lower_better in (("co_mean", 1.0, "Curve Optimizer", True),
                                            ("co_best_single", 0.5, "1-core clock with CO", False),
                                            ("stock_best_single", 0.5, "1-core clock", False)):
            va, vb = sa.get(key), sb.get(key)
            if va is None or vb is None or abs(va - vb) < (2 if key == "co_mean" else 25):
                continue
            score[a["label"] if (va < vb) == lower_better else b["label"]] += wt
            why.append(name)
        if score[a["label"]] == score[b["label"]]:
            v = f"{role} CCD: too close to call"
        else:
            w = max(score, key=score.get)
            v = f"{role} CCD: {w} is better ({', '.join(why)})"
        verdicts.append(v)
        L.append("")
    L.append("Verdict")
    L += [f"  {v}" for v in verdicts] or ["  no CCDs in common"]
    if any("V-Cache" in v for v in verdicts):
        L.append("  For games the V-Cache CCD matters most; the frequency CCD sets single-thread boost.")
    return "\n".join(L)


# ----------------------------------------------------------------------------- co phase (meat3-cotune)
def run_co(label: str, args) -> int:
    from meat3 import cotune
    st = cotune.load_state()
    if st and st.get("bin_label") not in (None, label):
        print(f"meat3-cotune has results for '{st.get('bin_label')}'. If that chip is out, clear them with "
              f"'sudo meat3-cotune --reset' and run this again.")
        return 2
    if not st:
        st = {}
    st["bin_label"] = label
    cotune.save_state(st)
    argv = ["--floor", str(args.floor), "--step-time", str(args.step_time)]
    return cotune.main(argv)


def collect_co(res: dict) -> None:
    from meat3 import cotune
    st = cotune.load_state()
    if st.get("bin_label") == res["label"] and st.get("cores"):
        res["co"] = {k: {x: c.get(x) for x in ("pass", "fail", "result", "fail_reason")} for k, c in st["cores"].items()}
        res["co_params"] = st.get("params")


# ----------------------------------------------------------------------------- main
def run(args) -> int:
    if os.geteuid() != 0:
        print("run as root: sudo meat3-bin ...")
        return 2
    if subprocess.run(["which", "stress-ng"], capture_output=True).returncode != 0:
        print("needs stress-ng (pacman -S stress-ng / apt install stress-ng)")
        return 2
    sysfs = Sysfs()
    try:
        d = Daemon()
    except OSError as e:
        print(f"meat3d isn't running ({e})")
        return 2
    try:
        res = load(args.label)
    except FileNotFoundError:
        res = {"label": args.label}
    res.update({"when": time.strftime("%Y-%m-%d %H:%M"), "cpu": cpu_model(), "system": sysfs.ident(),
                "ccds": sysfs.ccds})
    b = Binner(sysfs, d, settle_s=args.settle, sample_s=args.sample)

    def on_sig(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_sig)
    print(f"{res['cpu']['model']}: {len(sysfs.cores)} cores in {len(sysfs.ccds)} CCDs: " + ", ".join(
        f"CCD{c['ccd']} {c['l3_mb']:.0f} MB L3{' (V-Cache)' if c['vcache'] else ''}" for c in sysfs.ccds))
    try:
        if args.co:
            rc = run_co(args.label, args)
            collect_co(res)
            save(res)
            if rc != 0:
                return rc
        if args.boost:
            from meat3 import cotune
            collect_co(res)
            st = cotune.load_state()
            if st.get("bin_label") != args.label or cotune.apply_results(st) != 0:
                print("no Curve Optimizer results for this chip yet (run with --co first)")
                return 1
            try:
                print("== boost clocks with the Curve Optimizer results applied")
                res.setdefault("boost", {})["co"] = b.boost()
            finally:
                for k, v in (st.get("initial") or {}).items():
                    try:
                        d.call({"cmd": "apply", "id": cotune.CTRL.format(k=int(k)), "value": int(v or 0)})
                    except Exception:
                        pass
            save(res)
        if not args.co and not args.boost:
            print("== fused data")
            res["fused"] = b.fused()
            if any(res["fused"]["co_at_start"].values()):
                print("!! Curve Optimizer isn't at 0; set it to 0 (BIOS or Overclock tab) for a fair comparison")
            if res["fused"]["fixed_ratio"]:
                print(f"!! reference clock {res['fused']['tsc_mhz']:.0f} MHz is above the fused boost limit: the CPU "
                      f"is on a fixed BIOS multiplier. Set the BIOS to stock or PBO to bin it properly.")
            print("== boost clocks (stock)")
            res.setdefault("boost", {})["stock"] = b.boost()
            save(res)
            print("== voltage/frequency curve per CCD")
            res["vf"] = b.vf(points=args.points)
            save(res)
    except KeyboardInterrupt:
        print("\ninterrupted; frequency limits restored, partial results kept")
        return 130
    except RuntimeError as e:
        print(f"\nstopped: {e}")
        return 1
    finally:
        b.restore()
    print()
    print(report(res))
    print(f"\nsaved to {path_for(args.label)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="meat3-bin", description=__doc__.split("\n\n")[0])
    ap.add_argument("--label", help="name for this chip, e.g. A or the serial number")
    ap.add_argument("--co", action="store_true", help="per-core Curve Optimizer search (hours)")
    ap.add_argument("--boost", action="store_true", help="re-measure boost clocks with the CO results applied")
    ap.add_argument("--floor", type=int, default=-30, help="--co: most negative CO to try (default -30)")
    ap.add_argument("--step-time", type=int, default=60, help="--co: seconds per CO step (default 60)")
    ap.add_argument("--points", type=int, default=8, help="voltage/frequency points per CCD (default 8)")
    ap.add_argument("--settle", type=float, default=4.0, help="seconds of load before sampling (default 4)")
    ap.add_argument("--sample", type=int, default=8, help="seconds sampled per point (default 8)")
    ap.add_argument("--report", metavar="LABEL")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    args = ap.parse_args(argv)
    try:
        if args.report:
            print(report(load(args.report)))
            return 0
        if args.compare:
            print(compare(load(args.compare[0]), load(args.compare[1])))
            return 0
    except (OSError, ValueError) as e:
        print(f"meat3-bin: {e}")
        return 1
    if not args.label:
        ap.error("--label is required to measure a chip")
    try:
        path_for(args.label)
    except ValueError as e:
        ap.error(str(e))
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
