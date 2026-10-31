"""NVIDIA GPU backend: NVML + RM controls (patched open module) + ASUS Astral per-pin monitor.

Standard data and OC via NVML (pynvml). Extra data via the RM client (nv_rm):
- volt rails status (NVVDD/MSVDD live voltage and REL/ALT/OV limits)   [patched module]
- per-domain clock read (XBAR/SYS/HUB/NVD actual+target)                  [stock-served]
- per-domain frequency offsets (XBAR/SYS/...) and rail Vrel offsets       [patched module]
- documented thermal-system sensors                                        [stock]
Board power is shown raw and corrected by the configured shunt factor (config.shunt_factor).
"""
from __future__ import annotations

import os
import time

from ..core import config
from ..core.model import Backend, Control, Sensor
from . import nv_rm
from .asus_pins import AstralPins, find_bus, is_astral

try:
    import pynvml as N
except ImportError:  # pragma: no cover
    N = None

THROTTLE_BITS = [
    (0x1, "GPU idle"), (0x2, "Application clocks"), (0x4, "SW power cap"), (0x8, "HW slowdown"),
    (0x10, "Sync boost"), (0x20, "SW thermal"), (0x40, "HW thermal"), (0x80, "HW power brake"),
    (0x100, "Display clock"), (0x400, "Reliability (voltage)"),
]


def _try(fn, *a, default=None):
    try:
        return fn(*a)
    except Exception:
        return default


def module_routes(marker: bytes) -> bool:
    """True when the loaded nvidia.ko carries `marker` (a meat3/nvoc route-entry name): a cheap, read-only
    way to know which patched-module level is running without issuing the control."""
    try:
        import subprocess
        path = subprocess.run(["modinfo", "-n", "nvidia"], capture_output=True, text=True, timeout=5).stdout.strip()
        if not path or not os.path.exists(path):
            return False
        with open(path, "rb") as f:
            return marker in f.read()
    except Exception:
        return False


class NvidiaGPU(Backend):
    kind = "gpu"

    @classmethod
    def detect(cls) -> list["NvidiaGPU"]:
        if N is None:
            return []
        try:
            N.nvmlInit()
            n = N.nvmlDeviceGetCount()
        except Exception:
            return []
        out = []
        for i in range(n):
            try:
                out.append(cls(i))
            except Exception as e:  # pragma: no cover
                print(f"meat3: nvidia gpu {i} init failed: {e}")
        return out

    def __init__(self, index: int):
        self.i = index
        self.h = N.nvmlDeviceGetHandleByIndex(index)
        self.gpu_name = _try(N.nvmlDeviceGetName, self.h, default=f"NVIDIA GPU {index}")
        if isinstance(self.gpu_name, bytes):
            self.gpu_name = self.gpu_name.decode()
        pci = N.nvmlDeviceGetPciInfo(self.h)
        bus = pci.busId.decode() if isinstance(pci.busId, bytes) else pci.busId
        self.pci = bus.lower()[-12:] if len(bus) > 12 else bus.lower()      # "0000:01:00.0"
        self.name = f"GPU {index}: {self.gpu_name}"
        self.p = f"nv{index}"
        self.lock_max = None
        self.rm = None
        self.domains = {}            # domain bit -> object index / ranges
        self.patched = False
        self.vf_set = False          # module v4+: the client VF-point setter (0x2080d02d) is routed to GSP
        try:
            self.rm = nv_rm.RMClient(index)
            self.domains = {d["domain"]: d for d in self.rm.clock_domains()}
            self.patched = self.rm.volt_rails_status() is not None
            self.vf_set = self.patched and module_routes(b"nvre_clkVfControlSet")
        except Exception as e:
            print(f"meat3: RM client unavailable for GPU {index}: {e}")
            self.rm = None
        self.pins = None
        pdev = f"/sys/bus/pci/devices/{self.pci}"
        if is_astral(pdev):
            short = f"{int(self.pci[5:7], 16)}:{self.pci[8:]}"
            dev = find_bus(short)
            if dev:
                try:
                    self.pins = AstralPins(dev)
                except OSError as e:
                    print(f"meat3: Astral pin monitor unavailable: {e}")
        self.nfans = _try(N.nvmlDeviceGetNumFans, self.h, default=0) or 0
        # Blackwell (GB20x) => THERM block at 0x00AD0000 readable through GPU_EXEC_REG_OPS (root)
        arch = _try(N.nvmlDeviceGetArchitecture, self.h)
        bw_const = getattr(N, "NVML_DEVICE_ARCH_BLACKWELL", 10)
        self.blackwell = arch is not None and arch == bw_const and os.geteuid() == 0
        if self.blackwell and self.rm:
            test = _try(self.rm.blackwell_temps)
            self.blackwell = bool(test and test.get("hotspot") is not None)
        print(f"meat3: {self.name}: Blackwell hotspot registers {'available' if self.blackwell else 'not available'}")

    # ------------------------------------------------------------------ sensors
    def sensors(self) -> list[Sensor]:
        h, p, g = self.h, self.p, self.name
        S: list[Sensor] = []
        add = lambda sid, grp, label, val, unit="", dec=0, hint="": S.append(
            Sensor(f"{p}.{sid}", f"{g}/{grp}", label, val, unit, dec, hint))

        # clocks
        for sid, label, ct in (("clk.gpc", "GPC (core) clock", N.NVML_CLOCK_GRAPHICS),
                               ("clk.sm", "SM clock", N.NVML_CLOCK_SM),
                               ("clk.mem", "Memory clock", N.NVML_CLOCK_MEM),
                               ("clk.video", "Video clock", N.NVML_CLOCK_VIDEO)):
            add(sid, "Clocks", label, _try(N.nvmlDeviceGetClockInfo, h, ct), "MHz")
        mem = _try(N.nvmlDeviceGetClockInfo, h, N.NVML_CLOCK_MEM)
        add("clk.mem_eff", "Clocks", "Memory effective rate", mem * 2 / 1000 if mem else None, "Gbps", 1,
            "GDDR7: nvidia-smi MHz x 2 / 1000")
        if self.rm:
            for dom, sid in ((0x2, "xbar"), (0x4, "sys"), (0x8, "hub"), (0x100000, "nvd")):
                r = _try(self.rm.clock_read, dom)
                if r:
                    add(f"clk.{sid}", "Clocks", f"{nv_rm.DOMAIN_NAMES[dom]} clock", r[0], "MHz")
                    add(f"clk.{sid}_tgt", "Clocks", f"{nv_rm.DOMAIN_NAMES[dom]} target", r[1], "MHz")
        add("clk.gpc_max", "Clocks", "GPC max (curve top)", _try(N.nvmlDeviceGetMaxClockInfo, h, N.NVML_CLOCK_GRAPHICS), "MHz")

        # voltages
        if self.rm and self.patched:
            vs = _try(self.rm.volt_rails_status)
            if vs:
                for k, label in (("nvvdd", "NVVDD (core)"), ("msvdd", "MSVDD (XBAR/SYS/mem ctrl)")):
                    add(f"v.{k}", "Voltages", label, vs[k], "mV")
                    add(f"v.{k}_rel", "Voltages/Limits", f"{label} REL limit", vs[k + "_rel"], "mV")
                    add(f"v.{k}_alt", "Voltages/Limits", f"{label} ALT limit", vs[k + "_alt"], "mV")
                    add(f"v.{k}_ov", "Voltages/Limits", f"{label} OV limit", vs[k + "_ov"], "mV")
            vo = _try(self.rm.vrel_offsets)
            if vo:
                add("v.nvvdd_vrel", "Voltages/Limits", "NVVDD Vrel offset", vo[0], "mV")
                add("v.msvdd_vrel", "Voltages/Limits", "MSVDD Vrel offset", vo[1], "mV")

        # power
        sf = config.shunt_factor(self.pci)
        pw = _try(N.nvmlDeviceGetPowerUsage, h)
        pw = pw / 1000.0 if pw is not None else None
        add("pwr.board", "Power", "Board power (reported)", pw, "W", 1)
        if sf != 1.0:
            add("pwr.board_real", "Power", f"Board power (real, x{sf:g} shunt)", pw * sf if pw is not None else None,
                "W", 1, f"Reported x {sf} (shunt-mod correction from /etc/meat3/config.json)")
        pl = _try(N.nvmlDeviceGetEnforcedPowerLimit, h)
        add("pwr.limit", "Power", "Power limit", pl / 1000.0 if pl else None, "W")
        e = _try(N.nvmlDeviceGetTotalEnergyConsumption, h)
        add("pwr.energy", "Power", "Energy since driver load", e / 3.6e9 if e else None, "kWh", 3)

        # ASUS 12V-2x6 per-pin
        if self.pins:
            r = _try(self.pins.read)
            grp = "12V-2x6 connector (ASUS)"
            if r:
                amps = [r["pins"][k][1] for k in range(1, 7)]
                tot_a = sum(amps)
                tot_w = sum(v * a for v, a in r["pins"].values())
                for k in range(1, 7):
                    v, a = r["pins"][k]
                    add(f"pin{k}.a", grp, f"Pin {k} current", a, "A", 2, "Rated ~9.5 A per pin")
                    add(f"pin{k}.v", grp, f"Pin {k} voltage", v, "V", 3)
                add("pins.a", grp, "Connector total current", tot_a, "A", 1)
                add("pins.w", grp, "Connector total power", tot_w, "W", 0, "12V-2x6 rated 600 W")
                add("pins.max_a", grp, "Highest pin current", max(amps), "A", 2)
                add("pins.imbalance", grp, "Pin imbalance (max share)", 100 * max(amps) / tot_a if tot_a > 1 else None,
                    "%", 1, "Ideal 16.7 %")
                for j, (v, a) in enumerate(r["extra"], 1):
                    add(f"aux{j}.v", grp, f"Aux 12 V input {j} voltage", v, "V", 3)
                    add(f"aux{j}.a", grp, f"Aux 12 V input {j} current", a, "A", 2)
                    add(f"aux{j}.w", grp, f"Aux 12 V input {j} power", v * a if v and a is not None else None, "W", 1)
            else:
                for k in range(1, 7):
                    add(f"pin{k}.a", grp, f"Pin {k} current", None, "A", 2)

        # temperatures
        add("t.gpu", "Temperatures", "GPU", _try(N.nvmlDeviceGetTemperature, h, N.NVML_TEMPERATURE_GPU), "°C")
        chans = (_try(self.rm.thermal_channels, default=[]) or []) if self.rm else []
        by_type = {}
        for c in chans:
            by_type.setdefault(c["type"], c)
        mem_c = by_type.get(3)
        add("t.mem", "Temperatures", "Memory (GDDR)", mem_c["temp"] if mem_c else None, "°C", 1,
            "RM thermal channel type 3")
        bw = _try(self.rm.blackwell_temps) if (self.rm and self.blackwell) else None
        hs = by_type.get(1)
        hs_ch = hs["temp"] if hs and hs["enabled"] else None
        add("t.hotspot", "Temperatures", "Hotspot", (bw or {}).get("hotspot", hs_ch) if bw else hs_ch, "°C", 1,
            "GB20x THERM 0xAD0AA0 (max of die sensors), read via RM GPU_EXEC_REG_OPS" if bw else
            "Thermal channel (disabled by NVIDIA on RTX 50)")
        if bw:
            gpu_t = _try(N.nvmlDeviceGetTemperature, h, N.NVML_TEMPERATURE_GPU)
            if bw.get("hotspot") is not None and gpu_t is not None:
                add("t.hotspot_delta", "Temperatures", "Hotspot − GPU delta", bw["hotspot"] - gpu_t, "°C", 1,
                    "Large/growing delta under load = poor cooler contact or paste")
            for k, lab in (("die_a0", "Die sensor A0"), ("die_a1", "Die sensor A1"), ("die_a2", "Die sensor A2"),
                           ("die_a3", "Die sensor A3"), ("die_a4", "Die sensor A4"), ("bankb_max", "Die bank B max")):
                add(f"t.{k}", "Temperatures/Die sensors", lab, bw.get(k), "°C", 1, "GB20x TSENSE (Q9.5)")
            add("t.slowdown_hw", "Temperatures", "HW slowdown limit", bw.get("slowdown"), "°C", 1, "THERM 0xAD0718")
        g0 = by_type.get(0)
        add("t.gpu_ch", "Temperatures", "GPU (thermal channel)", g0["temp"] if g0 else None, "°C", 1)
        for c in chans:
            if c["enabled"] and c["type"] not in (0, 1, 3) and c["temp"] is not None:
                add(f"t.ch{c['index']}", "Temperatures", f"Channel {c['index']} (type {c['type']})", c["temp"], "°C", 1)
        add("t.slowdown", "Temperatures", "Slowdown threshold",
            _try(N.nvmlDeviceGetTemperatureThreshold, h, N.NVML_TEMPERATURE_THRESHOLD_SLOWDOWN), "°C")

        # usage / memory / pcie
        u = _try(N.nvmlDeviceGetUtilizationRates, h)
        add("u.gpu", "Usage", "GPU core load", u.gpu if u else None, "%")
        add("u.memctl", "Usage", "Memory controller load", u.memory if u else None, "%")
        enc = _try(N.nvmlDeviceGetEncoderUtilization, h)
        dec = _try(N.nvmlDeviceGetDecoderUtilization, h)
        add("u.enc", "Usage", "Video encoder load", enc[0] if enc else None, "%")
        add("u.dec", "Usage", "Video decoder load", dec[0] if dec else None, "%")
        mi = _try(N.nvmlDeviceGetMemoryInfo, h)
        add("mem.used", "Memory", "VRAM used", mi.used / 2**20 if mi else None, "MiB")
        add("mem.total", "Memory", "VRAM total", mi.total / 2**20 if mi else None, "MiB")
        add("mem.pct", "Memory", "VRAM usage", 100 * mi.used / mi.total if mi else None, "%", 1)
        add("pcie.gen", "PCIe", "Link generation", _try(N.nvmlDeviceGetCurrPcieLinkGeneration, h))
        add("pcie.width", "PCIe", "Link width", _try(N.nvmlDeviceGetCurrPcieLinkWidth, h), "x")
        for sid, ctr, label in (("pcie.tx", N.NVML_PCIE_UTIL_TX_BYTES, "TX throughput"),
                                ("pcie.rx", N.NVML_PCIE_UTIL_RX_BYTES, "RX throughput")):
            v = _try(N.nvmlDeviceGetPcieThroughput, h, ctr)
            add(sid, "PCIe", label, v / 1024 if v is not None else None, "MiB/s", 1)

        # fans
        for f in range(self.nfans):
            add(f"fan{f}.pct", "Fans", f"Fan {f + 1} speed", _try(N.nvmlDeviceGetFanSpeed_v2, h, f), "%")
            rpm = _try(getattr(N, "nvmlDeviceGetFanSpeedRPM", lambda *a: None), h, f)
            if rpm is not None:
                add(f"fan{f}.rpm", "Fans", f"Fan {f + 1} speed", getattr(rpm, "speed", rpm), "RPM")

        # performance limiters
        pst = _try(N.nvmlDeviceGetPerformanceState, h)
        add("perf.pstate", "Performance limits", "P-state", pst, "", 0, "P0 = max performance")
        reasons = _try(N.nvmlDeviceGetCurrentClocksEventReasons, h) if hasattr(N, "nvmlDeviceGetCurrentClocksEventReasons") \
            else _try(N.nvmlDeviceGetCurrentClocksThrottleReasons, h)
        for bit, label in THROTTLE_BITS:
            add(f"perf.reason_{bit:x}", "Performance limits", f"Limiter: {label}",
                (1 if reasons & bit else 0) if reasons is not None else None, "", 0, "1 = active")

        # clock-propagation ratios (patched module v3)
        for r in self._ratios():
            add(f"ratio.{r['index']}", "Clocks/Ratios", f"{r['name']} ratio", r["current"], "×", 4,
                f"VBIOS default {r['default']:.4f}" + (" (bidirectional)" if r["bidir"] else ""))

        # offsets currently applied
        add("oc.core", "Overclock state", "Core offset", _try(N.nvmlDeviceGetGpcClkVfOffset, h), "MHz")
        add("oc.mem", "Overclock state", "Memory offset", _try(N.nvmlDeviceGetMemClkVfOffset, h), "MHz",
            0, "NVML units: counts double vs nvidia-smi MHz")
        if self.rm and self.patched:
            offs = _try(self.rm.domain_offsets)
            if offs:
                for dom, sid in ((0x2, "xbar"), (0x4, "sys")):
                    d = self.domains.get(dom)
                    if d:
                        add(f"oc.{sid}", "Overclock state", f"{nv_rm.DOMAIN_NAMES[dom]} offset", offs.get(d["index"]), "MHz")
        return S

    def _ratios(self) -> list[dict]:
        if not (self.rm and self.patched):
            return []
        rs = _try(self.rm.clock_ratios, default=[]) or []
        by_idx = {d["index"]: d["domain"] for d in self.domains.values()}
        for r in rs:
            src = nv_rm.DOMAIN_NAMES.get(by_idx.get(r["src"]), f"dom{r['src']}")
            dst = nv_rm.DOMAIN_NAMES.get(by_idx.get(r["dst"]), f"dom{r['dst']}")
            r["name"] = f"{src} → {dst}"
            r["dst_bit"] = by_idx.get(r["dst"])
            r["src_bit"] = by_idx.get(r["src"])
        # Several entries can link the same pair (GB203: five GPC->XBAR entries, defaults 0.9/0.8/2.0/
        # 1.2/1.5). Which one applies when is not reverse-engineered yet, so name them by entry.
        names = [r["name"] for r in rs]
        for r in rs:
            if names.count(r["name"]) > 1:
                r["name"] += f" #{r['index']} (default {r['default']:.2f})"
        return rs

    # ----------------------------------------------------------------- controls
    def controls(self) -> list[Control]:
        h, p, g = self.h, self.p, self.name
        C: list[Control] = []
        cons = _try(N.nvmlDeviceGetPowerManagementLimitConstraints, h)
        dflt = _try(N.nvmlDeviceGetPowerManagementDefaultLimit, h)
        C.append(Control(f"{p}.oc.power_limit", g, "Power limit", "int",
                         value=(_try(N.nvmlDeviceGetPowerManagementLimit, h) or 0) // 1000,
                         min=cons[0] // 1000 if cons else None, max=cons[1] // 1000 if cons else None, step=5,
                         unit="W", default=dflt // 1000 if dflt else None, danger=1,
                         hint="Software power cap (reported watts). Connector/pin limits still apply.",
                         available=cons is not None))
        rng = _try(N.nvmlDeviceGetGpcClkMinMaxVfOffset, h)
        C.append(Control(f"{p}.oc.core_offset", g, "Core clock offset", "int",
                         value=_try(N.nvmlDeviceGetGpcClkVfOffset, h), min=rng[0] if rng else -500,
                         max=rng[1] if rng else 500, step=5, unit="MHz", default=0, danger=1))
        rng = _try(N.nvmlDeviceGetMemClkMinMaxVfOffset, h)
        C.append(Control(f"{p}.oc.mem_offset", g, "Memory clock offset", "int",
                         value=_try(N.nvmlDeviceGetMemClkVfOffset, h), min=rng[0] if rng else -2000,
                         max=rng[1] if rng else 3000, step=50, unit="MHz", default=0, danger=1,
                         hint="NVML units (x2 vs nvidia-smi). Watch FPS: GDDR7 retries errors silently."))
        C.append(Control(f"{p}.oc.clock_lock", g, "Max core clock lock (0 = off)", "int", value=self.lock_max or 0,
                         min=0, max=4000, step=15, unit="MHz", default=0,
                         hint="nvidia-smi -lgc 210,<value>: keeps the card off unstable top bins"))
        patched_hint = "" if self.patched else "Needs the meat3/nvoc patched NVIDIA open module"
        offs = _try(self.rm.domain_offsets) if self.rm and self.patched else None
        for dom, sid, label in ((0x2, "xbar_offset", "XBAR clock offset"), (0x4, "sys_offset", "SYS clock offset")):
            d = self.domains.get(dom)
            C.append(Control(f"{p}.oc.{sid}", g, label, "int",
                             value=offs.get(d["index"]) if offs and d else None,
                             min=max(d["min"], -500) if d else -500, max=min(d["max"], 500) if d else 500, step=5,
                             unit="MHz", default=0, danger=2 if sid == "sys_offset" else 1,
                             hint=patched_hint or "Per-domain offset via RM 0x2080d01c (firmware does no range check)",
                             available=bool(self.patched and d)))
        vo = _try(self.rm.vrel_offsets) if self.rm and self.patched else None
        C.append(Control(f"{p}.oc.msvdd_vrel", g, "MSVDD Vrel offset (aux voltage)", "int",
                         value=vo[1] if vo else None, min=-50, max=50, step=5, unit="mV", default=-50, danger=2,
                         hint=patched_hint or "Raises MSVDD REL limit 1:1 (stock -50 mV). Helps XBAR.",
                         available=bool(self.patched and vo)))
        C.append(Control(f"{p}.oc.nvvdd_vrel", g, "NVVDD Vrel offset (core)", "int",
                         value=vo[0] if vo else None, min=-150, max=50, step=5, unit="mV", default=0, danger=2,
                         hint=patched_hint or "Negative = hard undervolt cap; positive is capped by ALT/OV",
                         available=bool(self.patched and vo)))
        C.append(self._vf_control())
        for r in self._ratios():
            if {r.get("src_bit"), r.get("dst_bit")} & {0x2, 0x4, 0x100000} and r["current"] is not None:
                C.append(Control(f"{p}.oc.ratio_{r['index']}", g, f"Clock ratio {r['name']}", "float",
                                 value=round(r["current"], 4), min=round(r["default"], 4),
                                 max=round(r["default"] * 1.10, 4), step=0.005, unit="×",
                                 default=round(r["default"], 4), danger=2,
                                 hint="MSVDD-domain clock ceiling relative to its source (Afterburner 'MSVDD clock "
                                      "ratio'). Firmware does no range check; meat3 caps at +10%."))
        return C

    def _vf_control(self) -> Control:
        """The GPC V/F curve as one 'curve' control: per-point frequency deltas (what Afterburner's curve
        editor and mVolt+ edit). Reading needs module v2, writing module v4."""
        p, g = self.p, self.name
        pts = _try(self.rm.vf_curve) if self.rm and self.patched else None
        gmax = _try(N.nvmlDeviceGetMaxClockInfo, self.h, N.NVML_CLOCK_GRAPHICS)
        d = self.domains.get(0x1)
        lo, hi = (max(d["min"], -1000), min(d["max"], 1000)) if d else (-1000, 1000)
        value = {"mode": "delta", "points": pts or [], "lock": {"min": lo, "max": hi}, "max_mhz": gmax}
        if not self.patched:
            hint = "Needs the meat3/nvoc patched NVIDIA open module (v2 to read, v4 to write)"
        elif not self.vf_set:
            hint = "Curve is read-only on this module: the setter (0x2080d02d) is routed from module v4"
        else:
            hint = ("Per-point clock on the GPC V/F curve (voltage per point is fixed by the firmware). Drag "
                    "points, 'Flatten above' a point so no higher clock exists above that voltage. The firmware "
                    "only uses points up to its own request ceiling; a uniform 'Core clock offset' rewrites "
                    "every point's delta.")
        return Control(f"{p}.oc.vf_curve", g, "V/F curve (GPC)", "curve", value=value, min=lo, max=hi, step=5,
                       unit="MHz", default={"deltas": {}}, danger=1, hint=hint,
                       available=bool(self.vf_set and pts))

    def _apply_vf(self, value) -> tuple[bool, str]:
        if not self.vf_set:
            return False, "V/F curve writes need module v4"
        if not isinstance(value, dict):
            return False, "value must be {'deltas': {point: MHz}}"
        raw = value.get("deltas")
        if raw == {} or raw is None and value.get("stock"):
            pts = _try(self.rm.vf_curve) or []
            raw = {p["i"]: 0 for p in pts}
        if not isinstance(raw, dict) or not raw:
            return False, "no deltas given"
        try:
            deltas = {int(k): float(v) for k, v in raw.items()}
        except (TypeError, ValueError):
            return False, "deltas must map point index to MHz"
        d = self.domains.get(0x1)
        lim = min(1000.0, float(d["max"]), -float(d["min"])) if d else 1000.0
        try:
            n = self.rm.set_vf_deltas(deltas, max_abs_mhz=lim)
        except Exception as e:
            return False, f"failed: {e}"
        return True, f"V/F curve: {n} point deltas written"

    def apply(self, control_id: str, value) -> tuple[bool, str]:
        if control_id.endswith(".oc.vf_curve"):
            return self._apply_vf(value)
        if ".oc.ratio_" in control_id:
            ctl = {c.id: c for c in self.controls()}.get(control_id)
            if ctl is None or not ctl.available:
                return False, "not available"
            try:
                v = float(value)
            except (TypeError, ValueError):
                return False, "value must be a number"
            if not ctl.min <= v <= ctl.max:
                return False, f"{v} outside {ctl.min}..{ctl.max}"
            try:
                self.rm.set_clock_ratio(int(control_id.rsplit("_", 1)[1]), v)
            except Exception as e:
                return False, f"failed: {e}"
            return True, f"{ctl.label} = {v:.4f}"
        ctl = {c.id: c for c in self.controls()}.get(control_id)
        if ctl is None:
            return False, "unknown control"
        if not ctl.available:
            return False, f"not available: {ctl.hint}"
        try:
            v = int(round(float(value)))
        except (TypeError, ValueError):
            return False, "value must be a number"
        if ctl.min is not None and v < ctl.min or ctl.max is not None and v > ctl.max:
            return False, f"{v} outside {ctl.min}..{ctl.max}"
        key = control_id.split(".oc.", 1)[1]
        try:
            if key == "power_limit":
                N.nvmlDeviceSetPowerManagementLimit(self.h, v * 1000)
            elif key == "core_offset":
                N.nvmlDeviceSetGpcClkVfOffset(self.h, v)
            elif key == "mem_offset":
                N.nvmlDeviceSetMemClkVfOffset(self.h, v)
            elif key == "clock_lock":
                if v == 0:
                    N.nvmlDeviceResetGpuLockedClocks(self.h)
                    self.lock_max = None
                else:
                    if v < 300:
                        return False, "lock below 300 MHz refused"
                    N.nvmlDeviceSetGpuLockedClocks(self.h, 210, v)
                    self.lock_max = v
            elif key in ("xbar_offset", "sys_offset"):
                d = self.domains[0x2 if key == "xbar_offset" else 0x4]
                self.rm.set_domain_offset(d["index"], v)
            elif key == "msvdd_vrel":
                self.rm.set_vrel(1, v)
            elif key == "nvvdd_vrel":
                self.rm.set_vrel(0, v)
            else:
                return False, "unknown control"
        except Exception as e:
            return False, f"failed: {e}"
        return True, f"{ctl.label} = {v} {ctl.unit}"

    def close(self):
        for o in (self.rm, self.pins):
            if o:
                _try(o.close)
