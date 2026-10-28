"""Full RDNA4 PMFW metrics (SmuMetrics_t, smu14_driver_if_v14_0.h) via the meat3_smu kernel module.

amdgpu's gpu_metrics exposes only part of the firmware's metrics table. kmod/meat3_smu copies the
whole SmuMetricsExternal_t whenever gpu_metrics is read and publishes it in debugfs:
    /sys/kernel/debug/meat3_smu/gpuN  =  "M3SM" u32 version, u32 size, u32 seq, u64 ktime_ns, table

Offsets below were generated with gcc offsetof() against the kernel's smu14_driver_if_v14_0.h
(SmuMetricsExternal_t = 412 bytes, SmuMetrics_t = 260). Fields already in gpu_metrics are skipped.
"""
from __future__ import annotations

import os
import struct
from typing import Optional

DEBUGFS = "/sys/kernel/debug/meat3_smu"
MAGIC = b"M3SM"
HDR = struct.Struct("<4sIIIQ")           # magic, version, size, seq, ktime_ns
TABLE_SIZE = 412

SVI_PLANES = ["VDD_GFX", "VDD_SOC", "VDDCI_MEM", "VDDIO_MEM"]
TEMPS = ["Edge", "Hotspot", "Hotspot GFX", "Hotspot SoC", "Memory", "VR GFX", "VR SoC", "VR Mem0",
         "VR Mem1", "Liquid 0", "Liquid 1", "PLX"]
THROTTLERS = ["Temp edge", "Temp hotspot", "Temp hotspot GFX", "Temp hotspot SoC", "Temp memory",
              "Temp VR GFX", "Temp VR SoC", "Temp VR Mem0", "Temp VR Mem1", "Temp liquid 0",
              "Temp liquid 1", "Temp PLX", "TDC GFX", "TDC SoC", "PPT0", "PPT1", "PPT2", "PPT3", "FIT",
              "GFX APCC+", "GFX DVO"]

OFF = {
    "AverageGfxclkFrequencyTarget": 44, "AverageGfxclkFrequencyPreDs": 46,
    "AverageGfxclkFrequencyPostDs": 48, "AverageFclkFrequencyPreDs": 50,
    "AverageFclkFrequencyPostDs": 52, "AverageMemclkFrequencyPreDs": 54,
    "AverageMemclkFrequencyPostDs": 56, "AveragePCIeBusy": 66, "dGPU_W_MAX": 68,
    "MovingAverageSocketPower": 100, "MetricsCounter": 104, "AvgVoltage": 108, "AvgCurrent": 116,
    "AverageSocketPower": 136, "AverageTotalBoardPower": 138, "AvgTemperature": 140,
    "AvgTemperatureFanIntake": 164, "AvgFanPwm": 168, "ThrottlingPercentage": 172,
    "VmaxThrottlingPercentage": 193, "PublicSerialNumberLower": 252, "PublicSerialNumberUpper": 256,
}


WRAP_MA = 65536          # AvgCurrent is u16 mA, so it wraps every 65.536 A


def unwrap_gfx_ma(cur_ma: dict, mv: dict, tbp_w: float) -> tuple[Optional[float], int]:
    """(true VDD_GFX current in mA, wraps) or (None, -1) if board power can't pin the wrap count.

    VDD_GFX passes 65.5 A in any real load, and the u16 mA field wraps. The four rails can't draw
    more than the whole board (TotalBoardPower), and the remainder is board and regulator losses.
    So take the most wraps that still fit inside board power, and accept it only if the leftover
    is a believable loss. One wrap is >= 39 W even at 0.6 V, so at most one count fits the window.
    Checked on a real ASUS 9070 XT: idle 3150 mA, 0 wraps; 345 W load 30300 mA, 3 wraps = 227 A."""
    v = {k: x / 1000 for k, x in mv.items()}
    if not tbp_w or v.get("VDD_GFX", 0) < 0.3:
        return None, -1
    others = sum(v[k] * cur_ma[k] / 1000 for k in SVI_PLANES if k != "VDD_GFX")
    best = None
    for k in range(16):
        ma = cur_ma["VDD_GFX"] + k * WRAP_MA
        rails = others + v["VDD_GFX"] * ma / 1000
        if rails > tbp_w * 1.03:
            break
        best = (ma, k, tbp_w - rails)
    if best is None or best[2] > max(25.0, 0.25 * tbp_w):
        return None, -1
    return float(best[0]), best[1]

def read_raw(slot: int = 0, root: str = DEBUGFS) -> Optional[tuple[dict, bytes]]:
    """(header dict, table bytes) or None if the module isn't loaded / slot never filled."""
    try:
        with open(os.path.join(root, f"gpu{slot}"), "rb") as f:
            b = f.read()
    except OSError:
        return None
    if len(b) < HDR.size:
        return None
    magic, ver, size, seq, kt = HDR.unpack_from(b, 0)
    if magic != MAGIC or ver != 1 or size != TABLE_SIZE or seq == 0 or len(b) < HDR.size + size:
        return None
    return {"seq": seq, "ktime_ns": kt}, b[HDR.size:HDR.size + size]


def parse(t: bytes) -> dict:
    u16 = lambda o: struct.unpack_from("<H", t, o)[0]        # noqa: E731
    out = {k: u16(OFF[k]) for k in ("AverageGfxclkFrequencyTarget", "AverageGfxclkFrequencyPreDs",
                                     "AverageGfxclkFrequencyPostDs", "AverageFclkFrequencyPreDs",
                                     "AverageFclkFrequencyPostDs", "AverageMemclkFrequencyPreDs",
                                     "AverageMemclkFrequencyPostDs", "AveragePCIeBusy", "dGPU_W_MAX",
                                     "MovingAverageSocketPower", "AverageSocketPower",
                                     "AverageTotalBoardPower", "AvgTemperatureFanIntake")}
    out["MetricsCounter"] = struct.unpack_from("<I", t, OFF["MetricsCounter"])[0]
    out["AvgVoltage"] = dict(zip(SVI_PLANES, struct.unpack_from("<4H", t, OFF["AvgVoltage"])))
    out["AvgCurrent"] = dict(zip(SVI_PLANES, struct.unpack_from("<4H", t, OFF["AvgCurrent"])))
    out["AvgTemperature"] = dict(zip(TEMPS, struct.unpack_from("<12H", t, OFF["AvgTemperature"])))
    out["AvgFanPwm"] = t[OFF["AvgFanPwm"]]
    out["ThrottlingPercentage"] = dict(zip(THROTTLERS, t[OFF["ThrottlingPercentage"]:OFF["ThrottlingPercentage"] + 21]))
    out["VmaxThrottlingPercentage"] = t[OFF["VmaxThrottlingPercentage"]]
    lo, hi = struct.unpack_from("<II", t, OFF["PublicSerialNumberLower"])
    out["serial"] = f"{hi:08X}{lo:08X}"
    return out


def build(values: Optional[dict] = None) -> bytes:
    """Synthesize a table (tests / mock)."""
    v = values or {}
    t = bytearray(TABLE_SIZE)
    for k in ("AverageGfxclkFrequencyTarget", "AverageGfxclkFrequencyPostDs", "AverageMemclkFrequencyPostDs",
              "AverageFclkFrequencyPostDs", "AverageSocketPower", "AverageTotalBoardPower",
              "AvgTemperatureFanIntake", "dGPU_W_MAX"):
        if k in v:
            struct.pack_into("<H", t, OFF[k], v[k])
    struct.pack_into("<I", t, OFF["MetricsCounter"], v.get("MetricsCounter", 1))
    struct.pack_into("<4H", t, OFF["AvgVoltage"], *v.get("AvgVoltage", (0, 0, 0, 0)))
    struct.pack_into("<4H", t, OFF["AvgCurrent"], *v.get("AvgCurrent", (0, 0, 0, 0)))
    struct.pack_into("<12H", t, OFF["AvgTemperature"], *v.get("AvgTemperature", (0,) * 12))
    thr = v.get("ThrottlingPercentage", (0,) * 21)
    t[OFF["ThrottlingPercentage"]:OFF["ThrottlingPercentage"] + 21] = bytes(thr)
    t[OFF["VmaxThrottlingPercentage"]] = v.get("VmaxThrottlingPercentage", 0)
    struct.pack_into("<II", t, OFF["PublicSerialNumberLower"], *v.get("serial", (0, 0)))
    return bytes(t)


def file_blob(table: bytes, seq: int = 1) -> bytes:
    return HDR.pack(MAGIC, 1, TABLE_SIZE, seq, 0) + table
