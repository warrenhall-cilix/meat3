"""meat³ mock daemon: implements the full PROTOCOL.md over a Unix socket with fake hardware.

    python3 -m meat3.core.mockd --socket /tmp/meat3-mock.sock [--osd-file PATH] [--rate 1.0]

Simulated devices (all implement meat3.core.model.Backend, so this also doubles as a
reference for the real daemon's plumbing):

* GPU 0: NVIDIA GeForce RTX 5080 (shunt-modded x1.5, 12V-2x6 per-pin telemetry)
* GPU 1: AMD Radeon RX 9070 XT (mostly idle second GPU)
* CPU:   AMD Ryzen 7 9800X3D
* Board: ASUS ROG STRIX X870E-E GAMING WIFI (nct6799 hwmon)

The load follows a slow "game session" pattern (idle -> load -> heavy) so min/max/avg,
throttle flags and colour thresholds all get exercised. Controls change the simulation
(core offset raises GPC clock, power limit caps board power, fan mode/speed, PBO ...).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import signal
import socket
import socketserver
import sys
import threading
import time
from typing import Any, Optional

from meat3.core.fmt import osd_line
from meat3.core.model import Backend, Control, Sensor

PROTOCOL_VERSION = "0.1"
DEFAULT_HOTKEY = "KEY_RIGHTCTRL+KEY_F12"


# ----------------------------------------------------------------------------- helpers

def validate(c: Control, value: Any) -> tuple[bool, str, Any]:
    """Generic validation used by every mock backend (the real ones must do the same)."""
    if not c.available:
        return False, f"{c.label}: not available ({c.hint})", None
    if c.kind == "action":
        return True, "", True
    if c.kind == "bool":
        if isinstance(value, bool):
            return True, "", value
        if value in (0, 1):
            return True, "", bool(value)
        return False, f"{c.label}: expected bool", None
    if c.kind == "choice":
        if value not in c.choices:
            return False, f"{c.label}: {value!r} is not one of {c.choices}", None
        return True, "", value
    if c.kind == "curve":
        if not isinstance(value, dict) or not isinstance(value.get("deltas", value.get("points")), (dict, list)):
            return False, f"{c.label}: expected {{'deltas': {{point: MHz}}}}", None
        if "deltas" in value:
            try:
                d = {int(k): float(v) for k, v in value["deltas"].items()}
            except (TypeError, ValueError):
                return False, f"{c.label}: deltas must map point index to MHz", None
            for k, v in d.items():
                if c.min is not None and v < c.min or c.max is not None and v > c.max:
                    return False, f"{c.label}: delta {v:g} at point {k} outside {c.min:g}..{c.max:g}", None
            return True, "", {"deltas": d}
        return True, "", value
    if c.kind in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, f"{c.label}: expected a number", None
        if not math.isfinite(float(value)):
            return False, f"{c.label}: not finite", None
        v = int(round(value)) if c.kind == "int" else float(value)
        if c.kind == "int" and abs(v - value) > 1e-9:
            return False, f"{c.label}: expected an integer", None
        if c.min is not None and v < c.min - 1e-9:
            return False, f"{c.label}: {v} below minimum {c.min}", None
        if c.max is not None and v > c.max + 1e-9:
            return False, f"{c.label}: {v} above maximum {c.max}", None
        if c.step and c.min is not None:
            k = (v - c.min) / c.step
            if abs(k - round(k)) > 1e-6:
                return False, f"{c.label}: {v} not a multiple of step {c.step}", None
        if c.kind == "float":
            v = round(v, 6)
        return True, "", v
    return False, f"{c.label}: unknown kind {c.kind}", None


class Smooth:
    """First-order lag so fake values drift rather than jump."""

    def __init__(self, value: float, alpha: float = 0.35):
        self.v = value
        self.alpha = alpha

    def __call__(self, target: float) -> float:
        self.v += (target - self.v) * self.alpha
        return self.v


class Sim:
    """Global load pattern shared by all devices (seconds since start -> 0..1)."""

    def __init__(self, start: float = 12.0):
        # start a little into the cycle so a fresh mock is already "in game"
        self.t0 = time.monotonic() - start

    def t(self) -> float:
        return time.monotonic() - self.t0

    def load(self) -> float:
        t = self.t()
        cyc = t % 90.0
        if cyc < 8:        # menu / alt-tab
            base = 0.12
        elif cyc < 60:     # gameplay
            base = 0.82 + 0.10 * math.sin(t / 3.1)
        else:              # heavy scene
            base = 0.97
        return max(0.03, min(1.0, base))


class MockBackend(Backend):
    """Shared plumbing: control table + state dict + generic validated apply()."""

    group = "?"

    def __init__(self, sim: Sim, seed: int):
        self.sim = sim
        self.rng = random.Random(seed)
        self.state: dict[str, Any] = {}
        self.lock = threading.Lock()

    @classmethod
    def detect(cls) -> list[Backend]:  # pragma: no cover - mocks are instantiated directly
        return []

    def n(self, scale: float) -> float:
        return self.rng.gauss(0.0, scale)

    def S(self, sub: str, sid: str, label: str, value: Optional[float], unit: str = "",
          decimals: int = 0, hint: str = "") -> Sensor:
        g = self.group + ("/" + sub if sub else "")
        return Sensor(sid, g, label, value, unit, decimals, hint)

    def control_defs(self) -> list[Control]:
        return []

    def controls(self) -> list[Control]:
        with self.lock:
            out = []
            for c in self.control_defs():
                if c.kind not in ("action", "curve"):      # a curve control carries its own live value
                    c.value = self.state.get(c.id, c.default)
                out.append(c)
            return out

    def apply(self, control_id: str, value: Any) -> tuple[bool, str]:
        ctrl = {c.id: c for c in self.controls()}.get(control_id)
        if ctrl is None:
            return False, f"unknown control {control_id}"
        ok, msg, v = validate(ctrl, value)
        if not ok:
            return False, msg
        with self.lock:
            if ctrl.kind == "action":
                return self.action(control_id)
            self.state[control_id] = v
        unit = f" {ctrl.unit}" if ctrl.unit else ""
        return True, f"{ctrl.label} set to {v}{unit}"

    def action(self, control_id: str) -> tuple[bool, str]:
        return False, "not supported"

    def reset_ids(self, prefix: str) -> None:
        for c in self.control_defs():
            if c.id.startswith(prefix) and c.kind != "action":
                self.state[c.id] = c.default


# ----------------------------------------------------------------------------- RTX 5080

class MockRTX5080(MockBackend):
    kind = "gpu"
    name = "NVIDIA GeForce RTX 5080"
    group = "GPU 0: NVIDIA GeForce RTX 5080"
    P = "nv0"

    def __init__(self, sim: Sim):
        super().__init__(sim, 5080)
        self.temp = Smooth(42.0, 0.06)
        self.hot = Smooth(50.0, 0.08)
        self.mem_t = Smooth(48.0, 0.05)
        self.pwr = Smooth(30.0, 0.5)
        self.fan = Smooth(0.0, 0.15)
        self.fb_used = Smooth(1800.0, 0.1)
        for c in self.control_defs():
            if c.kind != "action":
                self.state[c.id] = c.default

    def control_defs(self) -> list[Control]:
        P, G = self.P, self.group
        manual = self.state.get(f"{P}.oc.fan_mode") == "manual"
        return [
            Control(f"{P}.oc.core_offset", G, "GPC clock offset", "int", min=-500, max=1000, step=5,
                    unit="MHz", default=0, danger=1, hint="Shifts the whole V/F curve. Test for stability."),
            Control(f"{P}.oc.mem_offset", G, "Memory clock offset", "int", min=-1000, max=3000, step=50,
                    unit="MHz", default=0, danger=1, hint="GDDR7 offset (data rate +2x offset)."),
            Control(f"{P}.oc.power_limit", G, "Power limit (raw)", "int", min=270, max=450, step=5,
                    unit="W", default=360, danger=1,
                    hint="Driver-side limit in raw (shunt-modded) watts; real board power is x1.5."),
            Control(f"{P}.oc.temp_target", G, "Temperature target", "int", min=65, max=90, step=1,
                    unit="°C", default=83, hint="GPU core temperature target."),
            Control(f"{P}.oc.nvvdd_max", G, "NVVDD max voltage", "float", min=0.900, max=1.100,
                    step=0.005, unit="V", default=1.095, danger=2,
                    hint="Caps the V/F curve. Raising above stock can degrade silicon."),
            Control(f"{P}.oc.msvdd_offset", G, "MSVDD offset", "int", min=-100, max=100, step=5,
                    unit="mV", default=0, danger=2, available=False,
                    hint="MSVDD control is not exposed by the r615 driver."),
            Control(f"{P}.oc.fan_mode", G, "Fan control", "choice", choices=["auto", "manual"],
                    default="auto", hint="auto = VBIOS fan curve."),
            Control(f"{P}.oc.fan_speed", G, "Fan speed", "int", min=30, max=100, step=1, unit="%",
                    default=50, available=manual,
                    hint="Fixed fan duty." if manual else "Set Fan control to 'manual' first."),
            Control(f"{P}.oc.clock_lock", G, "Lock GPC clock at max boost", "bool", default=False,
                    danger=1, hint="Locks the GPC clock, disables idle downclocking."),
            Control(f"{P}.oc.vf_curve", G, "V/F curve (GPC)", "curve", value=self._vf_value(), min=-1000,
                    max=1000, step=5, unit="MHz", default={"deltas": {}}, danger=1,
                    hint="Per-point clock deltas on the GPC V/F curve (voltage per point is fixed)."),
            Control(f"{P}.oc.reset_vf", G, "Reset V/F curve to stock", "action", danger=1,
                    hint="Clears offsets, voltage cap and clock lock."),
        ]

    # a GB203-like curve: 127 points, 450..1240 mV, stock clocks rising to 3300 MHz
    VF_MV = [450 + 25 * i for i in range(12)] + [750 + 5 * i for i in range(115)]

    def _vf_base(self, i: int) -> float:
        mv = self.VF_MV[i]
        return 255.0 if mv < 750 else round(255 + (mv - 750) * 6.2 + (mv - 750) ** 2 * -0.0012, 0)

    def _vf_value(self) -> dict:
        deltas = getattr(self, "vf_deltas", {}) or {}
        off = float(self.state.get(f"{self.P}.oc.core_offset", 0) or 0)
        pts = []
        for i in range(len(self.VF_MV)):
            b = self._vf_base(i)
            d = float(deltas.get(i, off))
            pts.append({"i": i, "mv": float(self.VF_MV[i]), "base": b, "mhz": b + d, "delta": d})
        return {"mode": "delta", "points": pts, "lock": {"min": -1000, "max": 1000}, "max_mhz": pts[-1]["mhz"]}

    def apply(self, control_id: str, value: Any) -> tuple[bool, str]:
        if control_id == f"{self.P}.oc.vf_curve":
            ctrl = {c.id: c for c in self.controls()}[control_id]
            ok, msg, v = validate(ctrl, value)
            if not ok:
                return False, msg
            with self.lock:
                d = dict(getattr(self, "vf_deltas", {}) or {})
                d.update(v["deltas"])
                if not v["deltas"]:
                    d = {}
                self.vf_deltas = d
            return True, f"V/F curve: {len(v['deltas'])} point deltas written"
        ok, msg = super().apply(control_id, value)
        if ok and control_id == f"{self.P}.oc.core_offset":
            self.vf_deltas = {}                     # a uniform offset rewrites every point (as the real driver)
        return ok, msg

    def action(self, control_id: str) -> tuple[bool, str]:
        if control_id.endswith("reset_vf"):
            self.vf_deltas = {}
            for k in ("core_offset", "mem_offset", "nvvdd_max", "clock_lock"):
                cid = f"{self.P}.oc.{k}"
                c = next(c for c in self.control_defs() if c.id == cid)
                self.state[cid] = c.default
            return True, "V/F curve reset to stock"
        return False, "unknown action"

    def sensors(self) -> list[Sensor]:
        with self.lock:
            st = dict(self.state)
        P, S, n = self.P, self.S, self.n
        load = self.sim.load()
        if st[f"{P}.oc.clock_lock"]:
            load = max(load, 0.35)
        plim_raw = float(st[f"{P}.oc.power_limit"])
        # raw draw before limiting (raw W, i.e. real / 1.5)
        want_raw = 18 + load * 345 * (1 + st[f"{P}.oc.core_offset"] / 4000.0) + n(4)
        raw = self.pwr(min(want_raw, plim_raw + n(1.5)))
        power_capped = want_raw > plim_raw * 0.985
        real = raw * 1.5
        vmax = float(st[f"{P}.oc.nvvdd_max"])
        idle = load < 0.2 and not st[f"{P}.oc.clock_lock"]
        gpc = (210 if idle else 2640 + 260 * (1 - (raw / plim_raw) ** 4)) + st[f"{P}.oc.core_offset"]
        gpc += (vmax - 1.095) * 1500 + n(8)
        if power_capped:
            gpc -= 45
        temp_target = st[f"{P}.oc.temp_target"]
        t = self.temp(30 + real * 0.095 + n(0.3))
        thermal = t > temp_target
        if thermal:
            gpc -= (t - temp_target) * 15
        nvvdd = 0.680 if idle else min(vmax, 0.86 + (gpc - 2300) / 2600)
        mem_mhz = (405 if idle else 15001) + st[f"{P}.oc.mem_offset"] / 2
        hot = self.hot(t + 9 + real * 0.012 + n(0.4))
        memj = self.mem_t(t + 8 + st[f"{P}.oc.mem_offset"] / 400 + n(0.3))
        if st[f"{P}.oc.fan_mode"] == "manual":
            duty = float(st[f"{P}.oc.fan_speed"])
        else:
            duty = 0.0 if t < 50 else min(100.0, 30 + (t - 50) * 2.2)
        duty = self.fan(duty)
        rpm = duty * 32.0
        util = 2 + n(1) if idle else min(100.0, 70 + load * 30 + n(2))
        util = max(0.0, util)
        fb = self.fb_used(1400 if idle else 11200 + n(150))

        # 12V-2x6 connector: real current split over 6 pins with an imbalance on pin 3
        conn_w = real * 0.93
        v_rail = 12.12 - real / 900.0
        weights = [1.00, 0.97, 1.22, 0.95, 0.92, 0.86]
        tot_w = sum(weights)
        amps_total = conn_w / v_rail
        pins_i = [amps_total * w / tot_w + n(0.06) for w in weights]
        pins_v = [v_rail - i * 0.004 + n(0.003) for i in pins_i]

        throttle = {
            "power": 1 if power_capped and not idle else 0,
            "thermal": 1 if thermal else 0,
            "hw_slowdown": 0,
            "reliability": 1 if (not idle and nvvdd >= vmax - 0.0005) else 0,
            "idle": 1 if idle else 0,
        }
        mask = (throttle["power"] << 2) | (throttle["hw_slowdown"] << 3) \
            | (throttle["thermal"] << 5) | (throttle["reliability"] << 7)

        out = [
            S("Clocks", f"{P}.clk.gpc", "GPC (core) clock", gpc, "MHz"),
            S("Clocks", f"{P}.clk.xbar", "XBAR clock", 300 if idle else gpc * 1.07 + n(5), "MHz"),
            S("Clocks", f"{P}.clk.sys", "SYS clock", 270 if idle else gpc * 0.94 + n(5), "MHz"),
            S("Clocks", f"{P}.clk.mem", "Memory clock", mem_mhz, "MHz",
              hint="GDDR7; effective data rate = 2x (30 Gbps at stock)."),
            S("Clocks", f"{P}.clk.video", "Video clock", 450 if idle else 2385 + n(4), "MHz"),
            S("Voltages", f"{P}.volt.nvvdd", "NVVDD", nvvdd + n(0.002), "V", 3),
            S("Voltages", f"{P}.volt.msvdd", "MSVDD", (0.750 if idle else 0.950) + n(0.002), "V", 3),
            S("Voltages", f"{P}.volt.nvvdd_limit", "NVVDD limit", vmax, "V", 3,
              hint="Max VID of the active V/F curve."),
            S("Voltages", f"{P}.volt.msvdd_limit", "MSVDD limit", None, "V", 3,
              hint="Not exposed by this VBIOS/driver."),
            S("Power", f"{P}.pwr.raw", "Board power (raw)", raw, "W", 1,
              hint="As reported by the driver (shunt-modded card reads low)."),
            S("Power", f"{P}.pwr.real", "Board power (real, x1.5 shunt corrected)", real, "W", 1,
              hint="Raw board power x1.5 (shunt mod correction factor)."),
            S("Power", f"{P}.pwr.limit", "Power limit (raw)", plim_raw, "W", 0),
            S("Power", f"{P}.pwr.tgp", "TGP", raw / plim_raw * 100, "%", 1),
            S("12V-2x6 connector", f"{P}.conn.power", "Connector power", conn_w, "W", 1),
            S("12V-2x6 connector", f"{P}.conn.current", "Connector current", amps_total, "A", 2),
        ]
        for i in range(6):
            out.append(S("12V-2x6 connector", f"{P}.conn.pin{i + 1}.v", f"Pin {i + 1} voltage",
                         pins_v[i], "V", 3))
        for i in range(6):
            out.append(S("12V-2x6 connector", f"{P}.conn.pin{i + 1}.a", f"Pin {i + 1} current",
                         pins_i[i], "A", 2, hint="Per-pin shunt; amber > 8 A, red > 9 A (9.5 A pin rating)."))
        out += [
            S("Temperatures", f"{P}.temp.gpu", "GPU temperature", t, "°C", 1),
            S("Temperatures", f"{P}.temp.hotspot", "Hot spot", hot, "°C", 1),
            S("Temperatures", f"{P}.temp.memj", "Memory junction", memj, "°C", 1),
            S("Fans", f"{P}.fan.1.rpm", "Fan 1", rpm + n(6) if rpm else 0, "RPM"),
            S("Fans", f"{P}.fan.2.rpm", "Fan 2", rpm * 1.02 + n(6) if rpm else 0, "RPM"),
            S("Fans", f"{P}.fan.duty", "Fan duty", duty, "%"),
            S("Utilisation", f"{P}.util.gpu", "GPU load", util, "%"),
            S("Utilisation", f"{P}.util.mem", "Memory controller load", util * 0.55, "%"),
            S("Utilisation", f"{P}.util.video", "Video engine load", 0.0, "%"),
            S("Utilisation", f"{P}.util.fb", "Framebuffer used", fb, "MB"),
            S("Throttle reasons", f"{P}.throttle.mask", "Throttle reasons (bitmask)", mask, "",
              hint="NVML clocks_event_reasons bitmask (idle bit 0x1 masked out)."),
            S("Throttle reasons", f"{P}.throttle.power", "Power cap", throttle["power"]),
            S("Throttle reasons", f"{P}.throttle.thermal", "Thermal (SW slowdown)", throttle["thermal"]),
            S("Throttle reasons", f"{P}.throttle.hw", "HW slowdown", throttle["hw_slowdown"]),
            S("Throttle reasons", f"{P}.throttle.vrel", "Reliability voltage", throttle["reliability"]),
            S("Throttle reasons", f"{P}.throttle.idle", "Idle", throttle["idle"]),
        ]
        return out


# ----------------------------------------------------------------------------- RX 9070 XT

class MockRX9070XT(MockBackend):
    kind = "gpu"
    name = "AMD Radeon RX 9070 XT"
    group = "GPU 1: AMD Radeon RX 9070 XT"
    P = "amd1"

    def __init__(self, sim: Sim):
        super().__init__(sim, 9070)
        self.edge = Smooth(38.0, 0.05)
        self.ppt = Smooth(24.0, 0.4)
        for c in self.control_defs():
            if c.kind != "action":
                self.state[c.id] = c.default

    def control_defs(self) -> list[Control]:
        P, G = self.P, self.group
        manual = self.state.get(f"{P}.oc.perf_level") == "manual"
        return [
            Control(f"{P}.oc.power_cap", G, "Power cap", "int", min=220, max=334, step=1, unit="W",
                    default=304, danger=1, hint="power1_cap (+10% max)."),
            Control(f"{P}.oc.sclk_offset", G, "GFX clock offset", "int", min=-500, max=1000, step=10,
                    unit="MHz", default=0, danger=1, hint="pp_od_clk_voltage SCLK offset."),
            Control(f"{P}.oc.mclk", G, "Memory clock max", "int", min=2400, max=2800, step=10,
                    unit="MHz", default=2518, danger=1),
            Control(f"{P}.oc.vddgfx_offset", G, "VDDGFX offset", "int", min=-200, max=0, step=5,
                    unit="mV", default=0, danger=2, hint="Undervolt; too low => driver timeouts/hangs."),
            Control(f"{P}.oc.perf_level", G, "Performance level", "choice",
                    choices=["auto", "low", "high", "manual"], default="auto",
                    hint="power_dpm_force_performance_level"),
            Control(f"{P}.oc.fan_pct", G, "Fan target", "int", min=0, max=100, step=1, unit="%",
                    default=0, available=manual,
                    hint="Fixed fan speed." if manual else "Requires Performance level = manual."),
            Control(f"{P}.oc.zero_rpm", G, "Zero RPM mode", "bool", default=True),
            Control(f"{P}.oc.reset_od", G, "Reset OD table", "action", danger=1,
                    hint="Writes 'r' to pp_od_clk_voltage."),
        ]

    def action(self, control_id: str) -> tuple[bool, str]:
        if control_id.endswith("reset_od"):
            self.reset_ids(f"{self.P}.oc.")
            return True, "OD table reset"
        return False, "unknown action"

    def sensors(self) -> list[Sensor]:
        with self.lock:
            st = dict(self.state)
        P, S, n = self.P, self.S, self.n
        load = 0.04 + 0.03 * math.sin(self.sim.t() / 7)
        ppt = self.ppt(min(st[f"{P}.oc.power_cap"], 22 + load * 280 + n(1.2)))
        edge = self.edge(30 + ppt * 0.28 + n(0.2))
        sclk = 520 + load * 2400 + st[f"{P}.oc.sclk_offset"] * load + n(15)
        zero_rpm = st[f"{P}.oc.zero_rpm"] and edge < 55
        if st[f"{P}.oc.perf_level"] == "manual":
            fan_pct = float(st[f"{P}.oc.fan_pct"])
        else:
            fan_pct = 0.0 if zero_rpm else 25.0 + max(0.0, edge - 50) * 2
        vgfx = 0.700 + load * 0.4 + st[f"{P}.oc.vddgfx_offset"] / 1000 + n(0.003)
        junction = None if self.rng.random() < 0.15 else edge + 9 + n(0.5)
        return [
            S("Clocks", f"{P}.clk.gfx", "GFX (shader) clock", sclk, "MHz"),
            S("Clocks", f"{P}.clk.soc", "SoC clock", 960 + n(4), "MHz"),
            S("Clocks", f"{P}.clk.mem", "Memory clock", 96 if load < 0.1 else st[f"{P}.oc.mclk"], "MHz",
              hint="GDDR6 20 Gbps at 2518 MHz."),
            S("Clocks", f"{P}.clk.fclk", "Fabric clock", 800 + n(2), "MHz"),
            S("Voltages", f"{P}.volt.vddgfx", "VDDGFX", vgfx, "V", 3),
            S("Voltages", f"{P}.volt.vddsoc", "VDDSOC", 0.805 + n(0.002), "V", 3),
            S("Power", f"{P}.pwr.ppt", "Total board power (PPT)", ppt, "W", 1),
            S("Power", f"{P}.pwr.cap", "Power cap", st[f"{P}.oc.power_cap"], "W"),
            S("Temperatures", f"{P}.temp.edge", "Edge", edge, "°C", 1),
            S("Temperatures", f"{P}.temp.junction", "Junction (hot spot)", junction, "°C", 1,
              hint="Occasionally unavailable while the SMU is in a low power state."),
            S("Temperatures", f"{P}.temp.mem", "Memory", edge + 4 + n(0.2), "°C", 1),
            S("Fans", f"{P}.fan.rpm", "Fan", fan_pct * 33 + (n(5) if fan_pct else 0), "RPM"),
            S("Fans", f"{P}.fan.pct", "Fan duty", fan_pct, "%"),
            S("Utilisation", f"{P}.util.gfx", "GFX activity", max(0.0, load * 100 + n(1)), "%"),
            S("Utilisation", f"{P}.util.mem", "Memory activity", max(0.0, load * 40 + n(0.5)), "%"),
            S("Utilisation", f"{P}.util.vram", "VRAM used", 612 + n(3), "MB"),
            S("Throttle", f"{P}.throttle.status", "Throttle status", 0,
              hint="gpu_metrics throttle_status bitmask"),
            S("Throttle", f"{P}.throttle.ppt", "PPT throttle", 0),
            S("Throttle", f"{P}.throttle.thermal", "Thermal throttle", 0),
        ]


# ----------------------------------------------------------------------------- 9800X3D

class MockRyzen9800X3D(MockBackend):
    kind = "cpu"
    name = "AMD Ryzen 7 9800X3D"
    group = "CPU: AMD Ryzen 7 9800X3D"
    P = "cpu"
    CORES = 8

    def __init__(self, sim: Sim):
        super().__init__(sim, 9800)
        self.tctl = Smooth(45.0, 0.08)
        self.core_t = [Smooth(44.0, 0.1) for _ in range(self.CORES)]
        self.usage = [Smooth(5.0, 0.4) for _ in range(self.CORES)]
        for c in self.control_defs():
            if c.kind != "action":
                self.state[c.id] = c.default

    def control_defs(self) -> list[Control]:
        P, G = self.P, self.group
        pbo = bool(self.state.get(f"{P}.oc.pbo"))
        return [
            Control(f"{P}.oc.pbo", G, "Precision Boost Overdrive", "bool", default=False, danger=1,
                    hint="Raises PPT/TDC/EDC to board limits."),
            Control(f"{P}.oc.boost_override", G, "Boost clock override", "int", min=0, max=200,
                    step=25, unit="MHz", default=0, danger=1, available=pbo,
                    hint="Max boost +N MHz." if pbo else "Enable Precision Boost Overdrive first."),
            Control(f"{P}.oc.co_all", G, "Curve Optimizer (all cores)", "int", min=-50, max=30,
                    step=1, unit="counts", default=0, danger=1,
                    hint="Negative = undervolt. Too negative => WHEA errors/crashes at idle."),
            Control(f"{P}.oc.ppt", G, "PPT limit", "int", min=65, max=230, step=1, unit="W",
                    default=162, danger=1),
            Control(f"{P}.oc.epp", G, "Energy performance preference", "choice",
                    choices=["power", "balance_power", "balance_performance", "performance"],
                    default="balance_performance", hint="amd-pstate-epp"),
            Control(f"{P}.oc.vsoc", G, "VSOC", "float", min=1.0, max=1.3, step=0.005, unit="V",
                    default=1.2, danger=2, available=False,
                    hint="VSOC is BIOS-only on this board (SMU mailbox command locked)."),
            Control(f"{P}.oc.reset_co", G, "Reset Curve Optimizer", "action",
                    hint="Sets all per-core CO counts to 0."),
            Control(f"{P}.l3.reserve", f"{G}/L3 cache", "Reserve L3 for games", "bool", default=True,
                    hint="While a game runs, background apps may only fill a small slice of L3."),
            Control(f"{P}.l3.reserve_bg_mb", f"{G}/L3 cache", "L3 limit for background apps while gaming",
                    "int", min=6, max=48, step=6, unit="MB", default=12,
                    hint="Rounded to whole L3 ways (6 MB each of 16); the game gets the rest."),
        ]

    def action(self, control_id: str) -> tuple[bool, str]:
        if control_id.endswith("reset_co"):
            self.state[f"{self.P}.oc.co_all"] = 0
            return True, "Curve Optimizer reset"
        return False, "unknown action"

    def sensors(self) -> list[Sensor]:
        with self.lock:
            st = dict(self.state)
        P, S, n = self.P, self.S, self.n
        load = self.sim.load()
        boost = 5200 + (st[f"{P}.oc.boost_override"] if st[f"{P}.oc.pbo"] else 0)
        co = st[f"{P}.oc.co_all"]
        out: list[Sensor] = []
        clocks, usages, powers = [], [], []
        game_threads = [0.95, 0.85, 0.7, 0.6, 0.45, 0.35, 0.3, 0.25]
        for i in range(self.CORES):
            u = self.usage[i](max(0.5, min(100.0, (game_threads[i] * load * 100) + n(4))))
            eff = 600 + (boost - 600 - i * 15 + (-co) * 2) * min(1.0, u / 60) + n(20)
            eff = min(eff, boost)
            p = 0.4 + u / 100 * 9.5 * (1 + co / 150) + n(0.15)
            ct = self.core_t[i](40 + p * 4.1 + n(0.3))
            clocks.append(eff)
            usages.append(u)
            powers.append(p)
            g = f"Cores/Core {i}"
            out += [
                S(g, f"{P}.core{i}.clk_eff", f"Core {i} effective clock", eff, "MHz"),
                S(g, f"{P}.core{i}.usage", f"Core {i} usage", u, "%", 1),
                S(g, f"{P}.core{i}.power", f"Core {i} power", p, "W", 2),
                S(g, f"{P}.core{i}.temp", f"Core {i} temperature", ct, "°C", 1),
                S(g, f"{P}.pmt.core{i}.volt", f"Core {i} voltage", 1.0 + 0.3 * min(1.0, u / 60) + n(0.005), "V", 4),
            ]
            be = max(0.0, 18 + 30 * u / 100 - (12 if st.get(f"{P}.l3.reserve") else 0) + n(2))
            fe, smt = max(0.0, 9 + n(1.5)), max(0.0, 4 * u / 100 + n(0.5))
            out += [
                S("Pipeline stalls", f"{P}.core{i}.ipc", f"Core {i} IPC", max(0.1, 3.2 - be / 25 + n(0.1)), "", 2),
                S("Pipeline stalls", f"{P}.core{i}.stall_be", f"Core {i} back-end bound", be, "%", 1),
                S("Pipeline stalls", f"{P}.core{i}.stall_fe", f"Core {i} front-end bound", fe, "%", 1),
                S("Pipeline stalls", f"{P}.core{i}.stall_smt", f"Core {i} SMT contention", smt, "%", 1),
            ]
        cores_w = sum(powers)
        soc_w = 19 + n(0.3)
        pkg = min(float(st[f"{P}.oc.ppt"]) * (1.4 if st[f"{P}.oc.pbo"] else 1.0), cores_w + soc_w + 6)
        tctl = self.tctl(42 + pkg * 0.42 + n(0.2))
        dram_r = 4 + load * 38 + n(0.8)
        dram_w = 2 + load * 14 + n(0.5)
        summary = [
            S("Cores", f"{P}.clk_eff_avg", "Average effective clock", sum(clocks) / len(clocks), "MHz"),
            S("Cores", f"{P}.usage_total", "Total usage", sum(usages) / len(usages), "%", 1),
            S("Cores", f"{P}.cores_power", "Cores power", cores_w, "W", 1),
        ]
        out = summary + out
        res = bool(st.get(f"{P}.l3.reserve"))
        bg_mb = float(st.get(f"{P}.l3.reserve_bg_mb") or 12)
        g_occ = (70 if res else 58) + 6 * load + n(1.5)
        ff = (bg_mb * 0.45 if res else 9) + n(0.4)
        other = bg_mb * 0.5 if res else 12
        out += [
            S("L3 cache", f"{P}.l3.ccd0.occupancy", "L3 in use", g_occ + ff + other, "MB", 1),
            S("L3 cache", f"{P}.l3.ccd0.occupancy_pct", "L3 in use (% of 96 MiB)",
              100 * (g_occ + ff + other) / 100.663296, "%", 1),
            S("L3 by app", f"{P}.l3app.steam_1091500.occupancy", "Cyberpunk2077.exe L3 in use", g_occ, "MB", 1),
            S("L3 by app", f"{P}.l3app.steam_1091500.cpu", "Cyberpunk2077.exe CPU usage", 48 * load + 5, "%", 1),
            S("L3 by app", f"{P}.l3app.firefox.occupancy", "firefox L3 in use", ff, "MB", 1),
            S("L3 by app", f"{P}.l3app.firefox.cpu", "firefox CPU usage", 2.5, "%", 1),
            S("L3 by app", f"{P}.l3app.other.occupancy", "Everything else L3 in use", other, "MB", 1),
            S("L3 by app", f"{P}.l3app.game.occupancy", "Game L3 in use", g_occ, "MB", 1,
              "game: Cyberpunk2077.exe (Steam app 1091500)"),
            S("L3 by app", f"{P}.l3.reserve.active",
              "L3 reservation: " + ("active for Cyberpunk2077.exe" if res else "off"), 1.0 if res else 0.0, "", 0),
            S("L3 by app", f"{P}.l3.reserve.game_mb", "L3 reserved for game", 96 - bg_mb if res else None, "MiB", 0),
            S("L3 by app", f"{P}.l3.reserve.bg_mb", "L3 limit for everything else", bg_mb if res else None, "MiB", 0),
        ]
        out.append(S("Pipeline by app", f"{P}.pipe.app_cost", "Per-app counting cost (estimate)",
                     0.4 * load + abs(n(0.02)), "%", 2))
        g_be = 42 - (14 if res else 0) + n(2)
        for k, nm, ipc, be, fe in (("steam_1091500", "Cyberpunk2077.exe", 1.4 + (0.35 if res else 0), g_be, 21),
                                   ("firefox", "firefox", 1.1, 35, 38), ("game", "Game", 1.4 + (0.35 if res else 0),
                                                                          g_be, 21)):
            out += [S("Pipeline by app", f"{P}.l3app.{k}.ipc", f"{nm} IPC", ipc + n(0.05), "", 2),
                    S("Pipeline by app", f"{P}.l3app.{k}.stall_be", f"{nm} back-end bound", be + n(1), "%", 1),
                    S("Pipeline by app", f"{P}.l3app.{k}.stall_fe", f"{nm} front-end bound", fe + n(1), "%", 1)]
        out += [
            S("Temperatures", f"{P}.temp.tctl", "Tctl/Tdie", tctl, "°C", 1),
            S("Temperatures", f"{P}.temp.ccd1", "CCD1 (Tdie)", tctl - 3 + n(0.3), "°C", 1),
            S("Temperatures", f"{P}.temp.l3", "L3 cache", tctl - 6 + n(0.3), "°C", 1),
            S("Temperatures", f"{P}.temp.iod", "IOD hotspot", 48 + load * 6 + n(0.2), "°C", 1),
            S("Fabric & memory", f"{P}.clk.fclk", "FCLK", 2100.0, "MHz"),
            S("Fabric & memory", f"{P}.clk.uclk", "UCLK", 3000.0, "MHz"),
            S("Fabric & memory", f"{P}.clk.mclk", "MCLK", 3000.0, "MHz", hint="DDR5-6000"),
            S("Fabric & memory", f"{P}.l3.hit", "L3 hit rate", max(0.0, min(100.0, 91 - load * 6 + n(0.8))), "%", 1,
              hint="perf: l3_lookup_state / l3 misses (96 MB V-Cache)."),
            S("Fabric & memory", f"{P}.dram.read", "DRAM read bandwidth", dram_r, "GB/s", 1),
            S("Fabric & memory", f"{P}.dram.write", "DRAM write bandwidth", dram_w, "GB/s", 1),
            S("Fabric & memory", f"{P}.dram.total", "DRAM total bandwidth", dram_r + dram_w, "GB/s", 1),
            S("Power", f"{P}.pwr.package", "Package power (PPT)", pkg, "W", 1),
            S("Power", f"{P}.pwr.soc", "SoC power", soc_w, "W", 1),
            S("Power", f"{P}.pwr.ppt_limit", "PPT limit", float(st[f"{P}.oc.ppt"]), "W"),
            S("Power", f"{P}.pwr.tdc", "TDC", pkg * 0.62 + n(0.5), "A", 1),
            S("Power", f"{P}.pwr.edc", "EDC", pkg * 0.9 + n(1), "A", 1),
            S("Voltages", f"{P}.volt.vcore", "Vcore (SVI3)", 0.95 + load * 0.28 + co / 1000 * 3 + n(0.004), "V", 3),
            S("Voltages", f"{P}.volt.vid", "Core VID (effective)", 1.0 + load * 0.25 + n(0.004), "V", 3),
            S("Voltages", f"{P}.volt.vsoc", "VSOC (SVI3)", 1.200 + n(0.002), "V", 3),
            S("Voltages", f"{P}.volt.vddio", "VDDIO / VDDQ", 1.350, "V", 3),
            S("Voltages", f"{P}.volt.vddg", "VDDG IOD", None, "V", 3, hint="Not reported by this SMU PM table version."),
            S("Throttle", f"{P}.throttle.prochot", "PROCHOT", 0),
            S("Throttle", f"{P}.throttle.thermal", "Thermal throttle", 1 if tctl > 95 else 0),
            S("Throttle", f"{P}.throttle.ppt", "PPT throttle", 1 if pkg >= st[f"{P}.oc.ppt"] * 0.99 else 0),
        ]
        return out


# ----------------------------------------------------------------------------- board

class MockBoard(MockBackend):
    kind = "board"
    name = "ASUS ROG STRIX X870E-E GAMING WIFI"
    group = "Board: ASUS ROG STRIX X870E-E"
    P = "board"

    def __init__(self, sim: Sim):
        super().__init__(sim, 870)
        self.vrm = Smooth(40.0, 0.05)
        for c in self.control_defs():
            if c.kind != "action":
                self.state[c.id] = c.default

    def control_defs(self) -> list[Control]:
        P, G = self.P, self.group
        manual = self.state.get(f"{P}.fan.cpu_mode") == "manual"
        return [
            Control(f"{P}.fan.cpu_mode", G, "CPU fan mode", "choice", choices=["bios curve", "manual"],
                    default="bios curve", hint="nct6799 pwm1_enable"),
            Control(f"{P}.fan.cpu_duty", G, "CPU fan duty", "int", min=20, max=100, step=1, unit="%",
                    default=60, available=manual,
                    hint="pwm1" if manual else "Switch CPU fan mode to 'manual' first."),
            Control(f"{P}.fan.chassis_duty", G, "Chassis fans duty", "int", min=20, max=100, step=1,
                    unit="%", default=45, hint="pwm2..pwm4"),
        ]

    def sensors(self) -> list[Sensor]:
        with self.lock:
            st = dict(self.state)
        S, n = self.S, self.n
        load = self.sim.load()
        vrm = self.vrm(38 + load * 18 + n(0.2))
        cpu_rpm = (st["board.fan.cpu_duty"] if st["board.fan.cpu_mode"] == "manual" else 35 + load * 30) * 18
        ch = st["board.fan.chassis_duty"] * 16
        return [
            S("Temperatures", "board.temp.mb", "Motherboard", 34 + load * 3 + n(0.1), "°C", 1),
            S("Temperatures", "board.temp.vrm", "VRM", vrm, "°C", 1),
            S("Temperatures", "board.temp.chipset", "Chipset", 52 + n(0.1), "°C", 1),
            S("Temperatures", "board.temp.tsensor", "T_Sensor", None, "°C", 1, hint="No probe connected."),
            S("Voltages", "board.volt.12v", "+12V", 12.096 + n(0.01), "V", 3),
            S("Voltages", "board.volt.5v", "+5V", 5.040 + n(0.005), "V", 3),
            S("Voltages", "board.volt.3v3", "+3.3V", 3.344 + n(0.003), "V", 3),
            S("Voltages", "board.volt.vbat", "VBAT", 3.168, "V", 3),
            S("Voltages", "board.volt.dram", "DRAM VDD", 1.350 + n(0.002), "V", 3),
            S("Fans", "board.fan.cpu", "CPU fan", cpu_rpm + n(8), "RPM"),
            S("Fans", "board.fan.pump", "AIO pump", 2850 + n(10), "RPM"),
            S("Fans", "board.fan.ch1", "Chassis fan 1", ch + n(6), "RPM"),
            S("Fans", "board.fan.ch2", "Chassis fan 2", ch * 1.03 + n(6), "RPM"),
            S("Fans", "board.fan.ch3", "Chassis fan 3", 0.0, "RPM", hint="Header present, no fan."),
        ]


# ----------------------------------------------------------------------------- daemon core

class Stats:
    """min/max/avg per sensor id since start or last reset_stats."""

    def __init__(self):
        self.d: dict[str, list] = {}

    def add(self, sid: str, v: Optional[float]) -> None:
        if v is None or not math.isfinite(v):
            return
        e = self.d.get(sid)
        if e is None:
            self.d[sid] = [v, v, v, 1]
        else:
            if v < e[0]:
                e[0] = v
            if v > e[1]:
                e[1] = v
            e[2] += v
            e[3] += 1

    def get(self, sid: str) -> tuple:
        e = self.d.get(sid)
        if not e:
            return None, None, None
        return e[0], e[1], e[2] / e[3]

    def reset(self) -> None:
        self.d.clear()


class MockDaemon:
    def __init__(self, osd_file: str, rate: float = 1.0):
        self.sim = Sim()
        self.backends: list[MockBackend] = [
            MockRTX5080(self.sim), MockRX9070XT(self.sim), MockRyzen9800X3D(self.sim), MockBoard(self.sim),
        ]
        self.rate = rate
        self.osd_file = osd_file
        self.lock = threading.Lock()
        self.stats = Stats()
        self.sensors: list[dict] = []
        self.t = time.time()
        self.osd_enabled = True
        self.osd_items = ["nv0.clk.gpc", "nv0.temp.gpu", "nv0.pwr.real", "nv0.volt.nvvdd",
                          "cpu.clk_eff_avg", "cpu.temp.tctl", "cpu.pwr.package"]
        self.hotkey = DEFAULT_HOTKEY
        self.stop = threading.Event()
        self.poll()

    # -- polling
    def poll(self) -> None:
        sens: list[dict] = []
        for b in self.backends:
            try:
                for s in b.sensors():
                    if isinstance(s.value, float) and not math.isfinite(s.value):
                        s.value = None
                    if s.value is not None:
                        s.value = round(float(s.value), max(0, s.decimals) + 2)
                    sens.append(s.to_dict())
            except Exception as e:  # a broken backend must not kill the daemon
                print(f"mockd: backend {b.name} failed: {e}", file=sys.stderr)
        with self.lock:
            for s in sens:
                self.stats.add(s["id"], s["value"])
                mn, mx, av = self.stats.get(s["id"])
                s["min"], s["max"], s["avg"] = mn, mx, (round(av, 4) if av is not None else None)
            self.sensors = sens
            self.t = time.time()
        self.write_osd()

    def write_osd(self) -> None:
        if not self.osd_file:
            return
        with self.lock:
            by_id = {s["id"]: s for s in self.sensors}
            lines = [osd_line(by_id[i]) for i in self.osd_items if i in by_id] if self.osd_enabled else []
        tmp = f"{self.osd_file}.tmp{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + ("\n" if lines else ""))
            os.replace(tmp, self.osd_file)    # atomic: MangoHud never sees a partial file
        except OSError as e:
            print(f"mockd: cannot write {self.osd_file}: {e}", file=sys.stderr)

    def run_poller(self) -> None:
        nxt = time.monotonic()
        while not self.stop.is_set():
            nxt += self.rate
            self.stop.wait(max(0.0, nxt - time.monotonic()))
            if self.stop.is_set():
                break
            self.poll()

    # -- protocol
    def handle(self, req: Any) -> dict:
        if not isinstance(req, dict):
            return {"ok": False, "msg": "request must be a JSON object"}
        cmd = req.get("cmd")
        if cmd == "hello":
            return {"ok": True, "version": PROTOCOL_VERSION, "host": socket.gethostname() + " (mock)",
                    "backends": [{"kind": b.kind, "name": b.name} for b in self.backends]}
        if cmd == "snapshot":
            with self.lock:
                sens = [dict(s) for s in self.sensors]
                t = self.t
            ctrls = []
            for b in self.backends:
                ctrls += [c.to_dict() for c in b.controls()]
            return {"ok": True, "t": t, "sensors": sens, "controls": ctrls}
        if cmd == "apply":
            cid = req.get("id")
            for b in self.backends:
                if any(c.id == cid for c in b.controls()):
                    try:
                        ok, msg = b.apply(cid, req.get("value"))
                    except Exception as e:
                        ok, msg = False, f"backend error: {e}"
                    return {"ok": bool(ok), "msg": msg}
            return {"ok": False, "msg": f"unknown control {cid!r}"}
        if cmd == "reset_stats":
            with self.lock:
                self.stats.reset()
            return {"ok": True}
        if cmd == "osd_get":
            with self.lock:
                return {"ok": True, "enabled": self.osd_enabled, "items": list(self.osd_items),
                        "hotkey": self.hotkey, "file": self.osd_file}
        if cmd == "osd_set":
            with self.lock:
                if "enabled" in req:
                    self.osd_enabled = bool(req["enabled"])
                if "items" in req:
                    items = req["items"]
                    if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
                        return {"ok": False, "msg": "items must be a list of sensor ids"}
                    self.osd_items = list(dict.fromkeys(items))
                if "hotkey" in req:
                    from meat3.osd.mangohud import parse_hotkey
                    try:
                        self.hotkey = "+".join(parse_hotkey(req["hotkey"]))
                    except ValueError as e:
                        return {"ok": False, "msg": f"hotkey: {e}"}
            self.write_osd()
            return {"ok": True}
        if cmd == "osd_toggle":
            with self.lock:
                self.osd_enabled = not self.osd_enabled
                en = self.osd_enabled
            self.write_osd()
            return {"ok": True, "enabled": en}
        if cmd == "update_status":
            return {"ok": True, "installed": "mock", "behind": 2, "checking": False, "running": False,
                    "commits": ["abc1234 mock change one", "def5678 mock change two"], "log": [],
                    "error": "", "checked": time.time(), "can_update": True}
        if cmd == "update_check":
            return {"ok": True}
        if cmd == "update_start":
            return {"ok": False, "msg": "the mock daemon can't update itself"}
        return {"ok": False, "msg": f"unknown cmd {cmd!r}"}


def make_server(path: str, daemon: MockDaemon) -> socketserver.UnixStreamServer:
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            for raw in self.rfile:
                if len(raw) > 1 << 20:
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                    resp = daemon.handle(req)
                except json.JSONDecodeError as e:
                    resp = {"ok": False, "msg": f"bad json: {e}"}
                except Exception as e:  # never drop the connection on a handler bug
                    resp = {"ok": False, "msg": f"internal error: {e}"}
                try:
                    self.wfile.write((json.dumps(resp, separators=(",", ":")) + "\n").encode())
                    self.wfile.flush()
                except OSError:
                    break

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    if os.path.exists(path):
        os.unlink(path)
    srv = Server(path, Handler)
    os.chmod(path, 0o660)
    return srv


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="meat³ mock daemon (fake hardware)")
    ap.add_argument("--socket", default="/tmp/meat3-mock.sock")
    ap.add_argument("--osd-file", default=None, help="osd.txt path (default: <socket>.osd.txt)")
    ap.add_argument("--rate", type=float, default=1.0, help="poll interval in seconds")
    a = ap.parse_args(argv)
    osd_file = a.osd_file or (a.socket + ".osd.txt")
    d = MockDaemon(osd_file, a.rate)
    srv = make_server(a.socket, d)
    poller = threading.Thread(target=d.run_poller, daemon=True)
    poller.start()

    def _stop(*_):
        d.stop.set()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print(f"meat3 mockd listening on {a.socket} (osd file {osd_file})", flush=True)
    try:
        srv.serve_forever(poll_interval=0.2)
    finally:
        srv.server_close()
        for p in (a.socket,):
            try:
                os.unlink(p)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
