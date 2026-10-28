"""AMD Zen (family 17h/19h/1Ah) CPU backend for meat³.

Primary target: Ryzen 7 9800X3D (Zen 5 "Granite Ridge", family 1Ah model 44h). Works with
reduced detail on any Zen 2..5 part; rows that cannot be read stay with value=None and a hint.

Data sources (all read-only):
  * /dev/cpu/N/msr (msr module): APERF 0xE8 / MPERF 0xE7 / TSC 0x10 per thread ->
    effective clock, active clock, C0 residency; RAPL_PWR_UNIT 0xC0010299, CORE_ENERGY_STAT
    0xC001029A (per core), PKG_ENERGY_STAT 0xC001029B -> per-core and package power.
  * cpufreq cpuinfo_avg_freq / scaling_cur_freq -> clock fallback without msr.
  * /proc/stat, cpuidle sysfs -> utilisation and C-state residency per thread / core.
  * k10temp (or zenpower) hwmon -> Tctl/Tdie/Tccd (+ SVI2 voltages/currents if exposed).
  * powercap intel-rapl:0 (AMD RAPL) -> package/core power.
  * ryzen_smu /sys/kernel/ryzen_smu_drv/pm_table -> SMU PM table (FCLK/UCLK/MCLK, limits,
    voltages, per-core telemetry) using `pm_tables.py` layouts keyed by pm_table_version.
  * perf_event_open on amd_l3 (per CCX) and amd_umc_N (Zen 4+) uncore PMUs -> L3 hit rate and
    DRAM bandwidth.
Controls: cpufreq governor / EPP / boost / min-max freq / amd-pstate mode (validated sysfs
writes) and SMU PBO/Curve Optimizer writes which are disabled (available=False) unless
MEAT3_EXPERIMENTAL_SMU=1.
"""
from __future__ import annotations

import ctypes
import errno
import glob
import os
import platform
import re
import struct
import subprocess
import sys
import time
from typing import Any, Optional

from meat3.core.model import Backend, Control, Sensor
from meat3.backends import _cpu_common as cc
from meat3.backends._cpu_common import read_int, read_str
from meat3.backends import pm_tables
from meat3.backends import amd_smu
from meat3.backends import l3apps, l3reserve
from meat3.backends import cpu_topdown
from meat3.core import config

# --------------------------------------------------------------------------- MSRs
MSR_TSC = 0x10
MSR_MPERF = 0xE7
MSR_APERF = 0xE8
MSR_RAPL_PWR_UNIT = 0xC0010299
MSR_CORE_ENERGY = 0xC001029A
MSR_PKG_ENERGY = 0xC001029B

RETRY_S = 30.0


class MsrDev:
    """Read-only /dev/cpu/N/msr access (pread at offset = MSR index). Never writes."""

    def __init__(self, root: str = "/dev/cpu"):
        self.root = root
        self.fds: dict[int, int] = {}
        self.error = ""
        self._failed_at = -1e9

    def available(self, cpu: int = 0) -> bool:
        return self._fd(cpu) is not None

    def _fd(self, cpu: int) -> Optional[int]:
        fd = self.fds.get(cpu)
        if fd is not None:
            return fd
        if time.monotonic() - self._failed_at < RETRY_S and self.error:
            return None
        try:
            fd = os.open(os.path.join(self.root, str(cpu), "msr"), os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        except OSError as e:
            self.error = {errno.ENOENT: "msr module not loaded (/dev/cpu/N/msr missing)",
                          errno.EACCES: "no permission for /dev/cpu/N/msr (needs root)",
                          errno.EPERM: "no permission for /dev/cpu/N/msr (needs root/CAP_SYS_RAWIO)"
                          }.get(e.errno, f"/dev/cpu/{cpu}/msr: {e.strerror}")
            self._failed_at = time.monotonic()
            return None
        self.fds[cpu] = fd
        self.error = ""
        return fd

    def read(self, cpu: int, reg: int) -> Optional[int]:
        fd = self._fd(cpu)
        if fd is None:
            return None
        try:
            b = os.pread(fd, 8, reg)
        except OSError:
            return None      # EIO: MSR not implemented on this CPU
        if len(b) != 8:
            return None
        return struct.unpack("<Q", b)[0]

    def close(self) -> None:
        for fd in self.fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds.clear()


# --------------------------------------------------------------------------- perf_event_open

class _PerfAttr(ctypes.Structure):
    # PERF_ATTR_SIZE_VER1 (72 bytes) is enough for counting uncore events.
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("config", ctypes.c_uint64),
                ("sample_period", ctypes.c_uint64), ("sample_type", ctypes.c_uint64),
                ("read_format", ctypes.c_uint64), ("flags", ctypes.c_uint64),
                ("wakeup_events", ctypes.c_uint32), ("bp_type", ctypes.c_uint32),
                ("config1", ctypes.c_uint64), ("config2", ctypes.c_uint64)]


_PERF_SYSCALL = {"x86_64": 298, "aarch64": 241, "i686": 336, "i386": 336}
PERF_FLAG_FD_CLOEXEC = 8


class PerfEvents:
    """Minimal perf_event_open(2) wrapper (counting mode, system-wide on one cpu).

    Tests substitute an object with the same open/read/close interface."""

    def __init__(self):
        self.nr = _PERF_SYSCALL.get(platform.machine())
        self.libc = None
        try:
            self.libc = ctypes.CDLL(None, use_errno=True)
            self.libc.syscall.restype = ctypes.c_long
        except OSError:
            pass

    def open(self, pmu_type: int, config: int, cpu: int, read_format: int = 0) -> int:
        if self.nr is None or self.libc is None:
            raise OSError(errno.ENOSYS, "perf_event_open unsupported on this arch")
        attr = _PerfAttr()
        attr.type = pmu_type
        attr.size = ctypes.sizeof(_PerfAttr)
        attr.config = config
        attr.read_format = read_format
        fd = self.libc.syscall(ctypes.c_long(self.nr), ctypes.byref(attr), ctypes.c_int(-1),
                               ctypes.c_int(cpu), ctypes.c_int(-1), ctypes.c_ulong(PERF_FLAG_FD_CLOEXEC))
        if fd < 0:
            e = ctypes.get_errno()
            raise OSError(e, os.strerror(e))
        return int(fd)

    def read(self, fd: int) -> Optional[int]:
        try:
            b = os.read(fd, 8)
        except OSError:
            return None
        return struct.unpack("<Q", b)[0] if len(b) == 8 else None

    def read_scaled(self, fd: int) -> Optional[tuple[int, int, int]]:
        """(value, time_enabled, time_running) for an fd opened with read_format=3."""
        try:
            b = os.read(fd, 24)
        except OSError:
            return None
        return struct.unpack("<QQQ", b) if len(b) == 24 else None

    def close(self, fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass


def encode_pmu_config(fmt_dir: str, **fields: int) -> int:
    """Encode fields into perf_event_attr.config using the PMU's sysfs `format` dir.
    e.g. amd_df event='config:0-7,32-37' places the low 8 bits at 0-7 and the rest at 32-37."""
    config = 0
    for name, value in fields.items():
        spec = read_str(os.path.join(fmt_dir, name))
        if not spec or not spec.startswith("config:"):
            raise ValueError(f"PMU format field {name!r} not found in {fmt_dir}")
        v = value
        for rng in spec[len("config:"):].split(","):
            a, _, b = rng.partition("-")
            lo, hi = int(a), int(b or a)
            width = hi - lo + 1
            config |= (v & ((1 << width) - 1)) << lo
            v >>= width
        if v:
            raise ValueError(f"value {value:#x} too wide for {name} ({spec})")
    return config


def l3_events_for(family: int, model: int) -> dict[str, dict[str, int]]:
    """L3 PMC encodings from Linux tools/perf/pmu-events/arch/x86/amdzen{2,3,4,5}."""
    zen4plus = family >= 0x1A or (family == 0x19 and (0x10 <= model <= 0x1F or 0x60 <= model <= 0x7F
                                                       or 0xA0 <= model <= 0xAF))
    if zen4plus:
        # l3_lookup_state.all_coherent_accesses_to_l3 / l3_lookup_state.l3_miss
        return {"access": {"event": 0x04, "umask": 0xFF}, "miss": {"event": 0x04, "umask": 0x01}}
    # Zen 2/3: l3_lookup_state.all_l3_req_typs / l3_comb_clstr_state.request_miss
    return {"access": {"event": 0x04, "umask": 0xFF}, "miss": {"event": 0x06, "umask": 0x01}}


# UMC (Zen 4+ amd_umc_N): umc_mem_clk, umc_cas_cmd.rd, umc_cas_cmd.wr (each CAS = 64 B)
UMC_EVENTS = {"memclk": {"event": 0x00}, "rd": {"event": 0x0A, "rdwrmask": 1}, "wr": {"event": 0x0A, "rdwrmask": 2}}


class UncoreCounters:
    """Opens/reads amd_l3 (one fd set per CCX) and amd_umc_N counters; computes rates."""

    def __init__(self, sys_root: str, perf, family: int, model: int, clock):
        self.base = os.path.join(sys_root, "bus/event_source/devices")
        self.perf = perf
        self.family, self.model = family, model
        self.clock = clock
        self.l3: dict[int, dict[str, Optional[int]]] = {}    # cpu -> {access: fd, miss: fd}
        self.umc: dict[str, dict[str, Optional[int]]] = {}   # "amd_umc_0" -> {...}
        self.umc_cpu: dict[str, int] = {}
        self.l3_error = ""
        self.umc_error = ""
        self.l3_cpus: list[int] = []
        self.umc_names: list[str] = sorted(
            (d for d in cc._listdir(self.base) if d.startswith("amd_umc_")), key=lambda d: int(d.rsplit("_", 1)[1]))
        l3dir = os.path.join(self.base, "amd_l3")
        if os.path.isdir(l3dir):
            self.l3_cpus = cc.parse_cpulist(read_str(os.path.join(l3dir, "cpumask")))
        self.rates = cc.RateTracker(wrap=1 << 64)
        self._opened_at = -1e9
        self._open()

    def _open(self) -> None:
        self._opened_at = self.clock()
        if self.perf is None:
            self.l3_error = self.umc_error = "perf_event_open unavailable"
            return
        l3dir = os.path.join(self.base, "amd_l3")
        if not os.path.isdir(l3dir):
            self.l3_error = "amd_l3 PMU not present (load amd_uncore)"
        else:
            typ = read_int(os.path.join(l3dir, "type"))
            evs = l3_events_for(self.family, self.model)
            for cpu in self.l3_cpus:
                fds = self.l3.setdefault(cpu, {})
                for key, fields in evs.items():
                    if fds.get(key) is not None:
                        continue
                    try:
                        fds[key] = self.perf.open(typ, encode_pmu_config(os.path.join(l3dir, "format"), **fields), cpu)
                    except (OSError, ValueError) as e:
                        fds[key] = None
                        self.l3_error = f"amd_l3 open failed: {getattr(e, 'strerror', None) or e}"
        if not self.umc_names:
            self.umc_error = ("amd_umc PMU not present (Zen 4+ with amd_uncore needed)"
                              if self.family >= 0x19 else "DRAM bandwidth counters need Zen 4+ amd_umc PMU")
        for name in self.umc_names:
            d = os.path.join(self.base, name)
            typ = read_int(os.path.join(d, "type"))
            cpus = cc.parse_cpulist(read_str(os.path.join(d, "cpumask")))
            if typ is None or not cpus:
                continue
            self.umc_cpu[name] = cpus[0]
            fds = self.umc.setdefault(name, {})
            for key, fields in UMC_EVENTS.items():
                if fds.get(key) is not None:
                    continue
                try:
                    fds[key] = self.perf.open(typ, encode_pmu_config(os.path.join(d, "format"), **fields), cpus[0])
                except (OSError, ValueError) as e:
                    fds[key] = None
                    self.umc_error = f"{name} open failed: {getattr(e, 'strerror', None) or e}"

    def _need_retry(self) -> bool:
        any_missing = any(v is None for fds in self.l3.values() for v in fds.values()) or \
            any(v is None for fds in self.umc.values() for v in fds.values())
        return any_missing and self.clock() - self._opened_at > RETRY_S

    def sample(self) -> dict:
        if self._need_retry():
            self._open()
        now = self.clock()
        out: dict = {"l3": {}, "umc": {}}
        for cpu, fds in self.l3.items():
            out["l3"][cpu] = {k: self.rates.rate(("l3", cpu, k), now, None if fd is None else self.perf.read(fd))
                              for k, fd in fds.items()}
        for name, fds in self.umc.items():
            out["umc"][name] = {k: self.rates.rate(("umc", name, k), now, None if fd is None else self.perf.read(fd))
                                for k, fd in fds.items()}
        return out

    def close(self) -> None:
        if self.perf is None:
            return
        for d in list(self.l3.values()) + list(self.umc.values()):
            for fd in d.values():
                if fd is not None:
                    self.perf.close(fd)
        self.l3.clear()
        self.umc.clear()


# --------------------------------------------------------------------------- the backend

def zen_generation(family: int, model: int) -> str:
    if family == 0x17:
        return "Zen 2" if model >= 0x30 else "Zen/Zen+"
    if family == 0x19:
        if 0x10 <= model <= 0x1F or 0x60 <= model <= 0x7F or 0xA0 <= model <= 0xAF:
            return "Zen 4"
        return "Zen 3"
    if family == 0x1A:
        return "Zen 5"
    return f"family {family:#x}"


class ResctrlL3:
    """L3 occupancy + MBM bandwidth per L3 domain via the resctrl filesystem (AMD QoS monitoring,
    cpuinfo flags cqm_occup_llc / cqm_mbm_total). The daemon mounts resctrl if needed (root only,
    real /sys only); MEAT3_NO_RESCTRL=1 disables this."""

    def __init__(self, sys_root: str, env, clock):
        self.sys_root = sys_root
        self.base = os.path.join(sys_root, "fs/resctrl")
        self.clock = clock
        self.error: Optional[str] = None
        self.prev: dict[int, tuple[float, int]] = {}
        self.sizes = self._cache_sizes()
        self.apps: Optional[l3apps.L3Apps] = None
        self.reserve: Optional[l3reserve.L3Reserve] = None
        if env.get("MEAT3_NO_RESCTRL") == "1":
            self.error = "disabled (MEAT3_NO_RESCTRL=1)"
            return
        if not os.path.isdir(os.path.join(self.base, "mon_data")):
            self._mount()
        cfg = config.get().get("l3_per_app") or {}
        if (cfg.get("enabled", True) and sys_root == "/sys" and os.geteuid() == 0
                and os.path.isdir(os.path.join(self.base, "mon_groups"))):
            rc = config.get().get("l3_reserve") or {}
            if os.path.isdir(os.path.join(self.base, "info", "L3")):
                self.reserve = l3reserve.L3Reserve(self.base, enabled=rc.get("enabled", True),
                                                   background_mb=rc.get("background_mb", 12),
                                                   domain_sizes=self.sizes)
            self.apps = l3apps.L3Apps(self.base, top=int(cfg.get("top", 8)),
                                      game_patterns=cfg.get("game_patterns"), reserve=self.reserve)

    def _cache_sizes(self) -> dict[int, int]:
        sizes: dict[int, int] = {}
        for d in glob.glob(os.path.join(self.sys_root, "devices/system/cpu/cpu[0-9]*/cache/index3")):
            cid, sz = read_str(os.path.join(d, "id")), read_str(os.path.join(d, "size"))
            m = re.match(r"(\d+)([KMG]?)", sz or "")
            if cid is None or not m:
                continue
            mult = {"": 1, "K": 1024, "M": 1 << 20, "G": 1 << 30}[m.group(2)]
            sizes[int(cid)] = int(m.group(1)) * mult
        return sizes

    def _mount(self) -> None:
        if self.sys_root != "/sys" or os.geteuid() != 0:
            self.error = "resctrl not mounted (mount -t resctrl resctrl /sys/fs/resctrl, needs root)"
            return
        try:
            with open("/proc/filesystems") as f:
                if "resctrl" not in f.read():
                    self.error = "kernel without resctrl (CONFIG_X86_CPU_RESCTRL)"
                    return
            os.makedirs(self.base, exist_ok=True)
            r = subprocess.run(["mount", "-t", "resctrl", "resctrl", self.base], capture_output=True,
                               text=True, timeout=5)
            if r.returncode != 0:
                self.error = f"mount resctrl failed: {r.stderr.strip() or r.returncode}"
        except (OSError, subprocess.SubprocessError) as e:
            self.error = f"mount resctrl failed: {e}"

    def sample(self, now: float) -> dict[int, dict]:
        out: dict[int, dict] = {}
        # whole-L3 occupancy = root group + every control group (e.g. meat3_game while reserved)
        extra = l3apps.ctrl_groups(self.base)[1:]
        for d in sorted(glob.glob(os.path.join(self.base, "mon_data/mon_L3_*"))):
            try:
                cid = int(d.rsplit("_", 1)[1])
            except ValueError:
                continue
            occ = read_int(os.path.join(d, "llc_occupancy"))
            for g in extra:
                v = read_int(os.path.join(g, "mon_data", os.path.basename(d), "llc_occupancy"))
                occ = None if occ is None or v is None else occ + v
            rec = {"occ": occ, "size": self.sizes.get(cid), "has_mbm": os.path.exists(os.path.join(d, "mbm_total_bytes"))}
            if rec["has_mbm"]:
                tot = read_int(os.path.join(d, "mbm_total_bytes"))
                if tot is not None:
                    pv = self.prev.get(cid)
                    if pv and now > pv[0] and tot >= pv[1]:
                        rec["mbm_rate"] = (tot - pv[1]) / (now - pv[0])
                    self.prev[cid] = (now, tot)
            out[cid] = rec
        if not out and self.error is None:
            self.error = "resctrl mounted but no L3 monitoring domains (CPU lacks cqm_occup_llc?)"
        return out


class AmdCpuBackend(Backend):
    kind = "cpu"

    def __init__(self, sys_root: str = "/sys", proc_root: str = "/proc", dev_cpu_root: str = "/dev/cpu",
                 smu_root: Optional[str] = None, perf: Any = "auto", clock=time.monotonic,
                 env: Optional[dict] = None, cpuinfo: Optional[dict] = None, msr_reader: Any = None):
        self.sys_root, self.proc_root = sys_root, proc_root
        self.clock = clock
        self.env = os.environ if env is None else env
        self.info = cpuinfo or cc.parse_cpuinfo(proc_root)
        self.family, self.model = self.info["family"], self.info["model"]
        self.name = self.info["model_name"].replace(" Processor", "").strip() or "AMD CPU"
        self.prefix = "cpu0"
        self.g = f"CPU: {self.name}"
        self.topo = cc.Topology(sys_root)
        self.msr = msr_reader if msr_reader is not None else MsrDev(dev_cpu_root)
        self.stat = cc.ProcStat(proc_root)
        self.idle = cc.CpuIdle(sys_root, self.topo.cpus)
        self.rapl = [z for z in cc.rapl_zones(sys_root)]
        self.cpufreq = cc.CpufreqControls(sys_root, self.prefix, self.g)
        self.hwmon = cc.find_hwmon(sys_root, ("k10temp", "zenpower"))
        self.hwmon_chans = [(p, read_str(os.path.join(p, "name")), cc.hwmon_channels(p)) for p in self.hwmon]
        self.smu = amd_smu.RyzenSmu(smu_root or os.path.join(sys_root, "kernel/ryzen_smu_drv"), env=self.env)
        self.pmt = pm_tables.PmTableReader(self.smu, ncores=len(self.topo.cores),
                                           raw=self.env.get("MEAT3_PMT_RAW") == "1")
        if perf == "auto":
            perf = PerfEvents()
        self.uncore = UncoreCounters(sys_root, perf, self.family, self.model, clock)
        self.resctrl = ResctrlL3(sys_root, self.env, clock)
        td_on = (config.get().get("cpu_topdown") or {}).get("enabled", True) and self.env.get("MEAT3_NO_TOPDOWN") != "1"
        self.topdown = cpu_topdown.TopDown(perf, self.topo.cores, self.family, self.model, clock) if td_on else None
        self.msr_prev: dict[tuple[int, int], tuple[float, int]] = {}
        self.energy_unit: Optional[float] = None
        self.energy_rates = cc.RateTracker(wrap=1 << 32)

    @classmethod
    def detect(cls, **kw) -> list["AmdCpuBackend"]:
        try:
            info = cc.parse_cpuinfo(kw.get("proc_root", "/proc"))
            if info["vendor"] not in ("AuthenticAMD", "HygonGenuine") or info["family"] < 0x17:
                return []
            return [cls(cpuinfo=info, **kw)]
        except Exception:
            return []

    # ------------------------------------------------------------------ MSR sampling
    def _msr_delta(self, cpu: int, reg: int, now_val: Optional[int], wrap: int = 1 << 64) -> Optional[int]:
        key = (cpu, reg)
        if now_val is None:
            self.msr_prev.pop(key, None)
            return None
        prev = self.msr_prev.get(key)
        self.msr_prev[key] = (0.0, now_val)
        if prev is None:
            return None
        d = now_val - prev[1]
        if d < 0:
            d += wrap
        return d

    def _sample_threads(self, now: float) -> dict[int, dict[str, Optional[float]]]:
        """APERF/MPERF/TSC per thread -> eff clock (MHz), active clock (MHz), C0 %."""
        res: dict[int, dict[str, Optional[float]]] = {}
        for cpu in self.topo.cpus:
            t = self.clock()
            a = self.msr.read(cpu, MSR_APERF)
            m = self.msr.read(cpu, MSR_MPERF)
            tsc = self.msr.read(cpu, MSR_TSC)
            da, dm, dt_ = (self._msr_delta(cpu, MSR_APERF, a), self._msr_delta(cpu, MSR_MPERF, m),
                           self._msr_delta(cpu, MSR_TSC, tsc))
            prev_t = self.msr_prev.get((cpu, -1))
            self.msr_prev[(cpu, -1)] = (t, 0)
            r: dict[str, Optional[float]] = {"eff": None, "active": None, "c0": None, "base": None}
            if None not in (da, dm, dt_) and prev_t is not None and t > prev_t[0] and dt_ > 0:
                wall = t - prev_t[0]
                base_hz = dt_ / wall               # TSC (= P0/MPERF reference) rate
                r["base"] = base_hz / 1e6
                r["eff"] = da / wall / 1e6         # APERF counts actual clocks while in C0
                r["c0"] = min(100.0, 100.0 * dm / dt_)
                r["active"] = (da / dm) * base_hz / 1e6 if dm > 0 else None
            res[cpu] = r
        return res

    def _cpufreq_khz(self, cpu: int) -> tuple[Optional[float], str]:
        base = os.path.join(self.sys_root, f"devices/system/cpu/cpu{cpu}/cpufreq")
        for f, h in (("cpuinfo_avg_freq", "cpufreq cpuinfo_avg_freq (kernel APERF/MPERF average)"),
                     ("scaling_cur_freq", "cpufreq scaling_cur_freq (requested/last-sampled clock)")):
            v = read_int(os.path.join(base, f))
            if v is not None:
                return v / 1000.0, h
        return None, "no cpufreq"

    # ------------------------------------------------------------------ sensors
    def sensors(self) -> list[Sensor]:
        now = self.clock()
        out: list[Sensor] = []
        g, p = self.g, self.prefix
        msr_ok = self.msr.available(self.topo.cpus[0] if self.topo.cpus else 0)
        threads = self._sample_threads(now) if msr_ok else {}
        util = self.stat.sample()
        idle = self.idle.sample(now)
        msr_hint = "" if msr_ok else f" (msr unavailable: {self.msr.error}; cpufreq fallback)"

        # --- clocks / usage per core
        gc, gu, gt, gi = f"{g}/Clocks", f"{g}/Usage", f"{g}/Threads", f"{g}/C-states"
        core_eff_all = []
        core_rows_clk, core_rows_act, core_rows_util, core_rows_c0 = [], [], [], []
        base_mhz = None
        for k, cpus in enumerate(self.topo.cores):
            effs, acts, c0s, us = [], [], [], []
            for cpu in cpus:
                tr = threads.get(cpu, {})
                if tr.get("eff") is not None:
                    effs.append(tr["eff"])
                    c0s.append(tr["c0"])
                    if tr.get("active") is not None:
                        acts.append(tr["active"])
                    base_mhz = tr["base"]
                u = util.get(f"cpu{cpu}")
                if u is not None:
                    us.append(u)
            if msr_ok:
                # core clock = fastest thread's APERF rate (both SMT threads share the core clock;
                # each thread's APERF only ticks while that thread is in C0)
                eff = max(effs) if effs else None
                act = max(acts) if acts else None
                hint = "max of sibling threads' APERF/s (MSR 0xE8)"
            else:
                vals = [v for v in (self._cpufreq_khz(c)[0] for c in cpus) if v is not None]
                eff, act = (max(vals) if vals else None), None
                hint = self._cpufreq_khz(cpus[0])[1] + msr_hint
            if eff is not None:
                core_eff_all.append(eff)
            core_rows_clk.append(Sensor(f"{p}.core{k}.clk_eff", gc, f"Core {k} effective clock", eff, "MHz", 0,
                                        hint))
            if msr_ok:
                core_rows_act.append(Sensor(f"{p}.core{k}.clk_active", gc, f"Core {k} active clock", act, "MHz", 0,
                                            "TSC rate x dAPERF/dMPERF: clock while in C0"))
                core_rows_c0.append(Sensor(f"{p}.core{k}.c0", gi, f"Core {k} C0 residency",
                                           max(c0s) if c0s else None, "%", 1, "dMPERF/dTSC, busiest thread"))
            core_rows_util.append(Sensor(f"{p}.core{k}.util", gu, f"Core {k} usage",
                                         (sum(us) / len(us)) if us else None, "%", 1,
                                         "/proc/stat, mean of SMT siblings"))
        out.append(Sensor(f"{p}.clk.eff_avg", gc, "Average effective clock",
                          sum(core_eff_all) / len(core_eff_all) if core_eff_all else None, "MHz", 0,
                          "mean of per-core effective clocks" + msr_hint))
        out.append(Sensor(f"{p}.clk.eff_max", gc, "Highest core effective clock",
                          max(core_eff_all) if core_eff_all else None, "MHz", 0))
        out.append(Sensor(f"{p}.clk.base", gc, "Reference (TSC) clock", base_mhz, "MHz", 1,
                          "measured dTSC/dt; MPERF ticks at this rate" + msr_hint))
        out += core_rows_clk + core_rows_act
        out.append(Sensor(f"{p}.util.total", gu, "Total CPU usage", util.get("cpu"), "%", 1, "/proc/stat"))

        # --- CPPC preferred-core ranking (amd-pstate): firmware's per-core silicon quality ranking
        gq = f"{g}/Core quality (CPPC)"
        for k, cpus in enumerate(self.topo.cores):
            base = os.path.join(self.sys_root, f"devices/system/cpu/cpu{cpus[0]}/cpufreq")
            for fname, sid, label, hint in (
                    ("amd_pstate_prefcore_ranking", "rank", "preferred-core rank",
                     "Higher = better silicon (CPPC). Best cores tolerate more negative Curve Optimizer."),
                    ("amd_pstate_highest_perf", "highest_perf", "highest perf (CPPC)", "CPPC highest performance")):
                try:
                    v = float(open(os.path.join(base, fname)).read().strip())
                except (OSError, ValueError):
                    continue
                out.append(Sensor(f"{p}.core{k}.cppc_{sid}", gq, f"Core {k} {label}", v, "", 0, hint))
        out += core_rows_util
        # --- per-thread
        for cpu in self.topo.cpus:
            k = self.topo.core_of_cpu.get(cpu, 0)
            tr = threads.get(cpu, {})
            t_lbl = f"Thread {cpu} (core {k})"
            if msr_ok:
                out.append(Sensor(f"{p}.t{cpu}.clk_eff", gt, f"{t_lbl} effective clock", tr.get("eff"), "MHz", 0,
                                  "dAPERF/dt (MSR 0xE8)"))
            else:
                v, h = self._cpufreq_khz(cpu)
                out.append(Sensor(f"{p}.t{cpu}.clk_eff", gt, f"{t_lbl} clock", v, "MHz", 0, h + msr_hint))
            out.append(Sensor(f"{p}.t{cpu}.util", gt, f"{t_lbl} usage", util.get(f"cpu{cpu}"), "%", 1, "/proc/stat"))
            if msr_ok:
                out.append(Sensor(f"{p}.t{cpu}.c0", gt, f"{t_lbl} C0 residency", tr.get("c0"), "%", 1,
                                  "dMPERF/dTSC"))
        # --- C-states
        out += core_rows_c0
        for cpu in self.topo.cpus:
            for _, n in self.idle.states.get(cpu, []):
                out.append(Sensor(f"{p}.t{cpu}.idle.{cc.slug(n)}", gi, f"Thread {cpu} {n} residency",
                                  idle.get((cpu, n)), "%", 1, "cpuidle sysfs time delta (OS-requested state)"))

        out += self._temps()
        out += self._power(now, msr_ok)
        pm = self.pmt.sensors(self.prefix, g)
        out += pm
        out += self._core_summary(pm)
        out += self._uncore(pm)
        out += self._l3_occupancy(now)
        out += self._pipeline()
        out += self._misc()
        return out

    TD_ROWS = (("ipc", "ipc", "IPC", "", 2,
                "instructions retired per clock (PMCx0C0 / PMCx076), both threads of the core"),
               ("retiring", "retiring", "retiring", "%", 1,
                "dispatch slots doing useful work (ex_ret_ops / slots)"),
               ("backend", "stall_be", "back-end bound", "%", 1,
                "slots lost waiting on memory/cache or busy execution units (PMCx1A0 umask 0x1E)"),
               ("frontend", "stall_fe", "front-end bound", "%", 1,
                "slots lost waiting for instructions: i-cache/op-cache misses, branch redirects (PMCx1A0 umask 0x01)"),
               ("smt", "stall_smt", "SMT contention", "%", 1,
                "slots given to the sibling thread on the same core (PMCx1A0 umask 0x60)"),
               ("badspec", "bad_spec", "bad speculation", "%", 1,
                "slots spent on ops that were thrown away (mispredicts); the remainder of the other buckets"))

    def _pipeline(self) -> list[Sensor]:
        """Per-core IPC and top-down stall breakdown (cpu_topdown.py)."""
        td = self.topdown
        if td is None:
            return []
        g, p = f"{self.g}/Pipeline stalls", self.prefix
        s = td.sample()
        rows = [r for r in self.TD_ROWS if r[0] == "ipc" or td.width]
        width_txt = f"{td.width} slots/cycle" if td.width else "IPC only before Zen 4"
        note = "; counters time-shared (another perf user or NMI watchdog), values scaled" if td.multiplexed else ""
        err = f"; {td.error}" if td.error else ""
        # whole CPU: sum the raw deltas so busy cores weigh more
        tot: dict[str, float] = {}
        for r in s.values():
            for name, v in (r.get("raw") or {}).items():
                if v is not None:
                    tot[name] = tot.get(name, 0.0) + v
        whole = cpu_topdown.breakdown(tot, td.width)
        out = []
        for key, sid, label, unit, dec, hint in rows:
            out.append(Sensor(f"{p}.pipe.{sid}", g, f"CPU {label}", whole.get(key), unit, dec,
                              f"all cores, weighted by busy cycles: {hint} [{width_txt}]{note}{err}"))
        for key, sid, label, unit, dec, hint in rows:
            for k in range(len(self.topo.cores)):
                out.append(Sensor(f"{p}.core{k}.{sid}", g, f"Core {k} {label}", s.get(k, {}).get(key), unit, dec,
                                  f"{hint}; blank while the core is idle [{width_txt}]{note}{err}"))
        return out

    def _core_summary(self, pm: list[Sensor]) -> list[Sensor]:
        """Hottest core / highest core voltage across the per-core PM table blocks (OSD-friendly)."""
        per = {}
        for s in pm:
            m = re.match(rf"{re.escape(self.prefix)}\.pmt\.core(\d+)\.(temp|volt)$", s.id)
            if m and s.value is not None:
                per.setdefault(m.group(2), []).append((s.value, int(m.group(1))))
        if not per:
            return []
        g, out = f"{self.g}/Per-core summary", []
        if per.get("temp"):
            v, k = max(per["temp"])
            out.append(Sensor(f"{self.prefix}.cores.temp_max", g, "Hottest core temperature", v, "°C", 1,
                              "max of per-core SMU temperatures"))
            out.append(Sensor(f"{self.prefix}.cores.temp_max_core", g, "Hottest core", float(k), "", 0))
        if per.get("volt"):
            v, k = max(per["volt"])
            out.append(Sensor(f"{self.prefix}.cores.volt_max", g, "Highest core voltage", v, "V", 3,
                              "max of per-core SMU voltages (requested VID, not socket-measured)"))
            out.append(Sensor(f"{self.prefix}.cores.volt_max_core", g, "Highest-voltage core", float(k), "", 0))
        return out

    def _temps(self) -> list[Sensor]:
        g = f"{self.g}/Temperatures"
        out = []
        if not self.hwmon_chans:
            out.append(Sensor(f"{self.prefix}.temp.tctl", g, "Tctl", None, "°C", 1,
                              "k10temp hwmon not found (modprobe k10temp)"))
        for path, name, chans in self.hwmon_chans:
            for ch in chans:
                out.append(Sensor(f"{self.prefix}.{name}.{ch['key']}", g if ch["kind"] == "temp" else f"{self.g}/Voltages",
                                  ch["label"], cc.read_hwmon_value(ch), ch["unit"], ch["dec"],
                                  f"{name} hwmon" + (" (Tctl includes the AM4/AM5 offset if any)"
                                                     if ch["label"] == "Tctl" else "")))
        return out

    def _power(self, now: float, msr_ok: bool) -> list[Sensor]:
        g = f"{self.g}/Power"
        p = self.prefix
        out = []
        for z in self.rapl:
            w = z.power(now)
            zid = cc.slug(os.path.basename(z.path))
            label = {"package-0": "Package power (RAPL)",
                     "core": "RAPL 'core' zone (core 0 only)"}.get(z.name, f"RAPL {z.name}")
            note = ("; on AMD the intel_rapl driver reads MSR 0xC001029A of the package's first CPU, "
                    "i.e. core 0 only - use 'Cores power sum (MSR)'" if z.name == "core" else "")
            out.append(Sensor(f"{p}.rapl.{zid}", g, label, w, "W", 2,
                              f"powercap {os.path.basename(z.path)} energy_uj delta{note}" +
                              (f" ({z.error})" if w is None and z.error else "")))
        if not self.rapl:
            out.append(Sensor(f"{p}.rapl.intel_rapl_0", g, "Package power (RAPL)", None, "W", 2,
                              "powercap intel-rapl not present (modprobe intel_rapl_msr)"))
        # MSR energy counters
        if msr_ok and self.energy_unit is None:
            u = self.msr.read(self.topo.cpus[0], MSR_RAPL_PWR_UNIT)
            if u is not None:
                self.energy_unit = 1.0 / (1 << ((u >> 8) & 0x1F))
        eu = self.energy_unit
        pkg = self.msr.read(self.topo.cpus[0], MSR_PKG_ENERGY) if msr_ok else None
        r = self.energy_rates.rate(("pkg",), now, None if pkg is None else pkg & 0xFFFFFFFF)
        hint = "MSR 0xC001029B delta x energy unit (0xC0010299[12:8])" if msr_ok else f"msr unavailable: {self.msr.error}"
        out.append(Sensor(f"{p}.msr.pkg_power", g, "Package power (MSR)", None if r is None or eu is None else r * eu,
                          "W", 2, hint))
        total = 0.0
        have = False
        rows = []
        for k, cpus in enumerate(self.topo.cores):
            e = self.msr.read(cpus[0], MSR_CORE_ENERGY) if msr_ok else None
            rr = self.energy_rates.rate(("core", k), now, None if e is None else e & 0xFFFFFFFF)
            w = None if rr is None or eu is None else rr * eu
            if w is not None:
                total += w
                have = True
            rows.append(Sensor(f"{p}.core{k}.power", g, f"Core {k} power", w, "W", 2,
                               "MSR 0xC001029A (core energy) delta" if msr_ok else hint))
        out.append(Sensor(f"{p}.msr.cores_power", g, "Cores power sum (MSR)", total if have else None, "W", 2,
                          "sum of per-core energy counters"))
        return out + rows

    def _uncore(self, pm: list[Sensor]) -> list[Sensor]:
        s = self.uncore.sample()
        p = self.prefix
        out = []
        gl = f"{self.g}/L3 cache"
        if not self.uncore.l3:
            out.append(Sensor(f"{p}.l3.ccx0.hit_rate", gl, "L3 hit rate", None, "%", 1,
                              self.uncore.l3_error or "amd_l3 PMU unavailable"))
        doms = sorted(self.uncore.l3)
        for i, cpu in enumerate(doms):
            r = s["l3"].get(cpu, {})
            acc, miss = r.get("access"), r.get("miss")
            tag = f"CCX {i}" if len(doms) > 1 else "L3"
            hint = f"amd_l3 PMU on cpu{cpu}: event 0x04 umask 0xff (accesses) / " + \
                   ("0x04 umask 0x01 (misses)" if l3_events_for(self.family, self.model)["miss"]["event"] == 4
                    else "0x06 umask 0x01 (misses)")
            if acc is None and self.uncore.l3_error:
                hint += f"; {self.uncore.l3_error}"
            hit = None
            if acc is not None and miss is not None and acc > 0:
                hit = max(0.0, min(100.0, 100.0 * (1.0 - miss / acc)))
            out.append(Sensor(f"{p}.l3.ccx{i}.hit_rate", gl, f"{tag} hit rate", hit, "%", 1, hint))
            out.append(Sensor(f"{p}.l3.ccx{i}.accesses", gl, f"{tag} accesses", None if acc is None else acc / 1e6,
                              "M/s", 2, hint))
            out.append(Sensor(f"{p}.l3.ccx{i}.misses", gl, f"{tag} misses", None if miss is None else miss / 1e6,
                              "M/s", 2, hint))
        gm = f"{self.g}/Memory"
        rd = wr = 0.0
        ok = False
        clks = []
        for name, r in sorted(s["umc"].items()):
            if r.get("rd") is not None and r.get("wr") is not None:
                rd += r["rd"] * 64
                wr += r["wr"] * 64
                ok = True
            if r.get("memclk") is not None:
                clks.append(r["memclk"] / 1e6)
        hint = ("amd_umc_N PMU: umc_cas_cmd event 0x0a rdwrmask 1/2 x 64 B, summed over "
                f"{len(self.uncore.umc)} UMC(s)")
        if not ok and self.uncore.umc_error:
            hint += f"; {self.uncore.umc_error}"
        out.append(Sensor(f"{p}.mem.bw_read", gm, "DRAM read bandwidth", rd / 1e9 if ok else None, "GB/s", 2, hint))
        out.append(Sensor(f"{p}.mem.bw_write", gm, "DRAM write bandwidth", wr / 1e9 if ok else None, "GB/s", 2, hint))
        out.append(Sensor(f"{p}.mem.bw_total", gm, "DRAM total bandwidth", (rd + wr) / 1e9 if ok else None,
                          "GB/s", 2, hint))
        out.append(Sensor(f"{p}.mem.umc_clk", gm, "Memory clock (UMC counter)",
                          (sum(clks) / len(clks)) if clks else None, "MHz", 0,
                          "umc_mem_clk event 0x00 rate (should equal PM-table MCLK)"))
        return out

    def _l3_occupancy(self, now: float) -> list[Sensor]:
        """L3 occupancy (MB in use) and memory bandwidth per L3 domain (= per CCD on Ryzen) from resctrl."""
        r = self.resctrl
        doms = r.sample(now)
        p, gl, out = self.prefix, f"{self.g}/L3 cache", []
        if not doms:
            out.append(Sensor(f"{p}.l3.ccd0.occupancy", gl, "L3 in use", None, "MB", 1,
                              r.error or "resctrl L3 monitoring unavailable"))
            return out
        multi = len(doms) > 1
        for i, (cid, d) in enumerate(sorted(doms.items())):
            tag = f"CCD {i} L3" if multi else "L3"
            hint = f"resctrl mon_data/mon_L3_{cid:02d}/llc_occupancy (AMD L3 cache occupancy monitoring)"
            occ, size = d.get("occ"), d.get("size")
            out.append(Sensor(f"{p}.l3.ccd{i}.occupancy", gl, f"{tag} in use", None if occ is None else occ / 1e6,
                              "MB", 1, hint))
            if size:
                out.append(Sensor(f"{p}.l3.ccd{i}.occupancy_pct", gl, f"{tag} in use (% of {size / 1048576:.0f} MiB)",
                                  None if occ is None else min(100.0, 100.0 * occ / size), "%", 1, hint))
            # MBM (mbm_total_bytes) is not published: on Granite Ridge with kernel 7.3 in mbm_event
            # (ABMC) mode the counter sits at a constant ~2^50; DRAM bandwidth comes from the UMC PMU.
        out += self._l3_apps(now, doms)
        return out

    def _l3_apps(self, now: float, doms: dict) -> list[Sensor]:
        a = self.resctrl.apps
        if a is None:
            return []
        try:
            res = a.sample(now)
        except OSError as e:
            a.error = str(e)
            return []
        p, g, out = self.prefix, f"{self.g}/L3 by app", []
        size = sum(d.get("size") or 0 for d in doms.values()) or None
        multi = len(doms) > 1
        hint0 = ("resctrl monitoring group per app (threads of the app tagged with their own RMID); "
                 "lines count toward the app that filled them")
        tot = lambda occ: sum(v for v in occ.values() if v is not None) if any(  # noqa: E731
            v is not None for v in occ.values()) else None
        apps_sum = 0
        games = [r for r in res["apps"] if r["game"]]
        game = max(games, key=lambda r: r["cpu"]) if games else None
        for r in res["apps"]:
            b = tot(r["occ"])
            apps_sum += b or 0
            sid = l3apps.slug(r["key"])
            h = (f"{hint0}. {r['procs']} process(es), {r['threads']} thread(s), CPU {r['cpu']:.1f}%"
                 + (" [game]" if r["game"] else ""))
            if multi:
                h += "; per CCD: " + ", ".join(f"CCD {i} {(v or 0) / 1e6:.1f} MB"
                                               for i, (_, v) in enumerate(sorted(r["occ"].items())))
            out.append(Sensor(f"{p}.l3app.{sid}.occupancy", g, f"{r['name']} L3 in use",
                              None if b is None else b / 1e6, "MB", 1, h))
            out.append(Sensor(f"{p}.l3app.{sid}.cpu", g, f"{r['name']} CPU usage", r["cpu"], "%", 1,
                              "share of all CPU threads, smoothed"))
        # use the same occupancy read as the "L3 in use" rows so the numbers add up
        b = tot({cid: d.get("occ") for cid, d in doms.items()}) if doms else tot(res["total"])
        out.append(Sensor(f"{p}.l3app.other.occupancy", g, "Everything else L3 in use",
                          None if b is None else max(0.0, b - apps_sum) / 1e6, "MB", 1,
                          "whole L3 occupancy minus the tracked apps"))
        gb = tot(game["occ"]) if game else None
        out.append(Sensor(f"{p}.l3app.game.occupancy", g, "Game L3 in use",
                          None if gb is None else gb / 1e6, "MB", 1,
                          (f"game: {game['name']} (" + ("Steam app " + game["key"][6:] if game["key"].startswith("steam:")
                                                        else "name match") + ")") if game else
                          "no game running (Steam titles via SteamAppId, others by l3_per_app.game_patterns)"))
        out.append(Sensor(f"{p}.l3app.game.occupancy_pct", g, "Game L3 share",
                          None if gb is None or not size else 100.0 * gb / size, "%", 1,
                          "game's L3 occupancy as % of total L3 size"))
        if a.error:
            out[-1].hint += f"; note: {a.error}"
        out += self._l3_reserve_sensors(g)
        return out

    def _l3_reserve_sensors(self, g: str) -> list[Sensor]:
        r, p = self.resctrl.reserve, self.prefix
        if r is None:
            return []
        bg, game = r.masks()
        wb = r.way_bytes
        hint = ("MB here = MiB, as in AMD's \"96MB L3\". resctrl cache allocation: game threads in control group meat3_game may fill only the "
                f"game ways, everything else only the background ways (masks {bg:x} / {game:x} of {r.full:x}); "
                "set under Overclock → L3 cache")
        out = [Sensor(f"{p}.l3.reserve.active", g, f"L3 reservation: {r.status()}",
                      1.0 if r.active else 0.0, "", 0, hint)]
        if wb:
            on = r.active
            out.append(Sensor(f"{p}.l3.reserve.game_mb", g, "L3 reserved for game",
                              bin(game).count("1") * wb / 1048576 if on else None, "MB", 0, hint))
            out.append(Sensor(f"{p}.l3.reserve.bg_mb", g, "L3 limit for everything else",
                              bin(bg).count("1") * wb / 1048576 if on else None, "MB", 0, hint))
        return out

    def _misc(self) -> list[Sensor]:
        g = f"{self.g}/System"
        p = self.prefix
        out = [
            Sensor(f"{p}.info.model", g, f"{self.name} ({zen_generation(self.family, self.model)}, "
                                         f"fam {self.family:#x} model {self.model:#x})", None,
                   hint="/proc/cpuinfo (string shown in label)"),
            Sensor(f"{p}.info.cores", g, "Physical cores", float(len(self.topo.cores)), "", 0),
            Sensor(f"{p}.info.threads", g, "Logical processors", float(len(self.topo.cpus)), "", 0),
            Sensor(f"{p}.info.ccx", g, "L3 complexes (CCX)", float(len(self.topo.l3_domains)) or None, "", 0,
                   "cache/index3 ids"),
        ]
        out += self.cpufreq.info_sensors(g)
        if self.smu.present():
            out.append(Sensor(f"{p}.info.smu", g, f"SMU fw {self.smu.fw_version() or '?'}, PM table "
                                                  f"{self.pmt.version_str()}", None,
                              hint=f"ryzen_smu codename {self.smu.codename()}, driver {self.smu.drv_version()}"))
        else:
            out.append(Sensor(f"{p}.info.smu", g, "SMU: ryzen_smu not loaded", None,
                              hint="modprobe ryzen_smu for PM table telemetry"))
        out += cc.misc_sensors(p, g, self.proc_root)
        return out

    # ------------------------------------------------------------------ controls
    def controls(self) -> list[Control]:
        return (self.cpufreq.controls() + self._l3_controls()
                + amd_smu.smu_controls(self.smu, self.pmt, self.prefix, self.g, ncores=len(self.topo.cores)))

    def _l3_controls(self) -> list[Control]:
        r, p, g = self.resctrl.reserve, self.prefix, f"{self.g}/L3 cache"
        if r is None:
            return []
        why = None
        if not r.available:
            why = r.error
        elif self.resctrl.apps is None:
            why = "needs l3_per_app.enabled in /etc/meat3/config.json (game detection)"
        wm = r.way_mb
        base = ("While a game runs (Steam title or l3_per_app.game_patterns), background apps may only fill "
                "a small slice of L3 and the game gets the rest to itself, so browsers, Discord, shader "
                "compiles etc. cannot evict the game's data. Lookups are unaffected; nothing is written to "
                "hardware settings that survive a reboot. Saved to /etc/meat3/config.json.")
        out = [Control(f"{p}.l3.reserve", g, "Reserve L3 for games", "bool", r.enabled, default=False,
                       available=why is None, hint=(why + ". " if why else "") + base)]
        if wm:
            out.append(Control(f"{p}.l3.reserve_bg_mb", g, "L3 limit for background apps while gaming", "int",
                               round(r.bg_ways * wm), min=round(wm), max=round(r.max_bg_ways * wm), step=round(wm),
                               unit="MB", default=round(2 * wm), available=why is None,
                               hint=(why + ". " if why else "") +
                               f"Rounded to whole L3 ways ({wm:.0f} MB each of {r.ways}); the game gets the "
                               "rest. Smaller = more for the game, but background apps run from DRAM more."))
        return out

    def _l3_apply(self, control_id: str, value: Any) -> tuple[bool, str]:
        r = self.resctrl.reserve
        if r is None or not r.available or self.resctrl.apps is None:
            return False, "L3 reservation unavailable"
        cfg = config.get()
        rc = dict(cfg.get("l3_reserve") or {})
        if control_id.endswith(".l3.reserve"):
            if not isinstance(value, bool):
                return False, "expected true/false"
            r.set_enabled(value)
            rc["enabled"] = value
            msg = "L3 reservation " + ("on: applies while a game runs" if value else "off")
        else:
            try:
                mb = float(value)
            except (TypeError, ValueError):
                return False, "expected a number of MB"
            lo, hi = r.way_mb, r.max_bg_ways * r.way_mb
            if not lo - 1e-6 <= mb <= hi + 1e-6:
                return False, f"out of range {lo:.0f}..{hi:.0f} MB"
            r.set_background_mb(mb)
            rc["background_mb"] = round(r.bg_ways * r.way_mb)
            msg = f"background apps limited to {rc['background_mb']} MB of L3 while gaming"
        cfg["l3_reserve"] = rc
        try:
            config.save(cfg)
        except OSError as e:
            msg += f" (not saved: {e.strerror})"
        return True, msg

    def apply(self, control_id: str, value: Any) -> tuple[bool, str]:
        if self.cpufreq.handles(control_id):
            return self.cpufreq.apply(control_id, value)
        if control_id in (f"{self.prefix}.l3.reserve", f"{self.prefix}.l3.reserve_bg_mb"):
            return self._l3_apply(control_id, value)
        if control_id.startswith(f"{self.prefix}.smu."):
            return amd_smu.smu_apply(self.smu, self.pmt, self.prefix, control_id, value,
                                     ncores=len(self.topo.cores))
        return False, f"unknown control {control_id}"

    def close(self) -> None:
        self.msr.close()
        self.uncore.close()
        if self.topdown:
            self.topdown.close()
        if self.resctrl.apps:
            self.resctrl.apps.close()           # also releases the L3 reservation
        elif self.resctrl.reserve:
            self.resctrl.reserve.close()


BACKENDS = [AmdCpuBackend]


def _main(argv: list[str]) -> int:
    """Debug CLI: `python3 -m meat3.backends.cpu_amd [--pmt-raw] [--controls]` (read-only)."""
    bs = AmdCpuBackend.detect()
    if not bs:
        print("no AMD Zen CPU detected")
        return 1
    b = bs[0]
    if "--pmt-raw" in argv:
        print(b.pmt.raw_dump())
        return 0
    b.sensors()
    time.sleep(1.0)
    t = time.perf_counter()
    ss = b.sensors()
    dt = (time.perf_counter() - t) * 1000
    for s in ss:
        v = "None" if s.value is None else f"{s.value:.{s.decimals}f}"
        print(f"{s.id:34s} {s.group[len(b.g):] or '/':22s} {s.label:42s} {v:>10s} {s.unit}")
    print(f"# {len(ss)} sensors, sensors() took {dt:.1f} ms")
    if "--controls" in argv:
        for c in b.controls():
            print(f"{c.id:34s} {c.kind:6s} value={c.value!r} min={c.min} max={c.max} avail={c.available} "
                  f"danger={c.danger} choices={c.choices} :: {c.hint[:90]}")
    b.close()
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
