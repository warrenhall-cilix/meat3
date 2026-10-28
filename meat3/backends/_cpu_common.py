"""Shared helpers for the CPU backends (cpu_amd, cpu_generic) and board.

Everything here is read-only except `CpufreqControls.apply`, which writes only the
standard cpufreq / amd_pstate / intel_pstate sysfs knobs after strict validation.
All paths are relative to configurable roots so tests can point them at fixtures.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Callable, Optional

from meat3.core.model import Control, Sensor


# --------------------------------------------------------------------------- file helpers

def read_str(path: str) -> Optional[str]:
    try:
        with open(path, "r", errors="replace") as f:
            return f.read().strip()
    except (OSError, ValueError):
        return None


def read_int(path: str, base: int = 10) -> Optional[int]:
    s = read_str(path)
    if s is None or s == "":
        return None
    try:
        return int(s.split()[0], base)
    except ValueError:
        return None


def write_str(path: str, value: str) -> tuple[bool, str]:
    try:
        with open(path, "w") as f:
            f.write(value)
        return True, "ok"
    except OSError as e:
        return False, f"{path}: {e.strerror or e}"


def parse_cpulist(s: Optional[str]) -> list[int]:
    """'0-3,8,10-11' -> [0,1,2,3,8,10,11]"""
    out: list[int] = []
    if not s:
        return out
    for part in s.strip().split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "x"


# --------------------------------------------------------------------------- cpuinfo / topology

def parse_cpuinfo(proc_root: str) -> dict:
    """Return vendor/family/model/model_name from the first processor block."""
    info = {"vendor": "", "family": 0, "model": 0, "stepping": 0, "model_name": "CPU", "flags": set()}
    txt = read_str(os.path.join(proc_root, "cpuinfo")) or ""
    for line in txt.splitlines():
        if not line.strip():
            if info["vendor"]:
                break
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        try:
            if k == "vendor_id":
                info["vendor"] = v
            elif k == "cpu family":
                info["family"] = int(v)
            elif k == "model":
                info["model"] = int(v)
            elif k == "stepping":
                info["stepping"] = int(v)
            elif k == "model name":
                info["model_name"] = re.sub(r"\s+", " ", v)
            elif k == "flags":
                info["flags"] = set(v.split())
        except ValueError:
            pass
    return info


class Topology:
    """Logical CPUs grouped into physical cores and L3 domains (CCX)."""

    def __init__(self, sys_root: str):
        base = os.path.join(sys_root, "devices/system/cpu")
        self.cpus = parse_cpulist(read_str(os.path.join(base, "online"))) or \
            sorted(int(d[3:]) for d in _listdir(base) if re.fullmatch(r"cpu\d+", d))
        cores: dict[tuple, list[int]] = {}
        l3: dict[int, list[int]] = {}
        for c in self.cpus:
            t = os.path.join(base, f"cpu{c}/topology")
            pkg = read_int(os.path.join(t, "physical_package_id")) or 0
            die = read_int(os.path.join(t, "die_id")) or 0
            cid = read_int(os.path.join(t, "core_id"))
            if cid is None:
                cid = c
            cores.setdefault((pkg, die, cid), []).append(c)
            l3id = read_int(os.path.join(base, f"cpu{c}/cache/index3/id"))
            if l3id is not None:
                l3.setdefault(l3id, []).append(c)
        # core index k -> sorted list of logical cpus (first = "primary" thread)
        self.cores: list[list[int]] = [sorted(v) for _, v in sorted(cores.items())]
        self.core_of_cpu = {c: k for k, cl in enumerate(self.cores) for c in cl}
        self.l3_domains: dict[int, list[int]] = {k: sorted(v) for k, v in sorted(l3.items())}


def _listdir(p: str) -> list[str]:
    try:
        return sorted(os.listdir(p))
    except OSError:
        return []


# --------------------------------------------------------------------------- /proc/stat

class ProcStat:
    """Per-thread and total utilisation from /proc/stat deltas."""

    def __init__(self, proc_root: str):
        self.path = os.path.join(proc_root, "stat")
        self.prev: dict[str, tuple[int, int]] = {}

    def sample(self) -> dict[str, Optional[float]]:
        """Returns {'cpu': pct, 'cpu0': pct, ...}; None on first call."""
        out: dict[str, Optional[float]] = {}
        txt = read_str(self.path) or ""
        for line in txt.splitlines():
            if not line.startswith("cpu"):
                continue
            p = line.split()
            try:
                vals = [int(x) for x in p[1:9]]
            except ValueError:
                continue
            while len(vals) < 8:
                vals.append(0)
            # user nice system idle iowait irq softirq steal (guest is included in user)
            idle = vals[3] + vals[4]
            total = sum(vals)
            prev = self.prev.get(p[0])
            self.prev[p[0]] = (total, idle)
            if prev is None or total <= prev[0]:
                out[p[0]] = None
            else:
                dt, di = total - prev[0], idle - prev[1]
                out[p[0]] = max(0.0, min(100.0, 100.0 * (dt - di) / dt))
        return out


# --------------------------------------------------------------------------- cpuidle

class CpuIdle:
    """Per-thread residency (% of wall time) of every cpuidle state."""

    def __init__(self, sys_root: str, cpus: list[int]):
        self.base = os.path.join(sys_root, "devices/system/cpu")
        self.states: dict[int, list[tuple[str, str]]] = {}   # cpu -> [(state dir, name)]
        for c in cpus:
            d = os.path.join(self.base, f"cpu{c}/cpuidle")
            sts = []
            for s in _listdir(d):
                if s.startswith("state"):
                    sts.append((os.path.join(d, s), read_str(os.path.join(d, s, "name")) or s))
            if sts:
                self.states[c] = sts
        self.prev: dict[tuple[int, str], tuple[float, int]] = {}

    def state_names(self) -> list[str]:
        names: list[str] = []
        for sts in self.states.values():
            for _, n in sts:
                if n not in names:
                    names.append(n)
        return names

    def sample(self, now: float) -> dict[tuple[int, str], Optional[float]]:
        out: dict[tuple[int, str], Optional[float]] = {}
        for c, sts in self.states.items():
            for d, n in sts:
                t = read_int(os.path.join(d, "time"))   # µs
                key = (c, n)
                prev = self.prev.get(key)
                if t is None:
                    out[key] = None
                    continue
                self.prev[key] = (now, t)
                if prev is None or now <= prev[0] or t < prev[1]:
                    out[key] = None
                else:
                    out[key] = max(0.0, min(100.0, (t - prev[1]) / 1e6 / (now - prev[0]) * 100.0))
        return out


# --------------------------------------------------------------------------- RAPL (powercap)

class RaplZone:
    def __init__(self, path: str):
        self.path = path
        self.name = read_str(os.path.join(path, "name")) or os.path.basename(path)
        self.max_range = read_int(os.path.join(path, "max_energy_range_uj")) or (1 << 32)
        self.prev: Optional[tuple[float, int]] = None
        self.error = ""

    def power(self, now: float) -> Optional[float]:
        try:
            with open(os.path.join(self.path, "energy_uj")) as f:
                e = int(f.read().strip())
        except (OSError, ValueError) as ex:
            self.error = str(getattr(ex, "strerror", None) or ex)
            return None
        prev, self.prev = self.prev, (now, e)
        if prev is None or now <= prev[0]:
            return None
        de = e - prev[1]
        if de < 0:
            de += self.max_range
        return de / 1e6 / (now - prev[0])


def rapl_zones(sys_root: str) -> list[RaplZone]:
    base = os.path.join(sys_root, "class/powercap")
    out = []
    for d in _listdir(base):
        # intel-rapl:0, intel-rapl:0:0 ... (the AMD driver also registers as intel-rapl)
        if re.fullmatch(r"intel-rapl(:\d+)+", d):
            out.append(RaplZone(os.path.join(base, d)))
    return out


# --------------------------------------------------------------------------- hwmon lookup

def find_hwmon(sys_root: str, names: tuple[str, ...]) -> list[str]:
    base = os.path.join(sys_root, "class/hwmon")
    out = []
    for d in _listdir(base):
        p = os.path.join(base, d)
        if read_str(os.path.join(p, "name")) in names:
            out.append(p)
    return out


HWMON_KINDS = {
    # prefix: (unit, scale, decimals)
    "temp": ("°C", 1e-3, 1),
    "in": ("V", 1e-3, 3),
    "curr": ("A", 1e-3, 2),
    "power": ("W", 1e-6, 2),
    "fan": ("RPM", 1.0, 0),
    "humidity": ("%", 1e-3, 1),
    "freq": ("MHz", 1e-6, 0),
}

_CHAN_RE = re.compile(r"^(temp|in|curr|power|fan|humidity|freq)(\d+)_(input|average)$")


def hwmon_channels(path: str) -> list[dict]:
    """List readable channels of one hwmon dir: [{key, kind, file, label, unit, scale, dec}]."""
    chans = []
    seen = set()
    for f in _listdir(path):
        m = _CHAN_RE.match(f)
        if not m:
            continue
        kind, idx, what = m.groups()
        key = f"{kind}{idx}"
        if key in seen:     # power1_input preferred over power1_average
            if what == "average":
                continue
            chans = [c for c in chans if c["key"] != key]
        seen.add(key)
        unit, scale, dec = HWMON_KINDS[kind]
        label = read_str(os.path.join(path, f"{key}_label")) or key
        chans.append({"key": key, "kind": kind, "idx": int(idx), "file": os.path.join(path, f),
                      "label": label, "unit": unit, "scale": scale, "dec": dec})
    order = {k: i for i, k in enumerate(HWMON_KINDS)}
    chans.sort(key=lambda c: (order[c["kind"]], c["idx"]))
    return chans


def read_hwmon_value(chan: dict) -> Optional[float]:
    v = read_int(chan["file"])
    if v is None:
        return None
    return v * chan["scale"]


# --------------------------------------------------------------------------- misc system info

def misc_sensors(prefix: str, group: str, proc_root: str) -> list[Sensor]:
    out = []
    up = read_str(os.path.join(proc_root, "uptime"))
    upv = None
    if up:
        try:
            upv = float(up.split()[0]) / 3600.0
        except ValueError:
            pass
    out.append(Sensor(f"{prefix}.sys.uptime", group, "Uptime", upv, "h", 2, "/proc/uptime"))
    la = (read_str(os.path.join(proc_root, "loadavg")) or "").split()
    for i, n in enumerate(("1 min", "5 min", "15 min")):
        v = None
        if len(la) > i:
            try:
                v = float(la[i])
            except ValueError:
                pass
        out.append(Sensor(f"{prefix}.sys.load{i}", group, f"Load average ({n})", v, "", 2, "/proc/loadavg"))
    mi: dict[str, int] = {}
    for line in (read_str(os.path.join(proc_root, "meminfo")) or "").splitlines():
        k, _, v = line.partition(":")
        try:
            mi[k.strip()] = int(v.split()[0])   # kB
        except (ValueError, IndexError):
            pass
    gib = 1024.0 * 1024.0
    tot, avail = mi.get("MemTotal"), mi.get("MemAvailable")
    used = (tot - avail) if tot is not None and avail is not None else None
    out.append(Sensor(f"{prefix}.sys.ram_used", group, "RAM used", None if used is None else used / gib, "GiB", 2,
                      "MemTotal - MemAvailable"))
    out.append(Sensor(f"{prefix}.sys.ram_total", group, "RAM total", None if tot is None else tot / gib, "GiB", 2))
    out.append(Sensor(f"{prefix}.sys.ram_pct", group, "RAM load",
                      None if used is None or not tot else 100.0 * used / tot, "%", 1))
    st, sf = mi.get("SwapTotal"), mi.get("SwapFree")
    out.append(Sensor(f"{prefix}.sys.swap_used", group, "Swap used",
                      None if st is None or sf is None else (st - sf) / gib, "GiB", 2))
    out.append(Sensor(f"{prefix}.sys.swap_total", group, "Swap total", None if st is None else st / gib, "GiB", 2))
    return out


# --------------------------------------------------------------------------- cpufreq controls

class CpufreqControls:
    """Global (all-policy) cpufreq knobs: governor, EPP, boost, min/max freq, pstate mode.

    Values are read from policy0 (or the first policy found) and written to every policy.
    """

    def __init__(self, sys_root: str, prefix: str, group: str):
        self.base = os.path.join(sys_root, "devices/system/cpu")
        self.cf = os.path.join(self.base, "cpufreq")
        self.prefix = prefix
        self.group = group
        self.policies = [os.path.join(self.cf, d) for d in _listdir(self.cf) if re.fullmatch(r"policy\d+", d)]
        self.policies.sort(key=lambda p: int(p.rsplit("policy", 1)[1]))
        self.p0 = self.policies[0] if self.policies else None
        # pstate driver status files (amd_pstate or intel_pstate)
        self.amd_status = os.path.join(self.base, "amd_pstate/status")
        self.intel_no_turbo = os.path.join(self.base, "intel_pstate/no_turbo")

    # -- reads
    def _p0(self, name: str) -> Optional[str]:
        return read_str(os.path.join(self.p0, name)) if self.p0 else None

    def driver(self) -> Optional[str]:
        return self._p0("scaling_driver")

    def governor(self) -> Optional[str]:
        return self._p0("scaling_governor")

    def governors(self) -> list[str]:
        return (self._p0("scaling_available_governors") or "").split()

    def epp(self) -> Optional[str]:
        return self._p0("energy_performance_preference")

    def epps(self) -> list[str]:
        return (self._p0("energy_performance_available_preferences") or "").split()

    def boost_path(self) -> Optional[str]:
        g = os.path.join(self.cf, "boost")
        if os.path.exists(g):
            return g
        return None

    def boost(self) -> Optional[bool]:
        bp = self.boost_path()
        if bp:
            v = read_int(bp)
            return None if v is None else bool(v)
        if os.path.exists(self.intel_no_turbo):
            v = read_int(self.intel_no_turbo)
            return None if v is None else not bool(v)
        v = self._p0("boost")
        return None if v is None else v.strip() == "1"

    def pstate_status(self) -> Optional[str]:
        return read_str(self.amd_status)

    def khz(self, name: str) -> Optional[int]:
        v = self._p0(name)
        try:
            return int(v) if v is not None else None
        except ValueError:
            return None

    # -- controls
    def controls(self) -> list[Control]:
        g, p = self.group, self.prefix
        out: list[Control] = []
        if not self.p0:
            out.append(Control(f"{p}.cpufreq.governor", g, "Scaling governor", "choice", available=False,
                               hint="no cpufreq policies (driver not loaded?)"))
            return out
        govs = self.governors()
        out.append(Control(f"{p}.cpufreq.governor", g, "Scaling governor", "choice", self.governor(),
                           choices=govs, hint="scaling_governor, written to all policies",
                           available=bool(govs)))
        epps = self.epps()
        out.append(Control(f"{p}.cpufreq.epp", g, "Energy performance preference", "choice", self.epp(),
                           choices=epps, available=bool(epps),
                           hint="energy_performance_preference (all policies). With amd-pstate active + "
                                "'performance' governor only 'performance' is accepted by the kernel."
                           if epps else "EPP not exposed by this cpufreq driver"))
        b = self.boost()
        out.append(Control(f"{p}.cpufreq.boost", g, "Core performance boost", "bool", b,
                           available=b is not None, default=True,
                           hint="cpufreq/boost (or intel_pstate/no_turbo); off = no clocks above base"))
        lo, hi = self.khz("cpuinfo_min_freq"), self.khz("cpuinfo_max_freq")
        avail = lo is not None and hi is not None
        for name, label, dflt in (("scaling_max_freq", "Max frequency", hi), ("scaling_min_freq", "Min frequency", lo)):
            cur = self.khz(name)
            out.append(Control(f"{p}.cpufreq.{name.split('_')[1]}_freq", g, label, "int",
                               None if cur is None else cur // 1000,
                               min=None if lo is None else lo // 1000, max=None if hi is None else -(-hi // 1000),
                               step=1, unit="MHz", default=None if dflt is None else dflt // 1000,
                               available=avail, danger=0,
                               hint=f"{name}, all policies; limited to cpuinfo_min/max_freq"))
        st = self.pstate_status()
        if st is not None:
            out.append(Control(f"{p}.cpufreq.pstate_mode", g, "amd-pstate mode", "choice", st,
                               choices=["active", "passive", "guided"], default="active", danger=1,
                               hint="/sys/devices/system/cpu/amd_pstate/status; switching re-registers cpufreq "
                                    "policies (governor/EPP reset to driver defaults)"))
        return out

    def handles(self, control_id: str) -> bool:
        return control_id.startswith(f"{self.prefix}.cpufreq.")

    def apply(self, control_id: str, value: Any) -> tuple[bool, str]:
        name = control_id[len(self.prefix) + len(".cpufreq."):]
        if not self.p0 and name != "pstate_mode":
            return False, "no cpufreq policies"
        if name == "governor":
            if not isinstance(value, str) or value not in self.governors():
                return False, f"invalid governor {value!r}; choices: {self.governors()}"
            return self._write_all("scaling_governor", value)
        if name == "epp":
            if not isinstance(value, str) or value not in self.epps():
                return False, f"invalid EPP {value!r}; choices: {self.epps()}"
            return self._write_all("energy_performance_preference", value)
        if name == "boost":
            if not isinstance(value, bool) and value not in (0, 1):
                return False, "boost must be a bool"
            on = bool(value)
            bp = self.boost_path()
            if bp:
                return write_str(bp, "1" if on else "0")
            if os.path.exists(self.intel_no_turbo):
                return write_str(self.intel_no_turbo, "0" if on else "1")
            if os.path.exists(os.path.join(self.p0, "boost")):
                return self._write_all("boost", "1" if on else "0")
            return False, "no boost knob"
        if name in ("max_freq", "min_freq"):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
                return False, "frequency must be an integer (MHz)"
            mhz = int(value)
            lo, hi = self.khz("cpuinfo_min_freq"), self.khz("cpuinfo_max_freq")
            if lo is None or hi is None:
                return False, "cpuinfo_min/max_freq unknown"
            lo_m, hi_m = lo // 1000, -(-hi // 1000)
            if not lo_m <= mhz <= hi_m:
                return False, f"{mhz} MHz outside {lo_m}..{hi_m} MHz"
            khz = max(lo, min(hi, mhz * 1000))
            if name == "max_freq":
                cur_min = self.khz("scaling_min_freq")
                if cur_min is not None and khz < cur_min:
                    return False, f"max {mhz} MHz below current min {cur_min // 1000} MHz"
                return self._write_all("scaling_max_freq", str(khz))
            cur_max = self.khz("scaling_max_freq")
            if cur_max is not None and khz > cur_max:
                return False, f"min {mhz} MHz above current max {cur_max // 1000} MHz"
            return self._write_all("scaling_min_freq", str(khz))
        if name == "pstate_mode":
            if not os.path.exists(self.amd_status):
                return False, "amd_pstate status not available"
            if value not in ("active", "passive", "guided"):
                return False, f"invalid amd-pstate mode {value!r}"
            return write_str(self.amd_status, value)
        return False, f"unknown control {control_id}"

    def _write_all(self, fname: str, value: str) -> tuple[bool, str]:
        errs = []
        n = 0
        for p in self.policies:
            ok, msg = write_str(os.path.join(p, fname), value)
            if ok:
                n += 1
            else:
                errs.append(msg)
        if errs:
            return False, f"{n}/{len(self.policies)} policies updated; " + "; ".join(errs[:3])
        return True, f"{fname}={value} on {n} policies"

    def info_sensors(self, group: Optional[str] = None) -> list[Sensor]:
        """String state as label text (Sensor.value is numeric-only)."""
        g, p = group or self.group, self.prefix
        out = [
            Sensor(f"{p}.info.driver", g, f"cpufreq driver: {self.driver() or 'n/a'}", None,
                   hint="scaling_driver (string shown in label)"),
            Sensor(f"{p}.info.governor", g, f"Governor: {self.governor() or 'n/a'}", None,
                   hint="scaling_governor of policy0"),
            Sensor(f"{p}.info.epp", g, f"EPP: {self.epp() or 'n/a'}", None,
                   hint="energy_performance_preference of policy0"),
        ]
        st = self.pstate_status()
        if st is not None:
            out.append(Sensor(f"{p}.info.pstate", g, f"amd-pstate mode: {st}", None,
                              hint="/sys/devices/system/cpu/amd_pstate/status"))
        b = self.boost()
        out.append(Sensor(f"{p}.info.boost", g, "Boost enabled", None if b is None else float(b), "", 0,
                          "1 = enabled"))
        return out


class RateTracker:
    """d(counter)/dt helper with wrap handling for monotonic counters."""

    def __init__(self, wrap: Optional[int] = None):
        self.wrap = wrap
        self.prev: dict[Any, tuple[float, int]] = {}

    def rate(self, key: Any, now: float, value: Optional[int]) -> Optional[float]:
        if value is None:
            self.prev.pop(key, None)
            return None
        prev = self.prev.get(key)
        self.prev[key] = (now, value)
        if prev is None or now <= prev[0]:
            return None
        d = value - prev[1]
        if d < 0:
            if not self.wrap:
                return None
            d += self.wrap
        return d / (now - prev[0])


def monotonic() -> float:
    return time.monotonic()
