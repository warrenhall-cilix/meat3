"""AMD SMU PM table layouts (ryzen_smu /sys/kernel/ryzen_smu_drv/pm_table, float32 array).

Indices are float32 indices (byte offset / 4). Each field carries a `status`:
  "validated"  - cross-checked on a real 9800X3D (PM table 0x620105, SMU 98.84.0) against an
                 independent source (k10temp, amdgpu, UMC PMU memclk, MSR energy/APERF, RAPL,
                 MSR APERF/MPERF/TSC) with tools/validate_cpu_amd.py: PPT & socket power = RAPL
                 package (+-0.2 W), Tctl = k10temp (+-0.4 °C), MCLK = umc_mem_clk rate (3000 vs
                 2994 MHz), per-core power [301+] = MSR core energy (+-0.01 W), per-core C0
                 [341+] = dMPERF/dTSC (+-0.5 %), iGPU clock [108] = amdgpu sclk.
  "documented" - taken from open-source projects, not independently confirmed here.
  "inferred"   - pattern-based / projects disagree; shown with a caveat in the hint.

Sources:
  GR  = Granite Ridge 0x620105 / 0x620205: Kyworn/gnr-smu profiles.py + docs/architectures/
        granite_ridge/*.md, intelfx/ryzen_monitor src/pm_tables_zen5.c, amkillam/ryzen_smu
        PR #67 (userspace/monitor_cpu.c), irusanov/ZenStates-Core Hardware/PowerTable.cs.
  RPL = Raphael 0x540104/0x540004: intelfx/ryzen_monitor src/pm_tables_zen4.c, ZenStates-Core.
  VMR/MTS = Vermeer 0x380905/0x380805, Matisse 0x240903: mann1x/ryzen_monitor_ng src/pm_tables.c.
Unknown versions: only FCLK/UCLK/MCLK at the family's usual indices, and only when the values
pass sanity ranges (and are mutually consistent); MEAT3_PMT_RAW=1 adds every float as a row.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

from meat3.core.model import Sensor


@dataclass(frozen=True)
class F:
    key: str
    label: str
    idx: int
    unit: str
    dec: int
    sub: str               # sub-group under the CPU node
    status: str = "documented"
    scale: float = 1.0
    note: str = ""


@dataclass(frozen=True)
class CoreBlock:
    key: str
    label: str
    base: int
    unit: str
    dec: int
    sub: str
    status: str = "documented"
    scale: float = 1.0
    note: str = ""


S_LIM, S_PWR, S_VOLT, S_CLK, S_TEMP, S_CORE = ("SMU limits", "Power", "Voltages", "Infinity Fabric & Memory",
                                               "Temperatures", "Cores (PM table)")

# ---------------------------------------------------------------------- Granite Ridge (Zen 5)
_GR_COMMON = [
    F("ppt_limit", "PPT limit", 2, "W", 0, S_LIM, "documented", note="gnr-smu write-back test; stock 162 W on 9800X3D"),
    F("ppt", "PPT (socket power)", 3, "W", 1, S_LIM, "validated", note="tracks RAPL package power"),
    F("tdc_limit", "TDC limit (VDD)", 8, "A", 0, S_LIM, "documented"),
    F("tdc", "TDC (VDD current)", 9, "A", 1, S_LIM, "documented", note="equals SVI3 VDD current (idx 50)"),
    F("thm_limit", "Thermal limit", 10, "°C", 0, S_LIM, "documented"),
    F("thm", "Tctl (SMU)", 11, "°C", 1, S_LIM, "validated", note="matches k10temp Tctl"),
    F("edc_limit", "EDC limit", 63, "A", 0, S_LIM, "documented", note="gnr-smu write-back test; no EDC value field"),
    F("vid_peak", "Core VID peak", 18, "V", 4, S_VOLT, "documented"),
    F("vid", "Core VID (avg)", 19, "V", 4, S_VOLT, "documented"),
    F("vddcr_cpu_power", "VDDCR_CPU power", 20, "W", 2, S_PWR, "documented", note="= SVI3 VDD rail power (idx 51); includes L3/uncore on the VDD rail"),
    F("vddcr_soc_power", "VDDCR_SOC power", 21, "W", 2, S_PWR, "documented", note="= SVI3 SOC rail power (idx 56)"),
    F("vdd_misc_power", "VDD_MISC power", 22, "W", 2, S_PWR, "documented", note="= SVI3 MISC rail power (idx 61)"),
    F("socket_power", "Socket power (SMU)", 26, "W", 1, S_PWR, "validated", note="= RAPL package power"),
    F("svi3_vdd_vid", "VDDCR_CPU VID (SVI3)", 48, "V", 4, S_VOLT, "documented"),
    F("vddcr_cpu", "VDDCR_CPU (SVI3)", 49, "V", 4, S_VOLT, "documented", note="SVI3 telemetry, VDD rail"),
    F("vddcr_cpu_cur", "VDDCR_CPU current", 50, "A", 1, S_PWR, "documented"),
    F("vddcr_cpu_vrm_temp", "VDDCR_CPU VRM temp", 52, "°C", 1, S_TEMP, "documented"),
    F("svi3_soc_vid", "VDDCR_SOC VID (SVI3)", 53, "V", 4, S_VOLT, "documented"),
    F("vddcr_soc", "VDDCR_SOC (SVI3)", 54, "V", 4, S_VOLT, "documented"),
    F("vddcr_soc_cur", "VDDCR_SOC current", 55, "A", 1, S_PWR, "documented"),
    F("vddcr_soc_vrm_temp", "VDDCR_SOC VRM temp", 57, "°C", 1, S_TEMP, "documented"),
    F("vdd_misc", "VDD_MISC (SVI3)", 59, "V", 4, S_VOLT, "documented",
      note="third SVI3 rail (ZenStates 'VddSocMisc'); not VDDIO_MEM"),
    F("vdd_misc_cur", "VDD_MISC current", 60, "A", 1, S_PWR, "documented"),
    F("vdd_misc_vrm_temp", "VDD_MISC VRM temp", 62, "°C", 1, S_TEMP, "documented"),
    F("fclk", "FCLK (Infinity Fabric)", 71, "MHz", 0, S_CLK, "documented", note="all sources agree; no independent counter"),
    F("uclk", "UCLK (memory controller)", 75, "MHz", 0, S_CLK, "documented", note="UCLK=MCLK (1:1) on test box"),
    F("mclk", "MCLK (memory clock)", 79, "MHz", 0, S_CLK, "validated", note="= amd_umc umc_mem_clk rate"),
    F("vsoc_set", "VDDCR_SOC setpoint", 83, "V", 3, S_VOLT, "inferred", note="static; equals BIOS SoC voltage"),
    F("cldo_vddp", "CLDO VDDP", 269, "V", 4, S_VOLT, "inferred",
      note="ZenStates: CLDO_VDDP; gnr-smu labels it differently"),
    F("hotspot", "Hotspot temperature", 270, "°C", 1, S_TEMP, "documented", note="spiky; max sensor"),
]

GRANITE_RIDGE_1CCD = _GR_COMMON + [
    F("igpu_clk", "iGPU clock", 108, "MHz", 0, S_CLK, "validated", note="matches amdgpu sclk"),
    F("igpu_power", "iGPU power", 107, "W", 2, S_PWR, "documented"),
    F("l3_temp", "L3 / CCD temperature", 448, "°C", 1, S_TEMP, "documented"),
    F("core_clk_peak", "Peak core clock (SMU)", 450, "MHz", 0, S_CORE, "documented", scale=1000.0),
    F("core_clk_avg", "Average core clock (SMU)", 451, "MHz", 0, S_CORE, "documented", scale=1000.0),
]
GRANITE_RIDGE_1CCD_CORES = [
    # 301 was labelled "current" by gnr-smu, but on a 9800X3D it matches the MSR 0xC001029A core
    # energy rate to +-0.01 W on every core -> it is per-core power. Block 333 (gnr: "power") does
    # NOT match any independent source and is left to the raw dump.
    CoreBlock("power", "power (SMU)", 301, "W", 2, S_CORE, "validated", note="= MSR core energy rate"),
    CoreBlock("volt", "voltage", 309, "V", 4, S_CORE, "documented"),
    CoreBlock("temp", "temperature", 317, "°C", 1, S_CORE, "documented"),
    CoreBlock("clk", "clock (SMU)", 325, "MHz", 0, S_CORE, "documented", scale=1000.0,
              note="SMU core frequency target, not an APERF average"),
    CoreBlock("c0", "C0 residency (SMU)", 341, "%", 1, S_CORE, "validated", note="= MSR dMPERF/dTSC"),
    CoreBlock("cc1", "CC1 residency", 349, "%", 1, S_CORE, "documented"),
    CoreBlock("cc6", "CC6 residency", 357, "%", 1, S_CORE, "documented"),
    CoreBlock("fmax", "boost limit", 373, "MHz", 0, S_CORE, "documented", scale=1000.0),
]

GRANITE_RIDGE_2CCD = _GR_COMMON + [
    F("l3_temp_ccd0", "CCD0 L3 temperature", 595, "°C", 1, S_TEMP, "inferred"),
    F("l3_temp_ccd1", "CCD1 L3 temperature", 596, "°C", 1, S_TEMP, "inferred"),
]
GRANITE_RIDGE_2CCD_CORES = [
    # 301: intelfx/PR#67 say "current", togi says "power"; on 0x620105 the same block is
    # validated as per-core power, so power it is. 365 (intelfx: power, gnr: activity) omitted.
    CoreBlock("power", "power (SMU)", 301, "W", 2, S_CORE, "inferred", note="by analogy with validated 0x620105"),
    CoreBlock("volt", "voltage", 317, "V", 4, S_CORE, "documented"),
    CoreBlock("temp", "temperature", 333, "°C", 1, S_CORE, "documented"),
    CoreBlock("clk", "clock (SMU)", 349, "MHz", 0, S_CORE, "inferred", scale=1000.0,
              note="intelfx: effective freq; gnr-smu disagrees"),
    CoreBlock("c0", "C0 residency (SMU)", 381, "%", 1, S_CORE, "documented"),
    CoreBlock("cc1", "CC1 residency", 397, "%", 1, S_CORE, "documented"),
    CoreBlock("cc6", "CC6 residency", 413, "%", 1, S_CORE, "documented"),
]

# ---------------------------------------------------------------------- Raphael (Zen 4)
RAPHAEL = [
    F("ppt_limit", "PPT limit", 2, "W", 0, S_LIM), F("ppt", "PPT (socket power)", 3, "W", 1, S_LIM),
    F("tdc_limit", "TDC limit", 8, "A", 0, S_LIM), F("tdc", "TDC", 9, "A", 1, S_LIM),
    F("thm_limit", "Thermal limit", 10, "°C", 0, S_LIM), F("thm", "Tctl (SMU)", 11, "°C", 1, S_LIM),
    F("edc_limit", "EDC limit", 61, "A", 0, S_LIM), F("edc", "EDC", 49, "A", 1, S_LIM, "inferred"),
    F("vddcr_soc", "VDDCR_SOC", 52, "V", 4, S_VOLT), F("socket_power", "Socket power (SMU)", 26, "W", 1, S_PWR),
    F("fclk", "FCLK (Infinity Fabric)", 70, "MHz", 0, S_CLK), F("uclk", "UCLK (memory controller)", 74, "MHz", 0, S_CLK),
    F("mclk", "MCLK (memory clock)", 78, "MHz", 0, S_CLK), F("cldo_vddp", "CLDO VDDP", 268, "V", 4, S_VOLT),
]
RAPHAEL_1CCD_CORES = [
    CoreBlock("power", "power (SMU)", 293, "W", 2, S_CORE), CoreBlock("volt", "voltage", 301, "V", 4, S_CORE),
    CoreBlock("temp", "temperature", 309, "°C", 1, S_CORE),
    CoreBlock("clk", "clock (SMU)", 317, "MHz", 0, S_CORE, scale=1000.0),
    CoreBlock("clk_eff", "effective clock (SMU)", 325, "MHz", 0, S_CORE, scale=1000.0),
    CoreBlock("c0", "C0 residency (SMU)", 333, "%", 1, S_CORE), CoreBlock("cc1", "CC1 residency", 341, "%", 1, S_CORE),
    CoreBlock("cc6", "CC6 residency", 349, "%", 1, S_CORE),
]

# ---------------------------------------------------------------------- Vermeer / Matisse (Zen 3 / Zen 2)
_ZEN23 = [
    F("ppt_limit", "PPT limit", 0, "W", 0, S_LIM), F("ppt", "PPT (socket power)", 1, "W", 1, S_LIM),
    F("tdc_limit", "TDC limit", 2, "A", 0, S_LIM), F("tdc", "TDC", 3, "A", 1, S_LIM),
    F("thm_limit", "Thermal limit", 4, "°C", 0, S_LIM), F("thm", "Tctl (SMU)", 5, "°C", 1, S_LIM),
    F("edc_limit", "EDC limit", 8, "A", 0, S_LIM), F("edc", "EDC", 9, "A", 1, S_LIM),
    F("vddcr_soc_set", "VDDCR_SOC setpoint", 44, "V", 4, S_VOLT), F("vddcr_soc", "VDDCR_SOC", 45, "V", 4, S_VOLT),
    F("fclk", "FCLK (Infinity Fabric)", 48, "MHz", 0, S_CLK), F("uclk", "UCLK (memory controller)", 50, "MHz", 0, S_CLK),
    F("mclk", "MCLK (memory clock)", 51, "MHz", 0, S_CLK),
]
VERMEER = _ZEN23 + [F("vddm", "CLDO VDDM", 136, "V", 4, S_VOLT), F("cldo_vddp", "CLDO VDDP", 137, "V", 4, S_VOLT),
                    F("vddg_iod", "CLDO VDDG IOD", 138, "V", 4, S_VOLT), F("vddg_ccd", "CLDO VDDG CCD", 139, "V", 4, S_VOLT)]
MATISSE = _ZEN23 + [F("vddm", "CLDO VDDM", 124, "V", 4, S_VOLT), F("cldo_vddp", "CLDO VDDP", 125, "V", 4, S_VOLT),
                    F("vddg", "CLDO VDDG", 126, "V", 4, S_VOLT)]


def _z23_cores(power: int, stride: int) -> list[CoreBlock]:
    b = [("power", "power (SMU)", "W", 2, 1.0), ("volt", "voltage", "V", 4, 1.0), ("temp", "temperature", "°C", 1, 1.0)]
    out = [CoreBlock(k, lbl, power + i * stride, u, d, S_CORE, scale=s) for i, (k, lbl, u, d, s) in enumerate(b)]
    # frequency blocks start after one more (unused) block
    out += [CoreBlock("clk", "clock (SMU)", power + 5 * stride, "MHz", 0, S_CORE, scale=1000.0),
            CoreBlock("clk_eff", "effective clock (SMU)", power + 6 * stride, "MHz", 0, S_CORE, scale=1000.0),
            CoreBlock("c0", "C0 residency (SMU)", power + 7 * stride, "%", 1, S_CORE),
            CoreBlock("cc1", "CC1 residency", power + 8 * stride, "%", 1, S_CORE),
            CoreBlock("cc6", "CC6 residency", power + 9 * stride, "%", 1, S_CORE)]
    return out


@dataclass(frozen=True)
class Layout:
    name: str
    size: int                 # bytes (0 = don't check)
    fields: list
    cores: list
    slots: int                # per-core block width
    source: str


LAYOUTS: dict[int, Layout] = {
    0x620105: Layout("Granite Ridge 1-CCD", 0x724, GRANITE_RIDGE_1CCD, GRANITE_RIDGE_1CCD_CORES, 8,
                     "gnr-smu, ZenStates-Core, validated on 9800X3D"),
    0x620205: Layout("Granite Ridge 2-CCD", 0x994, GRANITE_RIDGE_2CCD, GRANITE_RIDGE_2CCD_CORES, 16,
                     "intelfx/ryzen_monitor, ryzen_smu PR#67, gnr-smu"),
    0x540104: Layout("Raphael 1-CCD", 0x6A8, RAPHAEL, RAPHAEL_1CCD_CORES, 8, "intelfx/ryzen_monitor zen4"),
    0x540004: Layout("Raphael 2-CCD", 0x8BC, RAPHAEL, [], 16, "intelfx/ryzen_monitor zen4 (per-core omitted: guessed)"),
    0x380905: Layout("Vermeer 1-CCD", 0, VERMEER, _z23_cores(172, 8), 8, "ryzen_monitor_ng"),
    0x380805: Layout("Vermeer 2-CCD", 0, VERMEER, _z23_cores(172, 16), 16, "ryzen_monitor_ng"),
    0x240903: Layout("Matisse", 0, MATISSE, _z23_cores(147, 8), 8, "ryzen_monitor_ng"),
}

# Family fallback for unknown versions: (version >> 16) -> (fclk, uclk, mclk) indices.
FAMILY_CLOCKS = {0x62: (71, 75, 79), 0x54: (70, 74, 78), 0x38: (48, 50, 51), 0x24: (48, 50, 51)}
CLOCK_RANGES = {"fclk": (400.0, 3600.0), "uclk": (400.0, 5000.0), "mclk": (400.0, 5000.0)}


def check_clocks(fclk: Optional[float], uclk: Optional[float], mclk: Optional[float]) -> bool:
    """Sanity check used for unknown table versions."""
    vals = {"fclk": fclk, "uclk": uclk, "mclk": mclk}
    for k, v in vals.items():
        lo, hi = CLOCK_RANGES[k]
        if v is None or not (lo <= v <= hi):
            return False
    # UCLK is MCLK or MCLK/2 on every Zen with a supported table
    return abs(uclk - mclk) < 1.0 or abs(uclk * 2 - mclk) < 2.0


class PmTableReader:
    def __init__(self, smu, ncores: int, raw: bool = False):
        self.smu = smu
        self.ncores = ncores
        self.raw = raw
        self.version: Optional[int] = None
        self.size: Optional[int] = None
        self.layout: Optional[Layout] = None
        self.slots: Optional[list[int]] = None      # live physical slots, index = logical core #
        self.error = ""
        self.last: Optional[tuple] = None
        self._init()

    def _init(self) -> None:
        if not self.smu.present():
            self.error = "ryzen_smu not loaded (modprobe ryzen_smu)"
            return
        self.version = self.smu.pm_table_version()
        self.size = self.smu.pm_table_size()
        if self.version is None:
            self.error = "pm_table_version unreadable (unsupported CPU/SMU?)"
            return
        lay = LAYOUTS.get(self.version)
        if lay and lay.size and self.size and lay.size != self.size:
            self.error = f"size {self.size:#x} != expected {lay.size:#x} for {self.version:#08x}"
            lay = None
        self.layout = lay

    def version_str(self) -> str:
        if self.version is None:
            return "n/a"
        return f"{self.version:#08x}" + (f" ({self.layout.name})" if self.layout else " (unknown layout)")

    def read(self) -> Optional[tuple]:
        if self.version is None:
            return None
        b = self.smu.read_pm_table()
        if not b or len(b) < 16:
            self.error = self.smu.error or "pm_table read failed"
            return None
        n = len(b) // 4
        self.last = struct.unpack(f"<{n}f", b[: n * 4])
        return self.last

    def _detect_slots(self, t: tuple) -> list[int]:
        lay = self.layout
        volt = next((c for c in lay.cores if c.key == "volt"), None)
        clk = next((c for c in lay.cores if c.key in ("clk", "fmax")), None)
        live = []
        for s in range(lay.slots):
            vals = [t[c.base + s] for c in (volt, clk) if c is not None and c.base + s < len(t)]
            if any(v and v == v and v > 0 for v in vals):      # skip 0.0 and NaN (fused slot)
                live.append(s)
        return live

    def sensors(self, prefix: str, g: str) -> list[Sensor]:
        out: list[Sensor] = []
        t = self.read()
        base_hint = f"SMU PM table {self.version_str()}"
        if self.version is None:
            out.append(Sensor(f"{prefix}.pmt.fclk", f"{g}/{S_CLK}", "FCLK (Infinity Fabric)", None, "MHz", 0,
                              self.error))
            return out
        if self.layout is None:
            idx = FAMILY_CLOCKS.get((self.version >> 16) & 0xFF)
            vals = [None, None, None]
            if t is not None and idx and max(idx) < len(t):
                vals = [t[i] for i in idx]
            ok = check_clocks(*vals)
            hint = (f"{base_hint}: unknown version, family-default index "
                    + ("passed sanity check (inferred)" if ok else "failed sanity check / unavailable"))
            for key, label, v in zip(("fclk", "uclk", "mclk"),
                                     ("FCLK (Infinity Fabric)", "UCLK (memory controller)", "MCLK (memory clock)"), vals):
                out.append(Sensor(f"{prefix}.pmt.{key}", f"{g}/{S_CLK}", label, v if ok else None, "MHz", 0, hint))
        else:
            lay = self.layout
            for f in lay.fields:
                v = t[f.idx] * f.scale if t is not None and f.idx < len(t) else None
                if v is not None and v != v:   # NaN
                    v = None
                hint = f"{base_hint} [{f.idx}] {f.status}" + (f"; {f.note}" if f.note else "")
                out.append(Sensor(f"{prefix}.pmt.{f.key}", f"{g}/{f.sub}", f.label, v, f.unit, f.dec, hint))
            if lay.cores:
                if self.slots is None and t is not None:
                    self.slots = self._detect_slots(t)
                slots = self.slots or []
                for blk in lay.cores:
                    for k, s in enumerate(slots):
                        i = blk.base + s
                        v = t[i] * blk.scale if t is not None and i < len(t) else None
                        if v is not None and v != v:
                            v = None
                        name = f"Core {k}" if len(slots) == self.ncores else f"Slot {s}"
                        hint = (f"{base_hint} [{blk.base}+{s}] {blk.status}" + (f"; {blk.note}" if blk.note else ""))
                        out.append(Sensor(f"{prefix}.pmt.core{k}.{blk.key}", f"{g}/{blk.sub}",
                                          f"{name} {blk.label}", v, blk.unit, blk.dec, hint))
        if self.raw and t is not None:
            for i, v in enumerate(t):
                out.append(Sensor(f"{prefix}.pmt.raw.{i:03d}", f"{g}/PM table raw", f"[{i:3d}] +{i * 4:#05x}",
                                  None if v != v else v, "", 4, "raw float32 (MEAT3_PMT_RAW=1)"))
        return out

    def raw_dump(self) -> str:
        t = self.read()
        if t is None:
            return f"pm table unavailable: {self.error}"
        lines = [f"# PM table {self.version_str()} size={self.size} floats={len(t)}"]
        named = {}
        if self.layout:
            for f in self.layout.fields:
                named[f.idx] = f.key
            for blk in self.layout.cores:
                for s in range(self.layout.slots):
                    named.setdefault(blk.base + s, f"{blk.key}[{s}]")
        for i, v in enumerate(t):
            lines.append(f"{i:4d} {i * 4:#06x} {v:14.6f} {named.get(i, '')}")
        return "\n".join(lines)

    def limit(self, key: str) -> Optional[float]:
        """Current PPT/TDC/EDC limit from the table (used as Control.value)."""
        if not self.layout:
            return None
        f = next((f for f in self.layout.fields if f.key == key), None)
        t = self.last if self.last is not None else self.read()
        if f is None or t is None or f.idx >= len(t):
            return None
        return t[f.idx]
