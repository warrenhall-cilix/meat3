"""Table-driven decoder for the amdgpu `gpu_metrics` sysfs blob.

Every version defined in kgd_pp_interface.h is described in the generated
`amdgpu_metrics_defs.STRUCTS` table (offsets verified against gcc). The blob starts
with `struct metrics_table_header {u16 structure_size; u8 format_revision;
u8 content_revision}`; we pick the matching layout and decode all fields.
Fields the SMU did not fill are left at their memset(0xFF) value by
smu_cmn_init_soft_gpu_metrics(); those (all-ones for the field width) decode as None.

Format families and their units (from the kernel's per-ASIC fill functions):
  v1_0..v1_3  dGPU (Navi1x..Navi4x, Vega20, Arcturus, Aldebaran):
              temps degC, activity %, power W, voltages mV, clocks MHz, fan RPM,
              pcie_link_speed in 0.1 GT/s.
  v1_4..v1_8  MI300-class: same base units.
  v1_9        attribute-encoded (self-describing; unit carried per attribute).
  v2_0..v2_4  APUs: temps centi-degC, activity centi-%, power mW, voltage mV,
              current mA.
  v3_0        APUs (SMU 14.0.0/15.0.0): temps centi-degC, power mW, activity %
              (centi-% on SMU 14.0.0 PMFW <= 0x5d4600, see smu_v14_0_0_ppt.c).
"""
from __future__ import annotations

import struct
from typing import Any, Optional

from . import amdgpu_metrics_defs as defs

_FMT_SIZE = {"B": 1, "b": 1, "H": 2, "h": 2, "I": 4, "i": 4, "Q": 8, "q": 8}
_ALL_ONES = {"B": 0xFF, "H": 0xFFFF, "I": 0xFFFFFFFF, "Q": 0xFFFFFFFFFFFFFFFF,
             "b": -1, "h": -1, "i": -1, "q": -1}
# amdgpu_metrics_attr_type -> struct format char
_ATTR_TYPES = ["B", "b", "H", "h", "I", "i", "Q", "q"]


def _unavailable(fmt: str, v: int) -> bool:
    return v == _ALL_ONES[fmt]


def parse_header(blob: bytes) -> Optional[tuple[int, int, int]]:
    if len(blob) < 4:
        return None
    size, frev, crev = struct.unpack_from("<HBB", blob, 0)
    return size, frev, crev


def _decode_struct(blob: bytes, base: int, sname: str) -> dict:
    size, fields = defs.STRUCTS[sname]
    out: dict[str, Any] = {}
    for name, off, kind, count in fields:
        if name == "common_header" or name.startswith("padding"):
            continue
        pos = base + off
        if kind.startswith("flex:"):
            continue
        if kind.startswith("struct:"):
            sub = kind.split(":", 1)[1]
            ssize = defs.STRUCTS[sub][0]
            items = []
            for i in range(count):
                if pos + (i + 1) * ssize > len(blob):
                    break
                items.append(_decode_struct(blob, pos + i * ssize, sub))
            out[name] = items
            continue
        w = _FMT_SIZE[kind]
        if count == 1:
            if pos + w > len(blob):
                out[name] = None
                continue
            v = struct.unpack_from("<" + kind, blob, pos)[0]
            out[name] = None if _unavailable(kind, v) else v
        else:
            vals = []
            for i in range(count):
                p = pos + i * w
                if p + w > len(blob):
                    vals.append(None)
                    continue
                v = struct.unpack_from("<" + kind, blob, p)[0]
                vals.append(None if _unavailable(kind, v) else v)
            out[name] = vals
    return out


def _decode_attrs(blob: bytes) -> tuple[dict, dict]:
    """Decode the __packed attribute-encoded layout (gpu_metrics v1_9, see
    DECLARE_SMU_METRICS_CLASS in smu_cmn.h): header, s32 attr_count, then for each
    attribute a u64 encoding followed by `inst` values of the encoded type."""
    fields: dict[str, Any] = {}
    units: dict[str, str] = {}
    if len(blob) < 8:
        return fields, units
    count = struct.unpack_from("<i", blob, 4)[0]
    pos = 8
    for _ in range(max(0, min(count, 4096))):
        if pos + 8 > len(blob):
            break
        enc = struct.unpack_from("<Q", blob, pos)[0]
        pos += 8
        unit = (enc >> 24) & 0xFF
        typ = (enc >> 20) & 0xF
        aid = (enc >> 10) & 0x3FF
        inst = enc & 0x3FF
        if typ >= len(_ATTR_TYPES):
            break
        fmt = _ATTR_TYPES[typ]
        w = _FMT_SIZE[fmt]
        name = (defs.ATTR_IDS[aid].lower() if aid < len(defs.ATTR_IDS) else f"attr_{aid}")
        vals = []
        for i in range(inst):
            if pos + w > len(blob):
                break
            v = struct.unpack_from("<" + fmt, blob, pos)[0]
            pos += w
            vals.append(None if _unavailable(fmt, v) else v)
        fields[name] = vals[0] if inst == 1 and vals else vals
        units[name] = defs.ATTR_UNITS[unit] if unit < len(defs.ATTR_UNITS) else "NONE"
    return fields, units


def parse_gpu_metrics(blob: bytes) -> Optional[dict]:
    """Return {'version': (frev, crev), 'struct': name, 'size': int, 'fields': {...},
    'exact': bool, 'attr_units': {...}} or None if the blob is unusable."""
    hdr = parse_header(blob)
    if hdr is None:
        return None
    size, frev, crev = hdr
    if size < 4 or frev == 0 or frev == 0xFF:
        return None
    blob = blob[:size] if len(blob) >= size else blob
    key = (frev, crev)
    sname = defs.VERSIONS.get(key)
    exact = sname is not None
    if sname is None:
        # Newer content revision we have no layout for: fall back to the newest
        # known revision of the same format family (fields are usually appended).
        cands = sorted(k for k in defs.VERSIONS if k[0] == frev and k[1] < crev)
        if not cands:
            return {"version": key, "struct": None, "size": size, "fields": {},
                    "exact": False, "attr_units": {}}
        sname = defs.VERSIONS[cands[-1]]
    if sname == "gpu_metrics_v1_9" or (not exact and frev == 1 and crev > 9):
        fields, units = _decode_attrs(blob)
        return {"version": key, "struct": "gpu_metrics_v1_9", "size": size,
                "fields": fields, "exact": exact, "attr_units": units}
    return {"version": key, "struct": sname, "size": size,
            "fields": _decode_struct(blob, 0, sname), "exact": exact, "attr_units": {}}


# --------------------------------------------------------------------------------------
# Semantics: map decoded fields to display rows.

# indep_throttle_status bits: SMU_THROTTLER_*_BIT in pm/swsmu/inc/amdgpu_smu.h
THROTTLER_BITS = {
    0: "PPT0", 1: "PPT1", 2: "PPT2", 3: "PPT3", 4: "SPL", 5: "FPPT", 6: "SPPT",
    7: "SPPT_APU",
    16: "TDC_GFX", 17: "TDC_SOC", 18: "TDC_MEM", 19: "TDC_VDD", 20: "TDC_CVIP",
    21: "EDC_CPU", 22: "EDC_GFX", 23: "APCC",
    32: "TEMP_GPU", 33: "TEMP_CORE", 34: "TEMP_MEM", 35: "TEMP_EDGE",
    36: "TEMP_HOTSPOT", 37: "TEMP_SOC", 38: "TEMP_VR_GFX", 39: "TEMP_VR_SOC",
    40: "TEMP_VR_MEM0", 41: "TEMP_VR_MEM1", 42: "TEMP_LIQUID0", 43: "TEMP_LIQUID1",
    44: "VRHOT0", 45: "VRHOT1", 46: "PROCHOT_CPU", 47: "PROCHOT_GFX",
    56: "PPM", 57: "FIT",
}
THROTTLER_CLASSES = [  # (key, label, bit range) -- SMU_THROTTLER_*_MASK groupings
    ("power", "Power limit (PPT/SPL)", range(0, 16)),
    ("current", "Current limit (TDC/EDC)", range(16, 32)),
    ("thermal", "Thermal limit", range(32, 48)),
    ("other", "Other (PPM/FIT)", range(48, 64)),
]


def decode_indep_throttle(v: Optional[int]) -> Optional[list[str]]:
    if v is None:
        return None
    return [n for b, n in THROTTLER_BITS.items() if v & (1 << b)] + \
           [f"BIT{b}" for b in range(64) if v & (1 << b) and b not in THROTTLER_BITS]


_TEMP_LABELS = {
    "temperature_edge": "Edge", "temperature_hotspot": "Hotspot (junction)",
    "temperature_mem": "Memory", "temperature_vrgfx": "VR GFX",
    "temperature_vrsoc": "VR SoC", "temperature_vrmem": "VR Memory",
    "temperature_gfx": "GFX", "temperature_soc": "SoC", "temperature_skin": "Skin",
    "temperature_core": "CPU core", "temperature_l3": "L3", "temperature_hbm": "HBM",
    "average_temperature_gfx": "GFX (avg)", "average_temperature_soc": "SoC (avg)",
    "average_temperature_core": "CPU core (avg)", "average_temperature_l3": "L3 (avg)",
}
_ACT_LABELS = {
    "average_gfx_activity": "GFX activity", "average_umc_activity": "Memory controller activity",
    "average_mm_activity": "Media (VCN) activity", "vcn_activity": "VCN activity",
    "jpeg_activity": "JPEG activity", "average_vcn_activity": "VCN activity",
    "average_ipu_activity": "IPU column activity", "average_core_c0_activity": "CPU core C0",
}
_POWER_LABELS = {
    "average_socket_power": "Socket power", "curr_socket_power": "Socket power",
    "average_gfx_power": "GFX power", "average_soc_power": "SoC power",
    "average_cpu_power": "CPU power", "average_core_power": "CPU core power",
    "average_ipu_power": "IPU power", "average_apu_power": "APU power",
    "average_dgpu_power": "dGPU power", "average_all_core_power": "All-core power",
    "average_sys_power": "System power", "stapm_power_limit": "STAPM limit (max)",
    "current_stapm_power_limit": "STAPM limit (current)",
}
_VOLT_LABELS = {
    "voltage_gfx": "VDDGFX", "voltage_soc": "VDDSOC", "voltage_mem": "VDDIO_MEM",
    "average_cpu_voltage": "CPU", "average_soc_voltage": "SoC", "average_gfx_voltage": "GFX",
}
_CURR_LABELS = {
    "average_cpu_current": "CPU current", "average_soc_current": "SoC current",
    "average_gfx_current": "GFX current",
}
_CLK_PREFIX_LABELS = [  # (field prefix, label)
    ("average_gfxclk_frequency", "GFX clock (avg)"), ("average_socclk_frequency", "SoC clock (avg)"),
    ("average_uclk_frequency", "Memory clock (avg)"), ("average_fclk_frequency", "Fabric clock (avg)"),
    ("average_vclk0_frequency", "VCLK0 (avg)"), ("average_dclk0_frequency", "DCLK0 (avg)"),
    ("average_vclk1_frequency", "VCLK1 (avg)"), ("average_dclk1_frequency", "DCLK1 (avg)"),
    ("average_vclk_frequency", "VCLK (avg)"), ("average_dclk_frequency", "DCLK (avg)"),
    ("average_vpeclk_frequency", "VPE clock (avg)"), ("average_ipuclk_frequency", "IPU clock (avg)"),
    ("average_mpipu_frequency", "MPIPU clock (avg)"),
    ("current_gfxclk", "GFX clock"), ("current_socclk", "SoC clock"), ("current_uclk", "Memory clock"),
    ("current_fclk", "Fabric clock"), ("current_vclk0", "VCLK0"), ("current_dclk0", "DCLK0"),
    ("current_vclk1", "VCLK1"), ("current_dclk1", "DCLK1"), ("current_vclk", "VCLK"),
    ("current_dclk", "DCLK"), ("current_coreclk", "CPU core clock"), ("current_l3clk", "L3 clock"),
    ("current_core_maxfreq", "CPU core max (limit)"), ("current_gfx_maxfreq", "GFX max (limit)"),
]
_CLK_LABELS = dict(_CLK_PREFIX_LABELS)

# fields deliberately not shown (counters/timestamps with no direct meaning per poll)
_SKIP = {"system_clock_counter", "firmware_timestamp", "gfx_activity_acc", "mem_activity_acc",
         "xcp_stats", "attr_count", "accumulation_counter", "time_filter_alphavalue"}


def _scales(version: tuple[int, int], smc_fw: Optional[int] = None) -> dict:
    frev = version[0]
    if frev == 1:
        return {"temp": 1.0, "act": 1.0, "power": 1.0}
    if frev == 2:
        return {"temp": 0.01, "act": 0.01, "power": 0.001}
    # v3: SMU 14.0.0 reports GFX activity in centi-% up to PMFW 0x5d4600
    act = 0.01 if (smc_fw is not None and smc_fw <= 0x5D4600) else 1.0
    return {"temp": 0.01, "act": act, "power": 0.001}


def metric_rows(parsed: dict, smc_fw: Optional[int] = None) -> list[dict]:
    """Turn a parsed blob into display rows:
    {key, cat, label, value, unit, decimals, hint}. `key` is stable per field/index.
    cat is one of: temp, clock, volt, current, power, activity, fan, pcie, throttle,
    memory, raw."""
    rows: list[dict] = []
    if not parsed:
        return rows
    ver = parsed["version"]
    vtag = f"v{ver[0]}_{ver[1]}"
    sc = _scales(ver, smc_fw)
    fields = parsed["fields"]
    attr_units = parsed.get("attr_units") or {}
    src = f"gpu_metrics {vtag}" + ("" if parsed.get("exact") else
                                   f" (decoded with {parsed.get('struct')} layout)")

    def add(key, cat, label, value, unit, dec, field):
        rows.append({"key": key, "cat": cat, "label": label, "value": value, "unit": unit,
                     "decimals": dec, "hint": f"{src}: {field}"})

    def each(name, val):
        """yield (key, index_suffix_label, value) for scalars and arrays."""
        if isinstance(val, list):
            for i, v in enumerate(val):
                yield f"{name}.{i}", f" {i}", v
        else:
            yield name, "", val

    if parsed["struct"] == "gpu_metrics_v1_9":
        unit_map = {"CLOCK_1": ("clock", "MHz", 1.0, 0), "TEMP_1": ("temp", "°C", 1.0, 0),
                    "POWER_1": ("power", "W", 1.0, 0), "PERCENT": ("activity", "%", 1.0, 0),
                    "SPEED_1": ("pcie", "GT/s", 1.0, 1), "SPEED_2": ("pcie", "GT/s", 0.1, 1),
                    "BW_1": ("memory", "GB/s", 1.0, 0), "DATA_1": ("raw", "KB", 1.0, 0)}
        for name, val in fields.items():
            if name in ("system_clock_counter", "firmware_timestamp"):
                continue
            cat, unit, scale, dec = unit_map.get(attr_units.get(name, "NONE"), ("raw", "", 1.0, 0))
            for key, sfx, v in each(name, val):
                add(key, cat, name.replace("_", " ").capitalize() + sfx,
                    None if v is None else v * scale, unit, dec, name)
        return rows

    for name, val in fields.items():
        if name in _SKIP:
            continue
        base = name
        if name in _TEMP_LABELS:
            for key, sfx, v in each(name, val):
                add(key, "temp", _TEMP_LABELS[base] + sfx,
                    None if v is None else v * sc["temp"], "°C", 0 if sc["temp"] == 1 else 1, name)
        elif name in _ACT_LABELS:
            for key, sfx, v in each(name, val):
                add(key, "activity", _ACT_LABELS[base] + sfx,
                    None if v is None else v * sc["act"], "%", 0 if sc["act"] == 1 else 1, name)
        elif name in _POWER_LABELS:
            for key, sfx, v in each(name, val):
                add(key, "power", _POWER_LABELS[base] + sfx,
                    None if v is None else v * sc["power"], "W", 0 if sc["power"] == 1 else 2, name)
        elif name in _VOLT_LABELS:
            for key, sfx, v in each(name, val):
                add(key, "volt", _VOLT_LABELS[base] + sfx,
                    None if v is None else v / 1000.0, "V", 3, name)
        elif name in _CURR_LABELS:
            for key, sfx, v in each(name, val):
                add(key, "current", _CURR_LABELS[base] + sfx,
                    None if v is None else v / 1000.0, "A", 2, name)
        elif name in _CLK_LABELS:
            for key, sfx, v in each(name, val):
                add(key, "clock", _CLK_LABELS[base] + sfx, v, "MHz", 0, name)
        elif name == "current_fan_speed":
            add(name, "fan", "Fan speed", val, "RPM", 0, name)
        elif name == "fan_pwm":
            add(name, "fan", "Fan PWM (SMU)", val, "", 0, name)
        elif name == "pcie_link_width":
            add(name, "pcie", "PCIe link width (SMU)", val, "lanes", 0, name)
        elif name == "pcie_link_speed":
            add(name, "pcie", "PCIe link speed (SMU)", None if val is None else val / 10.0,
                "GT/s", 1, name)
        elif name == "indep_throttle_status":
            active = decode_indep_throttle(val)
            add(name + ".count", "throttle", "Active throttlers",
                None if active is None else float(len(active)), "", 0, name)
            if active is not None:
                rows[-1]["hint"] += " = " + (", ".join(active) if active else "none")
            for ckey, clabel, rng in THROTTLER_CLASSES:
                v = None if val is None else float(any(val & (1 << b) for b in rng))
                add(f"{name}.{ckey}", "throttle", clabel, v, "", 0, name)
        elif name == "throttle_status":
            add(name, "throttle", "Throttle status (ASIC raw)",
                None if val is None else float(val), "", 0, name)
            if val is not None:
                rows[-1]["hint"] += f" = 0x{val:08x} (ASIC-dependent bits)"
        elif name == "energy_accumulator":
            # v1_x: 15.259 uJ units (2^-16 J); APUs: not populated
            if ver[0] == 1:
                add(name, "power", "Energy (accumulated)",
                    None if val is None else val / 65536.0 / 1000.0, "kJ", 3, name)
        elif name in ("average_dram_reads", "average_dram_writes", "average_ipu_reads",
                      "average_ipu_writes"):
            add(name, "memory", name.replace("average_", "").replace("_", " ").upper()
                .replace("READS", "reads").replace("WRITES", "writes"), val, "MB/s", 0, name)
        elif name == "mem_max_bandwidth":
            add(name, "memory", "VRAM max bandwidth", val, "GB/s", 0, name)
        else:
            if isinstance(val, list) and val and isinstance(val[0], dict):
                continue
            for key, sfx, v in each(name, val):
                add(key, "raw", name + sfx, None if v is None else float(v), "", 0, name)
    return rows
