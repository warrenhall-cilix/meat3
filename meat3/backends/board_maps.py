"""Motherboard Super I/O label maps: what each raw hwmon input means on a given board.

Linux exposes Super I/O chips raw: in0..inN are pin voltages, temp1..N and fan1..N are just
numbered. This table names them and applies the board's resistor dividers, the way HWiNFO and
LibreHardwareMonitor do.

Sources (Oct 2026):
  * LibreHardwareMonitor SuperIOHardware.cs (per-board names and Ri/Rf dividers)
  * frankcrawford/it87 gigabyte-it87-amd.conf (Gigabyte AM5, keyed by the board's SIV id)
  * Fred78290/nct6687d (MSI) and the mainline it87 / nct6775 / nct6683 drivers and docs

Multipliers are relative to the value hwmon reports. The drivers already double some inputs
(it87: in7/in8/in9, plus in3 when in3_label exists; nct6798/6799: in2/3/7/8/9/10/11/16), so a
LibreHardwareMonitor x2 on those becomes x1 here.

Every scaled rail with a known nominal voltage (+12V, +5V, +3.3V, standby, battery) is
plausibility checked: if the scaled value isn't within the expected window, the board backend
falls back to the raw pin value, so an unverified or wrong entry can't show a bogus +12V.
"""
from __future__ import annotations

import re
from typing import Optional

# expected window (V) for rails we can sanity check; matched against the label
NOMINAL = [(re.compile(r"\+12V"), 10.0, 14.0), (re.compile(r"\+5V"), 4.2, 5.8),
           (re.compile(r"\+3\.3V|3V Standby|3\.3V Standby|AVSB|AVCC|3VSB"), 2.8, 3.8),
           (re.compile(r"CMOS Battery|Vbat"), 1.8, 3.6)]

# open/unconnected thermistor readings: ITE -55/-128 °C, Nuvoton 127/255 (a real 125 °C VRM must show)
OPEN_PROBE = lambda v: v is not None and (v <= -40.0 or v >= 127.0)

GIGABYTE = "gigabyte"
ASUS = "asustek"
ASROCK = "asrock"
MSI = "micro-star"

# ---------------------------------------------------------------- per-board / per-chip entries
# match: chip = hwmon name prefix (before "_"), vendor = substring of DMI board_vendor (lower),
# boards = DMI board_name list (case-insensitive; None = any board of that vendor)
# in: {key: (label, mult)}, temp/fan: {key: label}, temp_by_label: {driver label: name},
# hide: keys to drop
_GB_X870E_BOARDS = ["X870E AORUS PRO", "X870E AORUS PRO ICE", "X870E AORUS XTREME AI TOP", "X870E AORUS MASTER",
                    "X870 AORUS ELITE WIFI7", "X870 AORUS ELITE WIFI7 ICE"]
_GB_B650_BOARDS = ["B650 AORUS ELITE", "B650 AORUS ELITE AX", "B650 AORUS ELITE AX ICE", "B650 AORUS ELITE AX V2",
                   "B650 AORUS ELITE V2", "B650E AORUS ELITE X AX ICE", "B650E AORUS STEALTH ICE", "B650M AORUS ELITE",
                   "B650M AORUS ELITE AX", "B650M AORUS ELITE AX ICE", "B650M AORUS PRO AX", "B650 GAMING X AX",
                   "B650M AORUS PRO", "B650 EAGLE AX"]
_GB_IT_RAILS = {"in0": ("Vcore", 1.0), "in1": ("+3.3V", 1.649), "in2": ("+12V", 6.0), "in3": ("+5V", 2.5),
                "in4": ("CPU VDDCR SoC", 1.0), "in5": ("CPU VDD Misc", 1.0), "in6": ("CPU VDDIO / Memory", 1.0),
                "in7": ("+3V Standby", 1.0), "in8": ("CMOS Battery", 1.0), "in9": ("AVCC3", 1.0)}
_GB_FANS = {"fan1": "CPU Fan", "fan2": "System Fan 1", "fan3": "System Fan 2", "fan4": "System Fan 3",
            "fan5": "CPU Optional Fan"}

_ASUS_800 = {"in0": ("Vcore", 1.0), "in1": ("+5V", 5.0), "in2": ("AVSB", 1.0), "in3": ("+3.3V", 1.0),
             "in4": ("+12V", 12.0), "in7": ("+3V Standby", 1.0), "in8": ("CMOS Battery", 1.0),
             "in10": ("CPU VDDIO / Memory", 1.0)}

ENTRIES: list[dict] = [
    # ---- Gigabyte AM5, ITE IT8696E (+ IT87952E second chip). Verified on an X870E AORUS PRO.
    {"name": "Gigabyte X870/X870E (IT8696E)", "chip": "it8696", "vendor": GIGABYTE, "boards": _GB_X870E_BOARDS,
     "in": _GB_IT_RAILS,
     "temp": {"temp1": "System 1", "temp2": "PCH (chipset)", "temp3": "CPU (socket)", "temp4": "PCIe x16",
              "temp5": "VRM MOS", "temp6": "EC_TEMP1 header"},
     "fan": _GB_FANS, "hide": ["fan6", "pwm6"]},
    {"name": "Gigabyte X870/X870E (IT87952E)", "chip": "it87952", "vendor": GIGABYTE, "boards": _GB_X870E_BOARDS,
     "in": {"in2": ("PM VCC18", 1.0), "in4": ("CPU VDD18", 1.0), "in5": ("PM VDD1V", 1.0)},
     "temp": {"temp1": "PCIe x4", "temp2": "EC_TEMP2 header", "temp3": "System 2"},
     "fan": {"fan1": "System Fan 5 / Pump", "fan2": "System Fan 6 / Pump", "fan3": "System Fan 4"},
     "hide": ["in0", "in1", "in3", "in6", "in7", "in8"]},          # unused pins; 3VSB/Vbat duplicate the IT8696E
    # ---- Gigabyte AM5 B650 family (IT8689E)
    {"name": "Gigabyte B650 (IT8689E)", "chip": "it8689", "vendor": GIGABYTE, "boards": _GB_B650_BOARDS,
     "in": {**_GB_IT_RAILS, "in6": ("DDR5 +5V", 2.5)},
     "temp": {"temp1": "System", "temp2": "PCH (chipset)", "temp3": "CPU (socket)", "temp4": "PCIe x16",
              "temp5": "VRM MOS", "temp6": "VSoC MOS"},
     "fan": {**_GB_FANS, "fan6": "System Fan 4 / Pump"}},
    # ---- Gigabyte, any other board's main ITE chip (IT86xx; not the IT879xx second chip, whose pins
    # mean something else): the long-standing Gigabyte rail layout, kept where the plausibility check agrees
    {"name": "Gigabyte (ITE, generic)", "chip": "it86", "vendor": GIGABYTE, "boards": None,
     "in": _GB_IT_RAILS, "fan": {"fan1": "CPU Fan"}, "guess": True},

    # ---- ASUS (Nuvoton NCT6799D / NCT6701D, Linux kind nct6799)
    {"name": "ASUS X870E (NCT6701D)", "chip": "nct6799", "vendor": ASUS,
     "boards": ["ROG STRIX X870E-E GAMING WIFI", "ROG STRIX X870E-E GAMING WIFI7 R2", "ROG CROSSHAIR X870E HERO"],
     "in": _ASUS_800,
     "fan": {"fan1": "Chassis Fan 1", "fan2": "CPU Fan", "fan3": "Chassis Fan 2", "fan4": "Chassis Fan 3",
             "fan5": "Chassis Fan 4", "fan6": "Chassis Fan 5", "fan7": "AIO Pump"},
     "temp_by_label": {"CPUTIN": None},       # kernel docs: CPUTIN is meaningless on ASUS boards
     "hide": ["in5", "in6", "in9", "in11", "in12", "in13", "in14", "in15", "in16", "in17"]},
    {"name": "ASUS B850/X870 (NCT6701D)", "chip": "nct6799", "vendor": ASUS,
     "boards": ["ROG STRIX B850-A GAMING WIFI", "TUF GAMING B850-BTF WIFI W", "TUF GAMING X870-PRO WIFI7 W NEO",
                "ROG STRIX B850-E GAMING WIFI", "ROG STRIX B850-I GAMING WIFI", "TUF GAMING B850M-PLUS WIFI II",
                "ROG STRIX X870-I GAMING WIFI"],
     "in": {**_ASUS_800, "in1": ("+5V", 5.02), "in4": ("+12V", 11.98), "in9": ("VTT", 1.0),
            "in11": ("VDD Misc", 1.0), "in12": ("+1.8V Standby", 1.766)},
     # fan headers differ between these boards (and the source lists names, not channels): left as Fan n
     "temp_by_label": {"CPUTIN": None},
     "hide": ["in5", "in6", "in13", "in14", "in15", "in16", "in17"]},
    {"name": "ASUS PRIME B650-PLUS (NCT6799D)", "chip": "nct6799", "vendor": ASUS, "boards": ["PRIME B650-PLUS"],
     "in": {**_ASUS_800},
     "fan": {"fan1": "Chassis Fan 1", "fan2": "CPU Fan", "fan3": "Chassis Fan 2", "fan4": "Chassis Fan 3",
             "fan5": "CPU Optional Fan", "fan6": "AIO Pump"},
     "temp_by_label": {"CPUTIN": None},
     "hide": ["in5", "in6", "in9", "in11", "in12", "in13", "in14", "in15", "in16", "in17"]},
    {"name": "ASUS (Nuvoton, generic)", "chip": "nct67", "vendor": ASUS, "boards": None,
     "in": {"in0": ("Vcore", 1.0), "in1": ("+5V", 5.0), "in2": ("AVSB", 1.0), "in3": ("+3.3V", 1.0),
            "in4": ("+12V", 12.0), "in7": ("+3V Standby", 1.0), "in8": ("CMOS Battery", 1.0)},
     "fan": {"fan1": "Chassis Fan 1", "fan2": "CPU Fan"}, "guess": True,
     "temp_by_label": {"CPUTIN": None}},       # kernel docs: CPUTIN is meaningless on ASUS boards

    # ---- ASRock AM5 (NCT6799D / NCT6796D-S)
    {"name": "ASRock X870E Taichi (NCT6799D)", "chip": "nct6799", "vendor": ASROCK, "boards": ["X870E Taichi"],
     "in": {"in0": ("Vcore", 1.0), "in1": ("+12V", 6.6), "in3": ("+3.3V", 1.0), "in4": ("+5V", 3.0),
            "in5": ("+1.05V Always-on", 1.0), "in7": ("+3V Standby", 1.0), "in8": ("CMOS Battery", 1.0),
            "in10": ("CPU VDDCR SoC", 1.0), "in11": ("CPU VDD Misc", 1.0), "in13": ("+1.8V", 2.0)},
     "fan": {"fan1": "Chassis Fan 1", "fan2": "CPU Fan 1", "fan3": "CPU Fan 2", "fan4": "AIO Pump",
             "fan5": "Chassis Fan 2"}},
    {"name": "ASRock B650M-C (NCT6799D)", "chip": "nct6799", "vendor": ASROCK, "boards": ["B650M-C"],
     "in": {"in0": ("Vcore", 1.0), "in1": ("+12V", 6.6), "in2": ("AVCC", 1.0), "in3": ("+3.3V", 1.0),
            "in4": ("+5V", 3.0), "in5": ("+1.05V Always-on", 1.0), "in7": ("+3V Standby", 1.0),
            "in8": ("CMOS Battery", 1.0), "in9": ("CPU Termination", 1.0), "in10": ("CPU VDDCR SoC", 1.0),
            "in11": ("CPU VDD Misc", 1.0), "in13": ("+1.8V", 2.0), "in14": ("DIMM", 1.0)},
     "fan": {"fan1": "CPU Fan 2 / Pump", "fan2": "CPU Fan 1", "fan4": "Chassis Fan 1", "fan5": "Chassis Fan 2",
             "fan7": "Chassis Fan 3"}},
    {"name": "ASRock AM5 (Nuvoton, generic)", "chip": "nct67", "vendor": ASROCK, "boards": None,
     "in": {"in0": ("Vcore", 1.0), "in1": ("+12V", 6.6), "in2": ("AVCC", 1.0), "in3": ("+3.3V", 1.0),
            "in4": ("+5V", 3.0), "in7": ("+3V Standby", 1.0), "in8": ("CMOS Battery", 1.0),
            "in10": ("CPU VDDCR SoC", 1.0), "in11": ("CPU VDD Misc", 1.0)},
     "fan": {"fan2": "CPU Fan 1"}, "guess": True},
]

# ---------------------------------------------------------------- chip defaults (any board)
NUVOTON_TEMP = {
    "SYSTIN": "Motherboard", "CPUTIN": "CPU socket", "AUXTIN0": "Auxiliary 0", "AUXTIN1": "Auxiliary 1",
    "AUXTIN2": "Auxiliary 2", "AUXTIN3": "Auxiliary 3", "AUXTIN4": "Auxiliary 4", "AUXTIN5": "Thermistor (T_Sensor)",
    "SMBUSMASTER 0": "CPU (SMBus)", "SMBUSMASTER 1": "SMBus master 1",
    "PECI Agent 0": "CPU", "PECI Agent 1": "CPU 2", "PECI Agent 0 Calibration": "CPU (calibrated)",
    "PECI/TSI Agent 0 Calibration": "CPU (calibrated)", "PECI Agent 1 Calibration": "CPU 2 (calibrated)",
    "PECI/TSI Agent 1 Calibration": "CPU 2 (calibrated)",
    "PCH_CHIP_CPU_MAX_TEMP": "PCH max", "PCH_CHIP_TEMP": "PCH (chipset)", "PCH_CPU_TEMP": "PCH CPU",
    "PCH_MCH_TEMP": "PCH MCH", "Agent0 Dimm0": "DIMM 1", "Agent0 Dimm1": "DIMM 2", "Agent1 Dimm0": "DIMM 3",
    "Agent1 Dimm1": "DIMM 4", "BYTE_TEMP0": None, "BYTE_TEMP1": None, "Virtual_TEMP": None,
}
NUVOTON_IN = {"in0": ("Vcore", 1.0), "in2": ("AVSB", 1.0), "in3": ("+3.3V", 1.0), "in7": ("+3V Standby", 1.0),
              "in8": ("CMOS Battery", 1.0)}
NUVOTON_PINS = {"in1": "VIN1", "in4": "VIN0", "in5": "VIN8", "in6": "VIN4", "in9": "VTT", "in10": "VIN5",
                "in11": "VIN6", "in12": "VIN2", "in13": "VIN3", "in14": "VIN7", "in15": "VIN9", "in16": "VHIF",
                "in17": "VIN10"}
ITE_IN = {"in0": ("Vcore", 1.0)}
ITE_LABELS = {"3VSB": "+3V Standby", "Vbat": "CMOS Battery"}   # it87 driver labels (in9 "+3.3V" is AVCC3)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).upper()


def chip_family(hwmon_name: str) -> str:
    return hwmon_name.split("_", 1)[0].lower()     # "it8696_a008090a" -> "it8696"


def lookup(hwmon_name: str, vendor: str, board: str) -> Optional[dict]:
    """Best entry for this chip on this board: exact board first, then the vendor's generic one."""
    fam, v, b = chip_family(hwmon_name), (vendor or "").lower(), _norm(board)
    exact = generic = None
    for e in ENTRIES:
        if not fam.startswith(e["chip"]) or e["vendor"] not in v:
            continue
        if e["boards"] is None:
            generic = generic or e
        elif b in {_norm(x) for x in e["boards"]}:
            exact = exact or e
    return exact or generic


def plausible(label: str, volts: float) -> bool:
    for rx, lo, hi in NOMINAL:
        if rx.search(label):
            return lo <= volts <= hi
    return True


def describe(hwmon_name: str, key: str, driver_label: Optional[str], entry: Optional[dict]) -> dict:
    """How to show one channel: {label, mult, hidden, mapped, guess}. driver_label is the
    driver's own <key>_label (None if the file doesn't exist)."""
    fam = chip_family(hwmon_name)
    nuvoton = fam.startswith(("nct67", "nct5", "w836"))
    ite = fam.startswith(("it86", "it87"))
    out = {"label": driver_label or key, "mult": 1.0, "hidden": False, "mapped": False,
           "guess": bool(entry and entry.get("guess"))}
    if entry and key in entry.get("hide", []):
        out["hidden"] = True
        return out
    kind = re.match(r"[a-z]+", key).group(0)
    if kind == "in":
        if ite and key == "in3" and driver_label:
            # in3_label only exists when VIN3 carries AVCC (+3.3V, doubled by the driver): the board
            # map's +5V divider doesn't apply to this board
            out.update(label=driver_label, mapped=True)
            return out
        spec = (entry or {}).get("in", {}).get(key)
        if spec is None and nuvoton:
            spec = NUVOTON_IN.get(key)
            if spec is None and key in NUVOTON_PINS:
                out["label"] = f"{NUVOTON_PINS[key]} (raw pin)"
        if spec is None and ite:
            if driver_label in ITE_LABELS:
                spec = (ITE_LABELS[driver_label], 1.0)
            elif driver_label == "+3.3V" and key == "in9":
                spec = ("AVCC3", 1.0)
            else:
                spec = ITE_IN.get(key)
                if spec is None and not driver_label:
                    out["label"] = f"VIN{key[2:]} (raw pin)"
        if spec:
            label, mult = spec
            out.update(label=label, mult=mult, mapped=True)
        return out
    if kind == "temp":
        lab = (entry or {}).get("temp", {}).get(key)
        if lab is None and driver_label is not None:
            by = {**NUVOTON_TEMP, **((entry or {}).get("temp_by_label") or {})} if nuvoton else \
                ((entry or {}).get("temp_by_label") or {})
            if driver_label in by:
                if by[driver_label] is None:
                    out["hidden"] = True
                    return out
                lab = by[driver_label]
        if lab:
            out.update(label=lab, mapped=True)
        elif not driver_label:
            out["label"] = f"Temperature {key[4:]}"
        return out
    if kind in ("fan", "pwm"):
        n = re.sub(r"\D", "", key)
        lab = (entry or {}).get("fan", {}).get(f"fan{n}")
        if lab:
            out.update(label=lab if kind == "fan" else f"{lab} duty", mapped=True)
        elif not driver_label:
            out["label"] = f"Fan {n}" if kind == "fan" else f"Fan {n} duty"
        return out
    return out
