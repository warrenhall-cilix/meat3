"""Read-only parser for the SMU 14.0.2 / 14.0.3 (RDNA4, RX 9070 / 9070 XT) powerplay table.

On SMU14 dGPUs `/sys/class/drm/cardN/device/pp_table` returns the combo pptable fetched from the
PMFW (struct smu_14_0_2_powerplay_table, 5812 bytes; smu_v14_0_2_get_pptable_from_pmfw). Writing
it is effectively a no-op on this generation (setup re-fetches from PMFW), so meat3 only reads it.

It contains the board's real overdrive limits, which pp_od_clk_voltage's OD_RANGE only partly shows:
OD_RANGE "MCLK" is the union [BasicMin.UclkFmin, BasicMax.UclkFmax], but the driver validates
UclkFmin against [BasicMin.UclkFmin, BasicMax.UclkFmin] and UclkFmax against
[BasicMin.UclkFmax, BasicMax.UclkFmax] (smu_v14_0_2_get_od_setting_limits).

Offsets were computed with gcc (offsetof) from the kernel's own headers:
drivers/gpu/drm/amd/pm/swsmu/inc/smu_v14_0_2_pptable.h and pmfw_if/smu14_driver_if_v14_0.h.
"""
from __future__ import annotations

import struct
from typing import Optional

PPTABLE_SIZE = 5812           # sizeof(struct smu_14_0_2_powerplay_table)
FORMAT_REVISION = 23          # SMU_14_0_2_TABLE_FORMAT_REVISION (informational; not enforced)

# absolute offsets within struct smu_14_0_2_powerplay_table
OFF_SW_SHUTDOWN_TEMP = 43
OFF_SKU = 1372
OFF_HW_CTF = 1400              # SkuTable.HwCtfTempLimit (u16 °C)
OFF_DEFAULT_VMAX = 1436        # SkuTable.DefaultMaxVoltage[GFX, SOC] (u16 mV Q2)
OFF_BOOST_VMAX = 1440          # SkuTable.BoostMaxVoltage[GFX, SOC] (u16 mV Q2)
OFF_DRV_CLOCKS = 3948          # SkuTable.DriverReportedClocks
OFF_MSG_LIMITS = 3976          # SkuTable.MsgLimits
OFF_OD_MIN = 4092              # SkuTable.OverDriveLimitsBasicMin
OFF_OD_MAX = 4188              # SkuTable.OverDriveLimitsBasicMax
OFF_CUSTOM = 4924              # CustomSkuTable
OFF_SOCKET_PL_AC = 4924        # CustomSkuTable.SocketPowerLimitAc[PPT0..3] (u16 W)
OFF_VR_TDC = 4932              # CustomSkuTable.VrTdcLimit[GFX, SOC] (u16 A)
OFF_TEMP_LIMIT = 4944          # CustomSkuTable.TemperatureLimit[TEMP_COUNT] (u16 °C)
OFF_FAN_MAX_RPM = 5046         # CustomSkuTable.FanMaximumRpm (u16)
OFF_FW_CTF = 5092              # CustomSkuTable.FwCtfLimit[TEMP_COUNT] (u16 °C)

# TEMP_e order (smu14_driver_if_v14_0.h)
TEMP_NAMES = ["Edge", "Hotspot", "Hotspot GFX", "Hotspot SoC", "Memory", "VR GFX", "VR SoC",
              "VR Mem0", "VR Mem1", "Liquid 0", "Liquid 1", "PLX"]

# OverDriveLimits_t field offsets (96 bytes)
OD_FIELDS = {
    "FeatureCtrlMask": (0, "<I"),
    "VoltageOffsetPerZoneBoundary0": (4, "<h"),
    "VddGfxVmax": (16, "<H"),
    "VddSocVmax": (18, "<H"),
    "GfxclkFoffset": (20, "<h"),
    "UclkFmin": (24, "<H"),
    "UclkFmax": (26, "<H"),
    "FclkFmin": (28, "<H"),
    "FclkFmax": (30, "<H"),
    "Ppt": (32, "<h"),
    "Tdc": (34, "<h"),
    "MaxOpTemp": (57, "<B"),
    "GfxEdc": (70, "<h"),
    "GfxPccLimitControl": (72, "<h"),
}

# PP_OD_FEATURE_*_BIT (bit positions in FeatureCtrlMask)
OD_FEATURE_BITS = {
    0: "GFX V/F curve (voltage offset)", 1: "GFX Vmax", 2: "SoC Vmax", 3: "Power limit (PPT %)",
    4: "Fan curve", 5: "Fan legacy (acoustic)", 6: "Full control", 7: "TDC",
    8: "GFX clock offset", 9: "Memory clock", 10: "Fabric clock", 11: "Zero-RPM fan",
    12: "Max operating temperature", 13: "EDC",
}

# THROTTLER_*_BIT for SMU 14.0.2/14.0.3 (gpu_metrics throttle_status raw bits)
SMU14_THROTTLER_BITS = {
    0: "TEMP_EDGE", 1: "TEMP_HOTSPOT", 2: "TEMP_HOTSPOT_GFX", 3: "TEMP_HOTSPOT_SOC",
    4: "TEMP_MEM", 5: "TEMP_VR_GFX", 6: "TEMP_VR_SOC", 7: "TEMP_VR_MEM0", 8: "TEMP_VR_MEM1",
    9: "TEMP_LIQUID0", 10: "TEMP_LIQUID1", 11: "TEMP_PLX", 12: "TDC_GFX", 13: "TDC_SOC",
    14: "PPT0", 15: "PPT1", 16: "PPT2", 17: "PPT3", 18: "FIT", 19: "GFX_APCC_PLUS", 20: "GFX_DVO",
}
SMU14_THROTTLER_CLASSES = [
    ("power", "Power limit (PPT)", (14, 15, 16, 17)),
    ("current", "Current limit (TDC)", (12, 13)),
    ("thermal", "Thermal limit", tuple(range(0, 12))),
    ("other", "Other (FIT/APCC+/DVO)", (18, 19, 20)),
]


def decode_smu14_throttle(v: Optional[int]) -> Optional[list[str]]:
    if v is None:
        return None
    return [n for b, n in SMU14_THROTTLER_BITS.items() if v & (1 << b)] + \
           [f"BIT{b}" for b in range(32) if v & (1 << b) and b not in SMU14_THROTTLER_BITS]


def _u16s(b: bytes, off: int, n: int) -> list[int]:
    return list(struct.unpack_from(f"<{n}H", b, off))


def _od(b: bytes, base: int) -> dict:
    return {k: struct.unpack_from(fmt, b, base + o)[0] for k, (o, fmt) in OD_FIELDS.items()}


def parse(blob: Optional[bytes]) -> Optional[dict]:
    """Parse a SMU 14.0.2/14.0.3 pp_table blob; None if it is not one (size mismatch)."""
    if not blob or len(blob) != PPTABLE_SIZE:
        return None
    size, fmt_rev, _content = struct.unpack_from("<HBB", blob, 0)
    temps = _u16s(blob, OFF_TEMP_LIMIT, len(TEMP_NAMES))
    ctf = _u16s(blob, OFF_FW_CTF, len(TEMP_NAMES))
    base_ac, game_ac, boost_ac = _u16s(blob, OFF_DRV_CLOCKS, 3)
    msg_ppt0_ac = struct.unpack_from("<H", blob, OFF_MSG_LIMITS)[0]          # MsgLimits.Power[PPT0][AC]
    msg_tdc = _u16s(blob, OFF_MSG_LIMITS + 16, 2)
    dvmax = [v / 4.0 for v in _u16s(blob, OFF_DEFAULT_VMAX, 2)]
    bvmax = [v / 4.0 for v in _u16s(blob, OFF_BOOST_VMAX, 2)]
    od_min, od_max = _od(blob, OFF_OD_MIN), _od(blob, OFF_OD_MAX)
    return {
        "structuresize": size,
        "format_revision": fmt_rev,
        "sw_shutdown_temp": blob[OFF_SW_SHUTDOWN_TEMP] | (blob[OFF_SW_SHUTDOWN_TEMP + 1] << 8),
        "hw_ctf_temp": struct.unpack_from("<H", blob, OFF_HW_CTF)[0],
        "vmax_default_mv": {"gfx": dvmax[0], "soc": dvmax[1]},
        "vmax_boost_mv": {"gfx": bvmax[0], "soc": bvmax[1]},
        "clocks": {"base": base_ac, "game": game_ac, "boost": boost_ac},
        "msg_ppt0_ac_w": msg_ppt0_ac,
        "msg_tdc_a": {"gfx": msg_tdc[0], "soc": msg_tdc[1]},
        "socket_pl_ac_w": _u16s(blob, OFF_SOCKET_PL_AC, 4),
        "vr_tdc_a": {"gfx": _u16s(blob, OFF_VR_TDC, 2)[0], "soc": _u16s(blob, OFF_VR_TDC, 2)[1]},
        "temp_limits": dict(zip(TEMP_NAMES, temps)),
        "fw_ctf_limits": dict(zip(TEMP_NAMES, ctf)),
        "fan_max_rpm": struct.unpack_from("<H", blob, OFF_FAN_MAX_RPM)[0],
        "od_min": od_min,
        "od_max": od_max,
        # the driver treats OD as unavailable unless both masks are non-zero (check_powerplay_table)
        "od_features": [n for bit, n in OD_FEATURE_BITS.items()
                        if od_min["FeatureCtrlMask"] and od_max["FeatureCtrlMask"] & (1 << bit)],
    }


def od_param_ranges(pt: dict) -> dict[str, tuple[int, int]]:
    """Real per-parameter ranges, keyed like amdgpu_od OdParam.key, as the driver validates them."""
    mn, mx = pt["od_min"], pt["od_max"]
    r = {
        "sclk_offset": (mn["GfxclkFoffset"], mx["GfxclkFoffset"]),
        "mclk_min": (mn["UclkFmin"], mx["UclkFmin"]),
        "mclk_max": (mn["UclkFmax"], mx["UclkFmax"]),
        "vddgfx_offset": (mn["VoltageOffsetPerZoneBoundary0"], mx["VoltageOffsetPerZoneBoundary0"]),
    }
    return {k: v for k, v in r.items() if v[0] <= v[1] and not (v[0] == 0 and v[1] == 0)}


def power_cap_note(pt: dict) -> str:
    """How the driver programs power1_cap above the PMFW message limit (smu_v14_0_2_set_power_limit)."""
    msg = pt["msg_ppt0_ac_w"]
    ppt_max = pt["od_max"]["Ppt"]
    return (f"Firmware message limit {msg} W; above it the driver writes stock limit + OD Ppt % "
            f"(board allows up to +{ppt_max}%), so the result is rounded down to ~1% steps.")


def build_blob(values: Optional[dict] = None) -> bytes:
    """Synthesize a pp_table blob (tests/fixtures). `values` overrides fields by the same names
    parse() returns: clocks, msg_ppt0_ac_w, temp_limits (list), od_min/od_max (dicts)."""
    v = values or {}
    b = bytearray(PPTABLE_SIZE)
    struct.pack_into("<HBB", b, 0, PPTABLE_SIZE, FORMAT_REVISION, 0)
    struct.pack_into("<H", b, OFF_SW_SHUTDOWN_TEMP, v.get("sw_shutdown_temp", 118))
    struct.pack_into("<H", b, OFF_HW_CTF, v.get("hw_ctf_temp", 120))
    struct.pack_into("<2H", b, OFF_DEFAULT_VMAX, *[int(x * 4) for x in v.get("vmax_default_mv", (1150, 1000))])
    struct.pack_into("<2H", b, OFF_BOOST_VMAX, *[int(x * 4) for x in v.get("vmax_boost_mv", (1200, 1050))])
    struct.pack_into("<3H", b, OFF_DRV_CLOCKS, *v.get("clocks", (2400, 2400, 2970)))
    struct.pack_into("<H", b, OFF_MSG_LIMITS, v.get("msg_ppt0_ac_w", 304))
    struct.pack_into("<2H", b, OFF_MSG_LIMITS + 16, *v.get("msg_tdc_a", (300, 60)))
    struct.pack_into("<4H", b, OFF_SOCKET_PL_AC, *v.get("socket_pl_ac_w", (304, 0, 0, 0)))
    struct.pack_into("<2H", b, OFF_VR_TDC, *v.get("vr_tdc_a", (300, 60)))
    struct.pack_into(f"<{len(TEMP_NAMES)}H", b, OFF_TEMP_LIMIT,
                     *v.get("temp_limits", (100, 110, 110, 110, 108, 115, 115, 115, 115, 0, 0, 0)))
    struct.pack_into(f"<{len(TEMP_NAMES)}H", b, OFF_FW_CTF,
                     *v.get("fw_ctf_limits", (105, 115, 115, 115, 110, 125, 125, 125, 125, 0, 0, 0)))
    struct.pack_into("<H", b, OFF_FAN_MAX_RPM, v.get("fan_max_rpm", 3300))
    od_min = {"FeatureCtrlMask": 0x3fff, "VoltageOffsetPerZoneBoundary0": -200, "GfxclkFoffset": -500,
              "UclkFmin": 97, "UclkFmax": 1258, "Ppt": -30, "Tdc": 0}
    od_max = {"FeatureCtrlMask": 0x3fff, "VoltageOffsetPerZoneBoundary0": 0, "GfxclkFoffset": 1000,
              "UclkFmin": 1258, "UclkFmax": 1500, "Ppt": 10, "Tdc": 0}
    od_min.update(v.get("od_min", {}))
    od_max.update(v.get("od_max", {}))
    for base, od in ((OFF_OD_MIN, od_min), (OFF_OD_MAX, od_max)):
        for k, val in od.items():
            o, fmt = OD_FIELDS[k]
            struct.pack_into(fmt, b, base + o, val)
    return bytes(b)
