"""Parser + command builder for amdgpu overdrive (OD) sysfs text.

Covers the formats printed by the upstream driver (drivers/gpu/drm/amd/pm):
  * pp_od_clk_voltage, read order: OD_SCLK, OD_MCLK, OD_FCLK, OD_VDDC_CURVE, OD_RANGE,
    OD_VDDGFX_OFFSET, OD_CCLK (amdgpu_get_pp_od_clk_voltage()).
      - RDNA4 / SMU 14.0.2-3:  "OD_SCLK_OFFSET:\\n<n>Mhz", "OD_MCLK:\\n0: <min>Mhz\\n1: <max>MHz",
        "OD_VDDGFX_OFFSET:\\n<n>mV", OD_RANGE "SCLK_OFFSET:", "MCLK:", "VDDGFX_OFFSET:" (mv)
        writes: "s <offset>", "m 0|1 <MHz>", "vo <mV>", then "c"; "r" = restore defaults.
      - RDNA3 / SMU 13.0.0/7, RDNA2 / SMU 11.0.7, APUs: "OD_SCLK:\\n0: <min>Mhz\\n1: <max>Mhz"
        writes "s 0|1 <MHz>" (+ "m 0|1", "vo" where present).
      - RDNA1 / Vega20: "OD_VDDC_CURVE:" points "vc <i> <MHz> <mV>".
      - Legacy (smu7/vega10/cyan_skillfish): per-level "s <lvl> <MHz> <mV>" -- parsed for
        display only, not written by meat3.
  * gpu_od/fan_ctrl/* (SMU13/14): "OD_FAN_CURVE:" points "<i>: <T>C <P>%", single-value
    files (acoustic limit/target, target temp, minimum pwm, zero-rpm enable/stop temp),
    each followed by "OD_RANGE:" lines. writes "<i> <T> <P>" / "<v>", then "c".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

_HDR_RE = re.compile(r"^([A-Z][A-Z0-9_]*(?:\[\d+\])?(?:\([^)]*\))?(?: in Core\d+)?):?$")
_ENTRY_RE = re.compile(r"^(\d+):\s*(-?\d+)\s*M[Hh][Zz](?:\s+(-?\d+)\s*m[Vv])?")
_FANPT_RE = re.compile(r"^(\d+):\s*(-?\d+)\s*C\s+(-?\d+)\s*%")
_SINGLE_RE = re.compile(r"^(-?\d+)\s*(?:M[Hh][Zz]|m[Vv]|C|%|RPM)?$")
_RANGE_RE = re.compile(r"^([A-Za-z0-9_]+(?:\[\d+\])?(?:\([^)]*\))?):\s*(-?\d+)\s*[A-Za-z%]*\s+(-?\d+)")


def parse_od_text(text: str) -> dict:
    """Generic section parser. Returns
    {'sections': [names in order], 'entries': {sec: {idx: (mhz, mv|None)}},
     'fan_points': {idx: (temp, pwm)}, 'values': {sec: int}, 'ranges': {KEY: (min, max)}}"""
    res = {"sections": [], "entries": {}, "fan_points": {}, "values": {}, "ranges": {}}
    sec = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        # range lines and header lines can both end in ':'-ish; a range line has numbers
        m = _HDR_RE.match(line)
        if m and not _RANGE_RE.match(line):
            sec = m.group(1)
            res["sections"].append(sec)
            continue
        if sec is None:
            continue
        if sec == "OD_RANGE":
            m = _RANGE_RE.match(line)
            if m:
                res["ranges"][m.group(1)] = (int(m.group(2)), int(m.group(3)))
            continue
        m = _FANPT_RE.match(line)
        if m:
            res["fan_points"][int(m.group(1))] = (int(m.group(2)), int(m.group(3)))
            continue
        m = _ENTRY_RE.match(line)
        if m:
            mv = int(m.group(3)) if m.group(3) is not None else None
            res["entries"].setdefault(sec, {})[int(m.group(1))] = (int(m.group(2)), mv)
            continue
        m = _SINGLE_RE.match(line)
        if m:
            res["values"][sec] = int(m.group(1))
            continue
        # Renoir prints "OD_RANGE\nSCLK: ..." without colon after OD_RANGE
        m = _RANGE_RE.match(line)
        if m:
            res["ranges"][m.group(1)] = (int(m.group(2)), int(m.group(3)))
    return res


@dataclass
class OdParam:
    key: str                  # control suffix, e.g. "sclk_offset"
    label: str
    unit: str
    file: str                 # relative to device dir
    value: Optional[int]
    min: Optional[int]
    max: Optional[int]
    template: str             # str.format(v=...) or with extra named fields
    default: Optional[int] = None
    danger: int = 1
    hint: str = ""
    kind: str = "int"
    extra: dict = field(default_factory=dict)   # e.g. fan point companion values
    available: bool = True

    def command(self, v: int) -> str:
        return self.template.format(v=v, **self.extra)


# Conservative bound used when the driver prints OD_VDDGFX_OFFSET without a range
# (RDNA2 / SMU 11.0.7 does no range check of its own, see sienna_cichlid_ppt.c).
RDNA2_VO_FALLBACK = (-200, 0)


def build_clock_params(od: dict, file: str = "pp_od_clk_voltage") -> tuple[list[OdParam], list[str]]:
    """Derive writable OD parameters from parsed pp_od_clk_voltage. Returns (params, notes)."""
    params: list[OdParam] = []
    notes: list[str] = []
    rng = od["ranges"]
    ent = od["entries"]
    vals = od["values"]

    legacy = any(mv is not None for e in (ent.get("OD_SCLK", {}), ent.get("OD_MCLK", {}))
                 for (_mhz, mv) in e.values())
    if legacy:
        notes.append("legacy per-level OD table (s/m <level> <MHz> <mV>): shown read-only")

    if "OD_SCLK_OFFSET" in vals:
        lo, hi = rng.get("SCLK_OFFSET", (None, None))
        params.append(OdParam("sclk_offset", "GFX clock offset", "MHz", file, vals["OD_SCLK_OFFSET"],
                              lo, hi, "s {v}", default=0, danger=1,
                              hint="OD_SCLK_OFFSET: shifts the whole GFX V/F curve (RDNA4)"))
    if not legacy:
        for sec, letter, rkey, name, lab in (("OD_SCLK", "s", "SCLK", "sclk", "GFX clock"),
                                             ("OD_MCLK", "m", "MCLK", "mclk", "Memory clock"),
                                             ("OD_FCLK", "f", "FCLK", "fclk", "Fabric clock")):
            pts = ent.get(sec, {})
            lo, hi = rng.get(rkey, (None, None))
            for idx, (mhz, _mv) in sorted(pts.items()):
                if idx not in (0, 1):
                    continue
                which = "min" if idx == 0 else "max"
                params.append(OdParam(f"{name}_{which}", f"{lab} {which}", "MHz", file, mhz, lo, hi,
                                      f"{letter} {idx} {{v}}", danger=1,
                                      hint=f"{sec} index {idx} ({which} DPM clock)"))
        for idx, (mhz, mv) in sorted(ent.get("OD_VDDC_CURVE", {}).items()):
            slo, shi = rng.get(f"VDDC_CURVE_SCLK[{idx}]", (None, None))
            vlo, vhi = rng.get(f"VDDC_CURVE_VOLT[{idx}]", (None, None))
            params.append(OdParam(f"vc{idx}_sclk", f"V/F point {idx} clock", "MHz", file, mhz, slo, shi,
                                  "vc " + str(idx) + " {v} {mv}", danger=2, extra={"mv": mv},
                                  hint="OD_VDDC_CURVE point (Vega20/RDNA1)"))
            params.append(OdParam(f"vc{idx}_volt", f"V/F point {idx} voltage", "mV", file, mv, vlo, vhi,
                                  "vc " + str(idx) + " {mhz} {v}", danger=2, extra={"mhz": mhz},
                                  hint="OD_VDDC_CURVE point (Vega20/RDNA1)"))
    if "OD_VDDGFX_OFFSET" in vals:
        if "VDDGFX_OFFSET" in rng:
            lo, hi = rng["VDDGFX_OFFSET"]
            h = "OD_VDDGFX_OFFSET: added to every V/F point; negative = undervolt"
        else:
            lo, hi = RDNA2_VO_FALLBACK
            h = ("OD_VDDGFX_OFFSET: driver reports no range here; meat3 limits it to "
                 f"{lo}..{hi} mV (undervolt only)")
        params.append(OdParam("vddgfx_offset", "GFX voltage offset", "mV", file,
                              vals["OD_VDDGFX_OFFSET"], lo, hi, "vo {v}", default=0, danger=2, hint=h))
    return params, notes


FAN_SINGLE_FILES = [  # (file, section header, OD_RANGE key, key, label, unit, kind, danger)
    ("acoustic_limit_rpm_threshold", "OD_ACOUSTIC_LIMIT", "ACOUSTIC_LIMIT",
     "fan_acoustic_limit", "Fan acoustic limit", "RPM", "int", 1),
    ("acoustic_target_rpm_threshold", "OD_ACOUSTIC_TARGET", "ACOUSTIC_TARGET",
     "fan_acoustic_target", "Fan acoustic target", "RPM", "int", 1),
    ("fan_target_temperature", "FAN_TARGET_TEMPERATURE", "TARGET_TEMPERATURE",
     "fan_target_temp", "Fan target temperature", "°C", "int", 1),
    ("fan_minimum_pwm", "FAN_MINIMUM_PWM", "MINIMUM_PWM",
     "fan_min_pwm", "Fan minimum PWM", "%", "int", 1),
    ("fan_zero_rpm_enable", "FAN_ZERO_RPM_ENABLE", "ZERO_RPM_ENABLE",
     "fan_zero_rpm", "Fan zero-RPM mode", "", "bool", 0),
    ("fan_zero_rpm_stop_temperature", "FAN_ZERO_RPM_STOP_TEMPERATURE", "ZERO_RPM_STOP_TEMPERATURE",
     "fan_zero_rpm_stop_temp", "Fan zero-RPM stop temperature", "°C", "int", 1),
]
FAN_DIR = "gpu_od/fan_ctrl"


def build_fan_params(texts: dict[str, str]) -> list[OdParam]:
    """texts: {file name under gpu_od/fan_ctrl: content}. Missing/empty files are skipped."""
    params: list[OdParam] = []
    curve = texts.get("fan_curve")
    if curve:
        od = parse_od_text(curve)
        tlo, thi = od["ranges"].get("FAN_CURVE(hotspot temp)", (None, None))
        plo, phi = od["ranges"].get("FAN_CURVE(fan speed)", (None, None))
        for idx, (t, p) in sorted(od["fan_points"].items()):
            params.append(OdParam(f"fan_curve{idx}_temp", f"Fan curve point {idx} temperature", "°C",
                                  f"{FAN_DIR}/fan_curve", t, tlo, thi, str(idx) + " {v} {pwm}",
                                  danger=1, extra={"pwm": p, "pair": "pwm"},
                                  hint="OD_FAN_CURVE (hotspot temperature axis)"))
            params.append(OdParam(f"fan_curve{idx}_pwm", f"Fan curve point {idx} speed", "%",
                                  f"{FAN_DIR}/fan_curve", p, plo, phi, str(idx) + " {temp} {v}",
                                  danger=1, extra={"temp": t, "pair": "temp"},
                                  hint="OD_FAN_CURVE (fan speed axis)"))
    for fname, sec, rkey, key, label, unit, kind, danger in FAN_SINGLE_FILES:
        txt = texts.get(fname)
        if not txt:
            continue
        od = parse_od_text(txt)
        if sec not in od["values"]:
            continue
        lo, hi = od["ranges"].get(rkey, (None, None))
        params.append(OdParam(key, label, unit, f"{FAN_DIR}/{fname}", od["values"][sec], lo, hi,
                              "{v}", danger=danger, kind=kind, hint=f"{FAN_DIR}/{fname}"))
    return params
