"""meat³ backend for AMD GPUs on the upstream `amdgpu` kernel driver.

Primary target: Radeon RX 9070 XT (RDNA4, Navi 48, GC 12.0.1 / gfx1201, SMU/MP1 14.0.2),
which fills `gpu_metrics` v1_3 (smu_v14_0_2_get_gpu_metrics()). Works generically on any
amdgpu dGPU/APU: every gpu_metrics layout from kgd_pp_interface.h is decoded by
amdgpu_metrics.py; overdrive text formats are handled by amdgpu_od.py.

Sources (all read-only except in apply()):
  * hwmon:   temp*_input, fan*_input, pwm*, power*_average/_input, power*_cap*, in*_input,
             curr*_input, freq*_input (+ *_label)
  * device:  gpu_metrics (binary), gpu_busy_percent, mem_busy_percent, vcn_busy_percent,
             mem_info_{vram,vis_vram,gtt}_{used,total}, pp_dpm_*, current/max_link_*,
             power_dpm_force_performance_level, pp_power_profile_mode, pp_od_clk_voltage,
             gpu_od/fan_ctrl/*, ip_discovery/die/0/{GC,MP1}/0, fw_version/smc_fw_version
  * module:  /sys/module/amdgpu/parameters/ppfeaturemask (overdrive bit 0x4000)

The sysfs root is configurable (constructor arg / detect(root=...) / env MEAT3_SYSFS_ROOT)
so tests run against fixture trees. Every write goes through SysfsIO.write().
Set MEAT3_DRY_RUN=1 to log writes without performing them.
"""
from __future__ import annotations

import errno
import glob
import os
import re
import sys
import time
from typing import Any, Callable, Optional

if __package__ in (None, ""):  # allow `python3 meat3/backends/amdgpu.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    __package__ = "meat3.backends"

from ..core.model import Backend, Control, Sensor
from . import amdgpu_metrics as gm
from . import amdgpu_od as odm
from . import amdgpu_pptable as ppt

SYSFS_ROOT_ENV = "MEAT3_SYSFS_ROOT"
DRY_RUN_ENV = "MEAT3_DRY_RUN"
PP_OVERDRIVE_MASK = 0x4000
DEFAULT_PPFEATUREMASK = 0xFFF7BFFF   # amdgpu_drv.c default (overdrive bit clear)

PERF_LEVELS = ["auto", "low", "high", "manual", "profile_standard", "profile_min_sclk",
               "profile_min_mclk", "profile_peak"]

# PCI device id -> marketing family. Only used for the display name.
DEVICE_NAMES = {
    0x7550: "Radeon RX 9070 series (Navi 48)",
    0x7551: "Radeon AI PRO R9700 (Navi 48)",
    0x7590: "Radeon RX 9060 XT (Navi 44)",
    0x744C: "Radeon RX 7900 series (Navi 31)",
    0x7448: "Radeon PRO W7900 (Navi 31)",
    0x747E: "Radeon RX 7800 XT, 7700 XT (Navi 32)",
    0x7480: "Radeon RX 7600 series (Navi 33)",
    0x73BF: "Radeon RX 6800, 6900 XT (Navi 21)",
    0x73AF: "Radeon RX 6900 XT (Navi 21)",
    0x73A5: "Radeon RX 6950 XT (Navi 21)",
    0x73DF: "Radeon RX 6700 series (Navi 22)",
    0x73FF: "Radeon RX 6600 series (Navi 23)",
    0x743F: "Radeon RX 6400, 6500 XT (Navi 24)",
    0x731F: "Radeon RX 5600, 5700 series (Navi 10)",
    0x7340: "Radeon RX 5500 (XT) (Navi 14)",
    0x66AF: "Radeon VII (Vega 20)",
    0x687F: "Radeon RX Vega 56, 64 (Vega 10)",
    0x164E: "Radeon Graphics (Raphael iGPU)",
    0x13C0: "Radeon Graphics (Granite Ridge iGPU)",
    0x15BF: "Radeon 780M, 760M (Phoenix)",
    0x15C8: "Radeon 740M (Phoenix2)",
    0x150E: "Radeon 880M, 890M (Strix Point)",
    0x1586: "Radeon 8060S (Strix Halo)",
    0x1681: "Radeon 680M, 660M (Rembrandt)",
    0x1638: "Radeon Vega (Cezanne)",
    0x1636: "Radeon Vega (Renoir)",
    0x163F: "Custom GPU 0405 (Van Gogh, Steam Deck)",
}
GC_ARCH = [((12,), "RDNA4"), ((11, 5), "RDNA3.5"), ((11,), "RDNA3"), ((10, 3), "RDNA2"),
           ((10, 1), "RDNA1"), ((9, 4), "CDNA"), ((9,), "GCN5/Vega")]

CAT_GROUP = {"temp": "Temperatures", "clock": "Clocks", "volt": "Voltages", "current": "Currents",
             "power": "Power", "activity": "Activity", "fan": "Fan", "pcie": "PCIe",
             "throttle": "Throttling", "memory": "Memory", "raw": "SMU metrics (raw)",
             "dpm": "DPM levels", "limits": "Board limits (firmware)"}


# --------------------------------------------------------------------------------------
# sysfs I/O -- the ONLY place that writes.

def _default_writer(path: str, data: str) -> None:
    fd = os.open(path, os.O_WRONLY)
    try:
        os.write(fd, data.encode())   # one write() == one sysfs store() call
    finally:
        os.close(fd)


class SysfsIO:
    def __init__(self, root: Optional[str] = None,
                 writer: Optional[Callable[[str, str], None]] = None):
        self.root = os.path.abspath(root or os.environ.get(SYSFS_ROOT_ENV) or "/")
        self.writer = writer or _default_writer
        self.dry_run = os.environ.get(DRY_RUN_ENV, "") not in ("", "0")
        self.log: list[tuple[str, str]] = []   # (path relative to root, data) of every write

    def p(self, path: str) -> str:
        """Map an absolute sysfs path ("/sys/...") into the configured root."""
        if self.root == "/":
            return path
        return os.path.join(self.root, path.lstrip("/"))

    def rel(self, path: str) -> str:
        if self.root == "/":
            return path
        return "/" + os.path.relpath(path, self.root)

    @staticmethod
    def read(path: str) -> Optional[str]:
        try:
            with open(path, "r", errors="replace") as fh:
                return fh.read()
        except OSError:
            return None

    @staticmethod
    def read_bytes(path: str) -> Optional[bytes]:
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def write(self, path: str, data: str) -> None:
        real_root = os.path.realpath(self.root)
        real = os.path.realpath(path)
        if real_root != "/" and not (real == real_root or real.startswith(real_root + os.sep)):
            raise PermissionError(errno.EPERM, f"refusing write outside sysfs root: {path}")
        self.log.append((self.rel(path), data))
        if self.dry_run:
            return
        self.writer(path, data)


# --------------------------------------------------------------------------------------
# small text parsers

class _KmsgTail:
    """Opened before an OD write; on failure returns the amdgpu kernel messages logged since
    (e.g. 'Invalid overdrive table content: OD_FAN_CURVE_PWM_ERROR'). Silent if /dev/kmsg is
    unavailable (non-root, tests)."""

    def __init__(self):
        self.fd = None
        try:
            self.fd = os.open("/dev/kmsg", os.O_RDONLY | os.O_NONBLOCK)
            os.lseek(self.fd, 0, os.SEEK_END)
        except OSError:
            self.fd = None

    def amdgpu_lines(self) -> str:
        if self.fd is None:
            return ""
        msgs = []
        while True:
            try:
                rec = os.read(self.fd, 8192).decode(errors="replace")
            except BlockingIOError:
                break
            except OSError:
                continue          # EPIPE: overwritten records; keep reading
            if not rec:
                break
            text = rec.split(";", 1)[-1].strip()
            if "amdgpu" in text or "smu" in text.lower():
                msgs.append(re.sub(r"^amdgpu \S+: (\[drm\] )?", "", text))
        return " | ".join(msgs[-3:])

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def _int(s: Optional[str], base: int = 0) -> Optional[int]:
    if s is None:
        return None
    try:
        return int(s.strip(), base)
    except ValueError:
        return None


def _float_prefix(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)", s)
    return float(m.group(1)) if m else None


_DPM_RE = re.compile(r"^\s*(\d+|S):\s*(\d+(?:\.\d+)?)\s*([MG])?[Hh]z\s*(\*)?")
_PCIE_RE = re.compile(r"^\s*(\d+):\s*([\d.]+)GT/s,\s*x(\d+)\s*(?:(\d+)\s*Mhz)?\s*(\*)?", re.I)


def parse_pp_dpm(text: Optional[str]) -> tuple[list[float], Optional[int], Optional[float]]:
    """pp_dpm_<clk>: "0: 500Mhz\\n1: 2970Mhz *". Returns (levels MHz, current idx, current MHz).
    Index 'S' (deep-sleep level on some APUs) is ignored for the level list."""
    levels, cur_i, cur_v = [], None, None
    for line in (text or "").splitlines():
        m = _DPM_RE.match(line)
        if not m:
            continue
        mhz = float(m.group(2)) * (1000.0 if m.group(3) == "G" else 1.0)
        if m.group(1) != "S":
            levels.append(mhz)
        if m.group(4):
            cur_i = len(levels) - 1 if m.group(1) != "S" else None
            cur_v = mhz
    return levels, cur_i, cur_v


def parse_pp_dpm_pcie(text: Optional[str]) -> Optional[tuple[float, int]]:
    for line in (text or "").splitlines():
        m = _PCIE_RE.match(line)
        if m and m.group(5):
            return float(m.group(2)), int(m.group(3))
    return None


_PROFILE_RE = re.compile(r"^\s*(\d+)\s+([A-Z0-9_]+)\s*(\*)?\s*:?")


def parse_power_profiles(text: Optional[str]) -> tuple[dict[str, int], Optional[str]]:
    """pp_power_profile_mode -> ({name: index}, active name). Handles the SMU format
    (" 1 3D_FULL_SCREEN*:") and the older powerplay table format."""
    profiles: dict[str, int] = {}
    active = None
    for line in (text or "").splitlines():
        m = _PROFILE_RE.match(line)
        if not m:
            continue
        idx, name = int(m.group(1)), m.group(2)
        if name in profiles:
            continue
        profiles[name] = idx
        if m.group(3) or re.match(r"^\s*\d+\s+[A-Z0-9_]+\s*\*", line):
            active = name
    return profiles, active


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------------------

class AmdGpuBackend(Backend):
    kind = "gpu"
    name = "AMD GPU"

    def __init__(self, device_dir: str, index: int = 0, io: Optional[SysfsIO] = None,
                 root: Optional[str] = None):
        self.io = io or SysfsIO(root)
        self.dev = device_dir            # path inside the (possibly redirected) root
        self.index = index
        self.prefix = f"amdgpu{index}"
        uevent = self._r("uevent") or ""
        m = re.search(r"PCI_SLOT_NAME=(\S+)", uevent)
        self.slot = m.group(1) if m else os.path.basename(os.path.realpath(self.dev))
        self.device_id = _int(self._r("device")) or 0
        self.revision = _int(self._r("revision"))
        self.subsys = (_int(self._r("subsystem_vendor")), _int(self._r("subsystem_device")))
        self.vbios = (self._r("vbios_version") or "").strip() or None
        self.gc = self._ip_version("GC")
        self.mp1 = self._ip_version("MP1")
        self.smc_fw = self._smc_fw()
        self.arch = self._arch()
        self.hwmon = self._find_hwmon()
        blob = self.io.read_bytes(os.path.join(self.dev, "gpu_metrics"))
        self._metrics_parsed = gm.parse_gpu_metrics(blob) if blob else None
        ver = self._metrics_parsed["version"] if self._metrics_parsed else None
        self.metrics_version = ver
        self.is_apu = bool(ver and ver[0] in (2, 3))
        base = DEVICE_NAMES.get(self.device_id, f"Radeon GPU [1002:{self.device_id:04x}]")
        base = base.replace("/", "-")    # '/' separates tree levels in Sensor.group
        self.model = base
        self.name = f"AMD GPU {index}: {base}"
        self._seen_metric_keys: list[str] = []
        self._seen_rows: dict[str, dict] = {}
        self._hwmon_rows = self._scan_hwmon()
        self.pptable = None
        if self.mp1 and self.mp1[:2] == (14, 0) and not self.is_apu:
            try:
                self.pptable = ppt.parse(self.io.read_bytes(os.path.join(self.dev, "pp_table")))
            except Exception:
                self.pptable = None

    # ---- discovery -------------------------------------------------------------------

    @classmethod
    def detect(cls, root: Optional[str] = None) -> list["AmdGpuBackend"]:
        try:
            io = SysfsIO(root)
            found = []
            seen = set()
            for card in sorted(glob.glob(io.p("/sys/class/drm/card*"))):
                if not re.match(r"^card\d+$", os.path.basename(card)):
                    continue
                dev = os.path.join(card, "device")
                if (SysfsIO.read(os.path.join(dev, "vendor")) or "").strip().lower() != "0x1002":
                    continue
                if not cls._is_amdgpu(dev):
                    continue
                real = os.path.realpath(dev)
                if real in seen:
                    continue
                seen.add(real)
                ue = SysfsIO.read(os.path.join(dev, "uevent")) or ""
                m = re.search(r"PCI_SLOT_NAME=(\S+)", ue)
                found.append((m.group(1) if m else os.path.basename(real), dev))
            found.sort()
            out = []
            for i, (_slot, dev) in enumerate(found):
                try:
                    out.append(cls(dev, i, SysfsIO(root)))
                except Exception:   # never raise from detect()
                    continue
            return out
        except Exception:
            return []

    @staticmethod
    def _is_amdgpu(dev: str) -> bool:
        ue = SysfsIO.read(os.path.join(dev, "uevent")) or ""
        if re.search(r"^DRIVER=amdgpu$", ue, re.M):
            return True
        try:
            return os.path.basename(os.readlink(os.path.join(dev, "driver"))) == "amdgpu"
        except OSError:
            return False

    def _r(self, rel: str) -> Optional[str]:
        return SysfsIO.read(os.path.join(self.dev, rel))

    def _ip_version(self, ip: str) -> Optional[tuple[int, int, int]]:
        d = os.path.join(self.dev, "ip_discovery", "die", "0", ip, "0")
        vals = [_int(SysfsIO.read(os.path.join(d, f)), 10) for f in ("major", "minor", "revision")]
        return tuple(vals) if all(v is not None for v in vals) else None  # type: ignore

    def _smc_fw(self) -> Optional[int]:
        t = self._r("fw_version/smc_fw_version")
        return _int(t) if t else None

    def _arch(self) -> Optional[str]:
        if not self.gc:
            return None
        for prefix, name in GC_ARCH:
            if self.gc[:len(prefix)] == prefix:
                return name
        return None

    def _find_hwmon(self) -> Optional[str]:
        hs = sorted(glob.glob(os.path.join(self.dev, "hwmon", "hwmon*")))
        for h in hs:
            if (SysfsIO.read(os.path.join(h, "name")) or "").strip() == "amdgpu":
                return h
        return hs[0] if hs else None

    def _ppfeaturemask(self) -> Optional[int]:
        return _int(SysfsIO.read(self.io.p("/sys/module/amdgpu/parameters/ppfeaturemask")))

    # ---- sensors ---------------------------------------------------------------------

    def _grp(self, cat: str) -> str:
        return f"{self.name}/{CAT_GROUP[cat]}"

    def _scan_hwmon(self) -> list[tuple]:
        """Static list of (id, cat, label, file, scale, unit, decimals, hint)."""
        rows: list[tuple] = []
        h = self.hwmon
        if not h:
            return rows

        def label(stem: str) -> Optional[str]:
            t = SysfsIO.read(os.path.join(h, stem + "_label"))
            return t.strip() if t else None

        def files(pattern):
            out = []
            for f in glob.glob(os.path.join(h, pattern)):
                m = re.match(r"^([a-z]+)(\d+)_", os.path.basename(f))
                if m:
                    out.append((int(m.group(2)), f))
            return sorted(out)

        tnames = {"edge": "Edge", "junction": "Junction (hotspot)", "mem": "Memory"}
        for n, f in files("temp*_input"):
            lab = label(f"temp{n}") or f"temp{n}"
            rows.append((f"hwmon.temp{n}", "temp", tnames.get(lab, lab), f, 0.001, "°C", 0,
                         f"hwmon temp{n}_input ({lab})"))
        for n, f in files("temp*_crit"):
            if not re.search(r"temp\d+_crit$", f):
                continue
            lab = label(f"temp{n}") or f"temp{n}"
            for suffix, what in (("crit", "critical limit"), ("emergency", "emergency limit")):
                fp = os.path.join(h, f"temp{n}_{suffix}")
                if os.path.exists(fp):
                    rows.append((f"hwmon.temp{n}_{suffix}", "limits", f"{tnames.get(lab, lab)} {what}", fp,
                                 0.001, "°C", 0, f"hwmon temp{n}_{suffix}"))
        for n, f in files("fan*_input"):
            rows.append((f"hwmon.fan{n}", "fan", f"Fan {n} speed", f, 1.0, "RPM", 0,
                         f"hwmon fan{n}_input"))
        for n, f in files("fan*_target"):
            rows.append((f"hwmon.fan{n}_target", "fan", f"Fan {n} target", f, 1.0, "RPM", 0,
                         f"hwmon fan{n}_target"))
        for f in sorted(glob.glob(os.path.join(h, "pwm[0-9]"))):
            n = os.path.basename(f)[3:]
            rows.append((f"hwmon.pwm{n}", "fan", f"Fan {n} duty", f, 100.0 / 255.0, "%", 0,
                         f"hwmon pwm{n} (0-255 scaled to %)"))
        for kind, suffix in (("average", "avg"), ("input", "")):
            for n, f in files(f"power*_{kind}"):
                lab = label(f"power{n}") or f"power{n}"
                rows.append((f"hwmon.power{n}_{kind}", "power",
                             f"{lab} power" + (" (avg)" if suffix else ""), f, 1e-6, "W", 1,
                             f"hwmon power{n}_{kind} ({lab})"))
        for n, f in files("power*_cap"):
            if not re.search(r"power\d+_cap$", f):
                continue
            lab = label(f"power{n}") or f"power{n}"
            rows.append((f"hwmon.power{n}_cap", "power", f"{lab} limit", f, 1e-6, "W", 0,
                         f"hwmon power{n}_cap"))
        vnames = {"vddgfx": "VDDGFX", "vddnb": "VDDNB (SoC)", "vddboard": "VDDBOARD"}
        for n, f in files("in*_input"):
            lab = label(f"in{n}") or f"in{n}"
            rows.append((f"hwmon.in{n}", "volt", vnames.get(lab, lab), f, 0.001, "V", 3,
                         f"hwmon in{n}_input ({lab})"))
        for n, f in files("curr*_input"):
            lab = label(f"curr{n}") or f"curr{n}"
            rows.append((f"hwmon.curr{n}", "current", lab, f, 0.001, "A", 2,
                         f"hwmon curr{n}_input ({lab})"))
        fnames = {"sclk": "GFX clock (sclk)", "mclk": "Memory clock (mclk)"}
        for n, f in files("freq*_input"):
            lab = label(f"freq{n}") or f"freq{n}"
            rows.append((f"hwmon.freq{n}", "clock", fnames.get(lab, lab), f, 1e-6, "MHz", 0,
                         f"hwmon freq{n}_input ({lab})"))
        return rows

    def read_metrics(self) -> Optional[dict]:
        blob = self.io.read_bytes(os.path.join(self.dev, "gpu_metrics"))
        if not blob:
            return None
        return gm.parse_gpu_metrics(blob)

    def sensors(self) -> list[Sensor]:
        out: list[Sensor] = []
        P = self.prefix

        # hwmon
        for sid, cat, label, f, scale, unit, dec, hint in self._hwmon_rows:
            v = _int(SysfsIO.read(f))
            out.append(Sensor(f"{P}.{sid}", self._grp(cat), label,
                              None if v is None else v * scale, unit, dec, hint))

        # device-level files
        for fname, label in (("gpu_busy_percent", "GPU busy"), ("mem_busy_percent", "Memory busy"),
                             ("vcn_busy_percent", "VCN busy")):
            path = os.path.join(self.dev, fname)
            if os.path.exists(path):
                v = _int(SysfsIO.read(path))
                out.append(Sensor(f"{P}.dev.{fname}", self._grp("activity"), label,
                                  None if v is None else float(v), "%", 0, f"sysfs {fname}"))
        for kind, label in (("vram", "VRAM"), ("vis_vram", "Visible VRAM (BAR)"), ("gtt", "GTT")):
            for part in ("used", "total"):
                fname = f"mem_info_{kind}_{part}"
                path = os.path.join(self.dev, fname)
                if os.path.exists(path):
                    v = _int(SysfsIO.read(path))
                    out.append(Sensor(f"{P}.mem.{kind}_{part}", self._grp("memory"),
                                      f"{label} {part}", None if v is None else v / 1048576.0,
                                      "MiB", 0, f"sysfs {fname}"))
        for f in ("current_link_speed", "current_link_width", "max_link_speed", "max_link_width"):
            path = os.path.join(self.dev, f)
            if os.path.exists(path):
                t = SysfsIO.read(path)
                v = _float_prefix(t)
                speed = "speed" in f
                out.append(Sensor(f"{P}.pcie.{f}", self._grp("pcie"),
                                  f.replace("_", " ").capitalize().replace("link", "PCIe link"),
                                  v, "GT/s" if speed else "lanes", 1 if speed else 0,
                                  "PCI config space; on dGPUs with an internal switch this is the "
                                  "GPU<->switch link, see pp_dpm_pcie / SMU rows for the real slot link"))
        for clk in ("sclk", "mclk", "fclk", "socclk", "dcefclk", "vclk", "dclk", "vclk1", "dclk1"):
            path = os.path.join(self.dev, f"pp_dpm_{clk}")
            if not os.path.exists(path):
                continue
            levels, cur_i, cur_v = parse_pp_dpm(SysfsIO.read(path))
            if not levels and cur_v is None:
                continue  # empty on this ASIC (e.g. APU dcefclk)
            hint = f"pp_dpm_{clk}: levels " + ", ".join(f"{x:g}" for x in levels)
            if cur_i is not None:
                hint += f"; current level {cur_i}"
            out.append(Sensor(f"{P}.dpm.{clk}", self._grp("dpm"), f"{clk.upper()} DPM level",
                              cur_v, "MHz", 0, hint))
        pcie_path = os.path.join(self.dev, "pp_dpm_pcie")
        if os.path.exists(pcie_path):
            pc = parse_pp_dpm_pcie(SysfsIO.read(pcie_path))
            if pc is not None or (SysfsIO.read(pcie_path) or "").strip():
                out.append(Sensor(f"{P}.dpm.pcie_speed", self._grp("dpm"), "PCIe DPM link speed",
                                  pc[0] if pc else None, "GT/s", 1, "pp_dpm_pcie current level"))
                out.append(Sensor(f"{P}.dpm.pcie_width", self._grp("dpm"), "PCIe DPM link width",
                                  float(pc[1]) if pc else None, "lanes", 0, "pp_dpm_pcie current level"))

        # gpu_metrics
        parsed = self.read_metrics()
        rows = gm.metric_rows(parsed, self.smc_fw) if parsed else []
        current = {}
        for r in rows:
            current[r["key"]] = r
            if r["key"] not in self._seen_rows and r["value"] is not None:
                self._seen_metric_keys.append(r["key"])
            if r["value"] is not None or r["key"] in self._seen_rows:
                self._seen_rows[r["key"]] = r
        for key in self._seen_metric_keys:
            r = current.get(key) or dict(self._seen_rows[key], value=None)
            out.append(Sensor(f"{P}.gm.{key}", self._grp(r["cat"]), r["label"], r["value"],
                              r["unit"], r["decimals"], r["hint"]))
        if self.mp1 and self.mp1[0] == 14 and "throttle_status" in self._seen_rows:
            out += self._smu14_throttle(current.get("throttle_status"))
        out += self._board_limits()
        if self.is_apu and self.mp1 and tuple(self.mp1[:3]) == (13, 0, 5):
            self._mark_apu_power_unreliable(out)
        return out

    _APU_BAD_POWER = ("gm.average_socket_power", "gm.average_gfx_power", "gm.average_soc_power",
                      "hwmon.power1_input", "hwmon.power1_average")

    def _mark_apu_power_unreliable(self, out: list[Sensor]) -> None:
        """MP1 13.0.5 (Raphael/Granite Ridge desktop iGPU): the metrics table's power fields do not
        follow the header's meaning (Power[0]=VDDCR_VDD, Power[1]=VDDCR_SOC, CurrentSocketPower,
        all mW). Measured on a 9800X3D: socket power 0.05-0.10 W, and "VDDCR_VDD" falls from 23 W to
        10 W while package power rises from 30 W to 97 W under an all-core load. Only the SoC field
        (~11.7 W) is in the right range (CPU-side VDDCR_SOC 10.4 W). Use the CPU's PM-table rows."""
        for s in out:
            if s.id.split(".", 1)[-1] in self._APU_BAD_POWER:
                s.label += " (unreliable)"
                s.hint = ("Desktop Ryzen iGPU firmware (MP1 13.0.5) does not fill this power field as "
                          "documented; it does not track real power. Use the CPU's PM-table power rows "
                          "(socket, VDDCR_CPU, VDDCR_SOC). " + (s.hint or ""))

    def _smu14_throttle(self, row: Optional[dict]) -> list[Sensor]:
        """SMU14 raw throttle_status decoded with THROTTLER_*_BIT names (smu14_driver_if_v14_0.h).
        The driver's independent map drops hotspot GFX/SoC, PLX and DVO; these rows don't."""
        raw = row.get("value") if row else None
        val = None if raw is None else int(raw)
        active = ppt.decode_smu14_throttle(val)
        g, P = self._grp("throttle"), self.prefix
        hint = "gpu_metrics throttle_status, SMU14 bit names" + \
               ("" if active is None else " = " + (", ".join(active) if active else "none"))
        out = [Sensor(f"{P}.smu14.throttle.count", g, "Active throttle reasons (SMU14)",
                      None if active is None else float(len(active)), "", 0, hint)]
        for key, label, bits in ppt.SMU14_THROTTLER_CLASSES:
            names = [ppt.SMU14_THROTTLER_BITS[b] for b in bits]
            out.append(Sensor(f"{P}.smu14.throttle.{key}", g, f"{label} (SMU14)",
                              None if val is None else float(any(val & (1 << b) for b in bits)), "", 0,
                              "1 = throttling; bits " + ", ".join(names)))
        return out

    def _board_limits(self) -> list[Sensor]:
        """Static limits from the SMU14 pp_table (firmware): rated clocks, power, current, temps."""
        pt = self.pptable
        if not pt:
            return []
        g, P, out = self._grp("limits"), self.prefix, []
        src = "pp_table (SMU 14.0.2 powerplay table from PMFW)"
        add = lambda sid, label, v, unit, dec=0, h="": out.append(  # noqa: E731
            Sensor(f"{P}.limits.{sid}", g, label, None if v is None else float(v), unit, dec,
                   f"{src}{'; ' + h if h else ''}"))
        for k in ("base", "game", "boost"):
            add(f"clk_{k}", f"Rated {k} clock", pt["clocks"][k] or None, "MHz", 0, "DriverReportedClocks AC")
        add("ppt0", "Power limit (firmware message limit)", pt["msg_ppt0_ac_w"] or None, "W", 0,
            ppt.power_cap_note(pt))
        add("od_ppt_max", "Overdrive power limit max", pt["od_max"]["Ppt"], "%", 0,
            "OverDriveLimitsBasicMax.Ppt: highest +% the board allows above stock")
        add("od_ppt_min", "Overdrive power limit min", pt["od_min"]["Ppt"], "%", 0,
            "OverDriveLimitsBasicMin.Ppt")
        add("tdc_gfx", "GFX current limit (TDC)", pt["msg_tdc_a"]["gfx"] or None, "A", 0, "MsgLimits.Tdc[GFX]")
        add("tdc_soc", "SoC current limit (TDC)", pt["msg_tdc_a"]["soc"] or None, "A", 0, "MsgLimits.Tdc[SOC]")
        add("vmax_gfx", "GFX max voltage limit", pt["vmax_default_mv"]["gfx"] / 1000 or None, "V", 3,
            "SkuTable.DefaultMaxVoltage[GFX]")
        add("vmax_gfx_boost", "GFX boost max voltage limit", pt["vmax_boost_mv"]["gfx"] / 1000 or None, "V", 3,
            "SkuTable.BoostMaxVoltage[GFX] (with FIT controller)")
        for name, t in pt["temp_limits"].items():
            if t:
                add("temp_" + name.lower().replace(" ", "_"), f"{name} temperature limit", t, "°C", 0,
                    "CustomSkuTable.TemperatureLimit (throttle point)")
        for name, t in pt["fw_ctf_limits"].items():
            if t:
                add("ctf_" + name.lower().replace(" ", "_"), f"{name} shutdown threshold", t, "°C", 0,
                    "CustomSkuTable.FwCtfLimit (firmware thermal trip)")
        feats = pt["od_features"]
        add("od_features", "Overdrive features allowed by board", float(len(feats)), "", 0,
            "FeatureCtrlMask: " + (", ".join(feats) if feats else "none"))
        return out

    # ---- controls --------------------------------------------------------------------

    def _od_state(self) -> dict:
        """Collect everything needed for OD controls. Read-only."""
        path = os.path.join(self.dev, "pp_od_clk_voltage")
        st: dict[str, Any] = {"exists": os.path.exists(path), "writable": False, "params": [],
                              "notes": [], "reason": None}
        mask = self._ppfeaturemask()
        st["mask"] = mask
        if st["exists"]:
            st["writable"] = os.access(path, os.W_OK)
            text = SysfsIO.read(path) or ""
            od = odm.parse_od_text(text)
            params, notes = odm.build_clock_params(od)
            st["params"].extend(params)
            st["notes"].extend(notes)
        fan_dir = os.path.join(self.dev, odm.FAN_DIR)
        if os.path.isdir(fan_dir):
            texts = {}
            for f in ["fan_curve"] + [x[0] for x in odm.FAN_SINGLE_FILES]:
                fp = os.path.join(fan_dir, f)
                if os.path.exists(fp):
                    texts[f] = SysfsIO.read(fp) or ""
            st["params"].extend(odm.build_fan_params(texts))
        if not st["exists"]:
            if mask is not None and not mask & PP_OVERDRIVE_MASK:
                rec = mask | PP_OVERDRIVE_MASK
                st["reason"] = (
                    f"Overdrive is disabled in the amdgpu driver (ppfeaturemask=0x{mask:08x}, "
                    f"PP_OVERDRIVE_MASK 0x4000 clear). Add the kernel parameter "
                    f"amdgpu.ppfeaturemask=0x{rec:08x} (stock default 0xfff7bfff | 0x4000 = "
                    f"0xfff7ffff) and reboot. Note: the kernel is tainted while OD is enabled.")
            elif mask is None:
                st["reason"] = ("pp_od_clk_voltage not present. Overdrive needs the kernel parameter "
                                "amdgpu.ppfeaturemask=0xfff7ffff (sets PP_OVERDRIVE_MASK 0x4000).")
            else:
                st["reason"] = ("Overdrive bit is set but the driver exposes no pp_od_clk_voltage "
                                "for this GPU (OD unsupported on this ASIC/driver).")
        elif not st["writable"]:
            st["reason"] = "pp_od_clk_voltage is read-only for this user (meat3d must run as root)."
        return st

    def _placeholder_od(self) -> list[odm.OdParam]:
        """OD controls shown greyed out when overdrive is unavailable."""
        gc = self.gc or (0, 0, 0)
        mk = lambda key, label, unit, danger=1: odm.OdParam(  # noqa: E731
            key, label, unit, "pp_od_clk_voltage", None, None, None, "", danger=danger, available=False)
        if self.is_apu:
            return [mk("sclk_min", "GFX clock min", "MHz"), mk("sclk_max", "GFX clock max", "MHz")]
        if gc[0] >= 12:
            ps = [mk("sclk_offset", "GFX clock offset", "MHz"), mk("mclk_min", "Memory clock min", "MHz"),
                  mk("mclk_max", "Memory clock max", "MHz"),
                  mk("vddgfx_offset", "GFX voltage offset", "mV", 2)]
        elif gc[0] == 11 or gc[:2] == (10, 3):
            ps = [mk("sclk_min", "GFX clock min", "MHz"), mk("sclk_max", "GFX clock max", "MHz"),
                  mk("mclk_min", "Memory clock min", "MHz"), mk("mclk_max", "Memory clock max", "MHz"),
                  mk("vddgfx_offset", "GFX voltage offset", "mV", 2)]
        else:
            ps = [mk("sclk_max", "GFX clock max", "MHz"), mk("mclk_max", "Memory clock max", "MHz")]
        if gc[0] >= 11:
            for key, label, unit in (("fan_target_temp", "Fan target temperature", "°C"),
                                     ("fan_acoustic_limit", "Fan acoustic limit", "RPM"),
                                     ("fan_zero_rpm", "Fan zero-RPM mode", "")):
                p = mk(key, label, unit)
                p.file = f"{odm.FAN_DIR}/x"
                ps.append(p)
        return ps

    def _perf_level(self) -> Optional[str]:
        t = self._r("power_dpm_force_performance_level")
        return t.strip() if t else None

    def _power_caps(self) -> list[dict]:
        caps = []
        if not self.hwmon:
            return caps
        for f in sorted(glob.glob(os.path.join(self.hwmon, "power*_cap"))):
            m = re.match(r"power(\d+)_cap$", os.path.basename(f))
            if not m:
                continue
            n = m.group(1)
            rd = lambda s: _int(SysfsIO.read(os.path.join(self.hwmon, f"power{n}_{s}")))  # noqa: E731
            lab = (SysfsIO.read(os.path.join(self.hwmon, f"power{n}_label")) or f"power{n}").strip()
            caps.append({"n": n, "path": f, "label": lab, "cur": _int(SysfsIO.read(f)),
                         "min": rd("cap_min"), "max": rd("cap_max"), "default": rd("cap_default"),
                         "writable": os.access(f, os.W_OK)})
        return caps

    def _control_specs(self) -> list[tuple[Control, dict]]:
        """Build controls plus private apply metadata. Read-only."""
        P, G = self.prefix, self.name
        specs: list[tuple[Control, dict]] = []
        root_hint = " (read-only for this user: meat3d must run as root)"

        for c in self._power_caps():
            avail = c["min"] is not None and c["max"] is not None and c["max"] > 0
            w = lambda uw: None if uw is None else uw / 1e6  # noqa: E731
            hint = (f"hwmon power{c['n']}_cap ({c['label']}), driver range "
                    f"{w(c['min'])}-{w(c['max'])} W, stock {w(c['default'])} W.")
            if self.arch in ("RDNA3", "RDNA4"):
                hint += (" On RDNA3/4 the range above the board's message limit is only offered "
                         "with overdrive enabled (amdgpu.ppfeaturemask bit 0x4000).")
            if self.pptable and c["n"] == "1":
                hint += " " + ppt.power_cap_note(self.pptable)
            if not c["writable"]:
                avail = False
                hint += root_hint
            sid = "power_cap" if c["n"] == "1" else f"power{c['n']}_cap"
            specs.append((Control(f"{P}.oc.{sid}", G, f"Power limit ({c['label']})", "int",
                                  value=w(c["cur"]), min=w(c["min"]), max=w(c["max"]), step=1,
                                  unit="W", default=w(c["default"]), danger=1, hint=hint,
                                  available=avail),
                          {"type": "power_cap", "path": c["path"]}))

        lvl_path = os.path.join(self.dev, "power_dpm_force_performance_level")
        if os.path.exists(lvl_path):
            cur = self._perf_level()
            choices = list(PERF_LEVELS)
            if cur and cur not in choices:
                choices.append(cur)
            w_ok = os.access(lvl_path, os.W_OK)
            specs.append((Control(f"{P}.oc.perf_level", G, "Performance level", "choice", value=cur,
                                  choices=choices, default="auto", danger=0,
                                  hint="power_dpm_force_performance_level" + ("" if w_ok else root_hint),
                                  available=w_ok),
                          {"type": "perf_level", "path": lvl_path}))

        prof_path = os.path.join(self.dev, "pp_power_profile_mode")
        if os.path.exists(prof_path):
            profiles, active = parse_power_profiles(SysfsIO.read(prof_path))
            choices = [n for n in profiles if n != "CUSTOM"]
            w_ok = os.access(prof_path, os.W_OK)
            specs.append((Control(f"{P}.oc.power_profile", G, "Power profile", "choice", value=active,
                                  choices=choices,
                                  default="BOOTUP_DEFAULT" if "BOOTUP_DEFAULT" in profiles else None,
                                  danger=0, hint="pp_power_profile_mode (CUSTOM omitted: needs "
                                  "per-ASIC coefficient lists)" + ("" if w_ok else root_hint),
                                  available=w_ok and bool(choices)),
                          {"type": "profile", "path": prof_path, "profiles": profiles}))

        st = self._od_state()
        params = st["params"]
        od_avail = st["exists"] and st["writable"]
        if not params and not st["exists"]:
            params = self._placeholder_od()
        apu_note = (" APU: requires performance level 'manual' (set it first)." if self.is_apu else "")
        real = ppt.od_param_ranges(self.pptable) if self.pptable else {}
        for prm in params:
            if prm.key in real and prm.min is not None and prm.max is not None:
                lo, hi = max(prm.min, real[prm.key][0]), min(prm.max, real[prm.key][1])
                if lo <= hi and (lo, hi) != (prm.min, prm.max):
                    prm.hint += (f" OD_RANGE shows {prm.min}..{prm.max}, but the driver validates this field "
                                 f"against the board's pp_table range {lo}..{hi}; meat3 uses that.")
                    prm.min, prm.max = lo, hi
            avail = prm.available and od_avail and prm.min is not None and prm.max is not None
            hint = prm.hint
            if prm.min is not None and prm.max is not None:
                hint += f" Driver OD_RANGE {prm.min}..{prm.max} {prm.unit}."
            elif prm.available and st["exists"]:
                hint += " Driver reports no OD_RANGE for this setting; disabled."
            if not avail and st["reason"]:
                hint = (hint + " " + st["reason"]).strip()
            if prm.file.startswith(odm.FAN_DIR) and avail:
                hint += " Writing a fan setting switches the SMU fan mode accordingly."
            hint += apu_note
            kind = prm.kind
            val = prm.value
            dflt = prm.default
            if kind == "bool":
                val = None if val is None else bool(val)
                dflt = None if dflt is None else bool(dflt)
            specs.append((Control(f"{P}.oc.od.{prm.key}", G, prm.label + " (OD)", kind, value=val,
                                  min=prm.min, max=prm.max, step=1 if kind == "int" else None,
                                  unit=prm.unit, default=dflt, danger=prm.danger, hint=hint.strip(),
                                  available=avail),
                          {"type": "od", "param": prm}))
        if st["exists"] or params:
            avail = od_avail and bool(st["params"])
            specs.append((Control(f"{P}.oc.od_reset", G, "Reset overdrive to stock", "action",
                                  danger=0, available=avail,
                                  hint=("writes 'r' then 'c' to pp_od_clk_voltage (restores the boot "
                                        "OD table, including fan settings on SMU13/14)") +
                                  ("" if avail else " " + (st["reason"] or "no OD settings"))),
                          {"type": "od_reset"}))
        if st["notes"]:
            for c, _m in specs:
                if c.id.startswith(f"{P}.oc.od"):
                    c.hint += " [" + "; ".join(st["notes"]) + "]"
        return specs

    def controls(self) -> list[Control]:
        try:
            return [c for c, _ in self._control_specs()]
        except Exception:
            return []

    # ---- apply -----------------------------------------------------------------------

    def _write(self, path: str, data: str) -> None:
        self.io.write(path, data)

    def apply(self, control_id: str, value: Any) -> tuple[bool, str]:
        try:
            specs = {c.id: (c, meta) for c, meta in self._control_specs()}
        except Exception as e:  # pragma: no cover
            return False, f"cannot read current state: {e}"
        if control_id not in specs:
            return False, f"unknown control {control_id!r}"
        c, meta = specs[control_id]
        if not c.available:
            return False, f"{c.label} is not available: {c.hint}"

        # ---- validate ----
        if c.kind == "action":
            v = None
        elif c.kind == "choice":
            if not isinstance(value, str) or value not in c.choices:
                return False, f"invalid choice {value!r}; expected one of {c.choices}"
            v = value
        elif c.kind == "bool":
            if isinstance(value, bool):
                v = int(value)
            elif isinstance(value, (int, float)) and value in (0, 1):
                v = int(value)
            else:
                return False, f"{c.label}: expected a boolean, got {value!r}"
        else:
            f = _num(value)
            if f is None or f != f or f in (float("inf"), float("-inf")):
                return False, f"{c.label}: expected a number, got {value!r}"
            if c.kind == "int" and f != int(f):
                return False, f"{c.label}: expected an integer, got {value!r}"
            if c.min is None or c.max is None:
                return False, f"{c.label}: no valid range known; refusing"
            if f < c.min or f > c.max:
                return False, f"{c.label}: {value} {c.unit} outside allowed range {c.min:g}..{c.max:g} {c.unit}"
            v = int(f) if c.kind == "int" else f
        if c.kind == "bool" and c.min is not None and c.max is not None and not (c.min <= v <= c.max):
            return False, f"{c.label}: {v} outside allowed range {c.min:g}..{c.max:g}"

        try:
            t = meta["type"]
            if t == "power_cap":
                uw = int(round(v * 1_000_000))
                self._write(meta["path"], f"{uw}\n")
                back = _int(SysfsIO.read(meta["path"]))
                if back is not None and abs(back - uw) >= 1_000_000:
                    return True, (f"power limit requested {v} W, driver applied {back / 1e6:g} W "
                                  f"(above the firmware limit it is set as a whole-% overdrive step)")
                return True, f"power limit set to {v} W"
            if t == "perf_level":
                self._write(meta["path"], f"{v}\n")
                return True, f"performance level set to {v}"
            if t == "profile":
                idx = meta["profiles"][v]
                self._write(meta["path"], f"{idx}\n")
                return True, f"power profile set to {v} ({idx})"
            if t == "od_reset":
                path = os.path.join(self.dev, "pp_od_clk_voltage")
                self._write(path, "r\n")
                self._write(path, "c\n")
                return True, "overdrive settings restored to stock and committed"
            if t == "od":
                return self._apply_od(meta["param"], v, specs)
        except OSError as e:
            return False, f"write failed: {e.strerror or (os.strerror(e.errno) if e.errno else e)} " \
                          f"(driver rejected the value or insufficient permissions)"
        return False, "not supported"

    def _apply_od(self, prm: odm.OdParam, v: int, specs: dict) -> tuple[bool, str]:
        P = self.prefix
        if self.is_apu and self._perf_level() != "manual":
            return False, ("APU overdrive edits are only accepted by the driver in 'manual' "
                           "performance level; set Performance level = manual first")
        # cross-field constraints the driver also enforces
        for a, b in (("sclk_min", "sclk_max"), ("mclk_min", "mclk_max"), ("fclk_min", "fclk_max")):
            if prm.key == a and f"{P}.oc.od.{b}" in specs:
                other = specs[f"{P}.oc.od.{b}"][0].value
                if other is not None and v > other:
                    return False, f"{prm.label} {v} would exceed current max {other} {prm.unit}"
            if prm.key == b and f"{P}.oc.od.{a}" in specs:
                other = specs[f"{P}.oc.od.{a}"][0].value
                if other is not None and v < other:
                    return False, f"{prm.label} {v} would be below current min {other} {prm.unit}"
        # fan curve points write temp+pwm together: the companion must be in range too
        pair = prm.extra.get("pair")
        if pair:
            comp_key = prm.key.rsplit("_", 1)[0] + "_" + pair
            comp = specs.get(f"{P}.oc.od.{comp_key}")
            cv = prm.extra.get(pair)
            if comp is not None:
                cc = comp[0]
                if cv is None or cc.min is None or cc.max is None or not (cc.min <= cv <= cc.max):
                    return False, (f"{cc.label} is currently {cv} {cc.unit}, outside {cc.min}..{cc.max}; "
                                   f"set it first (fan curve points are written as temp+speed pairs)")
        path = os.path.join(self.dev, prm.file)
        cmd = prm.command(v)
        km = _KmsgTail()
        try:
            self._write(path, cmd + "\n")
            self._write(path, "c\n")
        except OSError as e:
            why = km.amdgpu_lines()
            if why:
                raise OSError(e.errno, f"{os.strerror(e.errno) if e.errno else e}; driver said: {why}") from e
            raise
        finally:
            km.close()
        return True, f"{prm.label} set to {v} {prm.unit} ('{cmd}' + commit)"


def _main(argv: list[str]) -> int:
    root = argv[1] if len(argv) > 1 else None
    devs = AmdGpuBackend.detect(root)
    if not devs:
        print("no amdgpu devices found")
        return 1
    for d in devs:
        print(f"== {d.name}  slot={d.slot} id=1002:{d.device_id:04x} rev={d.revision} "
              f"GC={d.gc} MP1={d.mp1} arch={d.arch} smc_fw={d.smc_fw and hex(d.smc_fw)} "
              f"vbios={d.vbios} gpu_metrics={d.metrics_version} apu={d.is_apu}")
        t0 = time.perf_counter()
        ss = d.sensors()
        dt = (time.perf_counter() - t0) * 1000
        grp = None
        for s in ss:
            g = s.group.split("/", 1)[1]
            if g != grp:
                print(f"  [{g}]")
                grp = g
            val = "n/a" if s.value is None else f"{s.value:.{s.decimals}f}"
            print(f"    {s.label:<34} {val:>10} {s.unit:<6} {s.id}")
        print(f"  ({len(ss)} sensors in {dt:.1f} ms)")
        print("  [Controls]")
        for c in d.controls():
            rng = "" if c.min is None else f" [{c.min:g}..{c.max:g}]"
            ch = f" {c.choices}" if c.choices else ""
            print(f"    {c.label:<40} {str(c.value):>14} {c.unit}{rng}{ch} "
                  f"{'avail' if c.available else 'UNAVAILABLE'} :: {c.hint[:150]}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
