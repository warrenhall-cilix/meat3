"""meat3-skutable: view and edit the RDNA4 board CustomSkuTable (what Windows tools call the
"SoftTuningTable"): power limits, current (TDC) limits, temperature limits, fan behaviour and the
board-power formula. Needs the meat3 amdgpu patch (tools/build_patched_amdgpu.sh), which uploads
/lib/firmware/amdgpu/custom_sku_<pci>.bin to the firmware at driver init.

    meat3-skutable --show                          # current table of the first RDNA4 GPU
    sudo meat3-skutable --tdc-gfx 360 --write      # raise GFX current limit, write the file
    sudo meat3-skutable --send                     # ask the patched driver to send it now
    sudo meat3-skutable --remove                   # back to stock at next boot

The firmware checks the table against the signed board limits and reports rejected sections
(PPT / TDC / TEMP / FAN) in the kernel log, so out-of-range values are refused, not applied.
"""
from __future__ import annotations

import argparse
import glob
import os
import struct
import subprocess
import sys
from typing import Optional

PPTABLE_SIZE = 5812
OFF_CUSTOM_SKU = 4924          # offsetof(smu_14_0_2_powerplay_table, smc_pptable.CustomSkuTable), gcc
SKU_SIZE = 360                 # sizeof(CustomSkuTable_t)
TEMPS = ["Edge", "Hotspot", "Hotspot GFX", "Hotspot SoC", "Memory", "VR GFX", "VR SoC", "VR Mem0",
         "VR Mem1", "Liquid 0", "Liquid 1", "PLX"]
# CustomSkuTable_t field offsets (gcc offsetof against smu14_driver_if_v14_0.h)
F = {"SocketPowerLimitAc": (0, "<4H"), "VrTdcLimit": (8, "<2H"), "TotalIdleBoardPowerM": (12, "<h"),
     "TotalIdleBoardPowerB": (14, "<h"), "TotalBoardPowerM": (16, "<h"), "TotalBoardPowerB": (18, "<h"),
     "TemperatureLimit": (20, "<12H"), "FanPwmMin": (116, "<H"), "AcousticTargetRpmThreshold": (118, "<H"),
     "AcousticLimitRpmThreshold": (120, "<H"), "FanMaximumRpm": (122, "<H"), "FanZeroRpmEnable": (132, "<B"),
     "FanTargetTemperature": (136, "<12H"), "FwCtfLimit": (168, "<12H"), "ODFeatureCtrlMask": (260, "<I"),
     "PlatformTdcLimit": (272, "<2H"), "SocketPowerLimitDc": (276, "<4H")}
FW_DIR = "/lib/firmware/amdgpu"


def get(t: bytes, name: str):
    off, fmt = F[name]
    v = struct.unpack_from(fmt, t, off)
    return list(v) if len(v) > 1 else v[0]


def put(t: bytearray, name: str, value) -> None:
    off, fmt = F[name]
    vals = value if isinstance(value, (list, tuple)) else [value]
    struct.pack_into(fmt, t, off, *vals)


def from_pptable(pp: bytes) -> bytes:
    if len(pp) < PPTABLE_SIZE or struct.unpack_from("<H", pp, 0)[0] != PPTABLE_SIZE:
        raise ValueError("not an RDNA4 (SMU 14.0.2/3) pp_table")
    return pp[OFF_CUSTOM_SKU:OFF_CUSTOM_SKU + SKU_SIZE]


def describe(t: bytes) -> str:
    ppt, tdc, ptdc = get(t, "SocketPowerLimitAc"), get(t, "VrTdcLimit"), get(t, "PlatformTdcLimit")
    temps = get(t, "TemperatureLimit")
    ctf = get(t, "FwCtfLimit")
    lines = [
        f"Power limit (PPT0, AC)      {ppt[0]} W   (PPT1 {ppt[1]} W)",
        f"Current limit GFX / SoC     VR {tdc[0]} / {tdc[1]} A   platform {ptdc[0]} / {ptdc[1]} A",
        f"Board power formula         idle M={get(t, 'TotalIdleBoardPowerM')} B={get(t, 'TotalIdleBoardPowerB')}   "
        f"load M={get(t, 'TotalBoardPowerM')} B={get(t, 'TotalBoardPowerB')}   (0 = firmware default)",
        "Temperature limits          " + ", ".join(f"{n} {v}" for n, v in zip(TEMPS, temps) if v),
        "Shutdown (CTF) limits       " + ", ".join(f"{n} {v}" for n, v in zip(TEMPS, ctf) if v),
        f"Fans                        max {get(t, 'FanMaximumRpm')} RPM, min PWM {get(t, 'FanPwmMin')}, "
        f"acoustic target/limit {get(t, 'AcousticTargetRpmThreshold')}/{get(t, 'AcousticLimitRpmThreshold')} RPM, "
        f"zero-RPM {'on' if get(t, 'FanZeroRpmEnable') else 'off'}",
    ]
    return "\n".join(lines)


def edit(t: bytes, a, stock: Optional[bytes] = None) -> tuple[bytes, list[str]]:
    """Apply CLI edits. Returns (new table, notes). Raises ValueError on refused edits."""
    b = bytearray(t)
    notes = []
    ref = stock or t
    if a.ppt is not None:
        for n in ("SocketPowerLimitAc", "SocketPowerLimitDc"):
            v = get(b, n)
            v[0] = a.ppt
            put(b, n, v)
        notes.append(f"PPT0 -> {a.ppt} W (the firmware caps this at the signed board maximum)")
    for idx, val, lab in ((0, a.tdc_gfx, "GFX"), (1, a.tdc_soc, "SoC")):
        if val is not None:
            for n in ("VrTdcLimit", "PlatformTdcLimit"):
                v = get(b, n)
                v[idx] = val
                put(b, n, v)
            notes.append(f"{lab} current limit -> {val} A")
    for spec in a.temp or []:
        name, _, val = spec.partition("=")
        match = [n for n in TEMPS if n.lower().replace(" ", "") == name.lower().replace(" ", "").replace("_", "")]
        if not match or not val.isdigit():
            raise ValueError(f"--temp {spec!r}: use NAME=C with NAME one of {', '.join(TEMPS)}")
        i = TEMPS.index(match[0])
        v = get(b, "TemperatureLimit")
        if int(val) > get(ref, "TemperatureLimit")[i] and not a.force:
            raise ValueError(f"raising the {match[0]} limit above stock ({get(ref, 'TemperatureLimit')[i]} C) "
                             "removes thermal headroom; add --force if you really mean it")
        if int(val) >= get(ref, "FwCtfLimit")[i] and get(ref, "FwCtfLimit")[i]:
            raise ValueError(f"{match[0]} limit must stay below its shutdown limit ({get(ref, 'FwCtfLimit')[i]} C)")
        v[i] = int(val)
        put(b, "TemperatureLimit", v)
        notes.append(f"{match[0]} temperature limit -> {val} C")
    if a.tbp_m is not None or a.tbp_b is not None:
        if not a.force:
            raise ValueError("the board-power formula is an experiment with unknown firmware behaviour; add --force")
        if a.tbp_m is not None:
            put(b, "TotalBoardPowerM", a.tbp_m)
        if a.tbp_b is not None:
            put(b, "TotalBoardPowerB", a.tbp_b)
        notes.append(f"board power formula -> M={get(b, 'TotalBoardPowerM')} B={get(b, 'TotalBoardPowerB')} "
                     "(experimental: watch the kernel log and the total board power sensor)")
    if a.zero_rpm is not None:
        put(b, "FanZeroRpmEnable", 1 if a.zero_rpm == "on" else 0)
        notes.append(f"zero-RPM {a.zero_rpm}")
    if a.fan_max_rpm is not None:
        put(b, "FanMaximumRpm", a.fan_max_rpm)
        notes.append(f"fan max -> {a.fan_max_rpm} RPM")
    return bytes(b), notes


def rdna4_cards() -> list[tuple[str, str]]:
    """(pci address, device dir) of GPUs exposing an RDNA4-sized pp_table."""
    out = []
    for dev in sorted(glob.glob("/sys/class/drm/card[0-9]*/device")):
        try:
            with open(os.path.join(dev, "pp_table"), "rb") as f:
                pp = f.read()
            from_pptable(pp)
        except (OSError, ValueError):
            continue
        out.append((os.path.basename(os.path.realpath(dev)), dev))
    return out


def debugfs_trigger(pci: str) -> Optional[str]:
    for d in glob.glob("/sys/kernel/debug/dri/*"):
        try:
            with open(os.path.join(d, "name")) as f:
                if pci not in f.read():
                    continue
        except OSError:
            continue
        p = os.path.join(d, "amdgpu_custom_sku")
        if os.path.exists(p):
            return p
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="meat3-skutable", description=__doc__.split("\n\n")[0])
    ap.add_argument("--gpu", help="PCI address (default: first RDNA4 GPU)")
    ap.add_argument("--from-pptable", help="read a pp_table file instead of the GPU (testing)")
    ap.add_argument("--ppt", type=int, help="PPT0 power limit in W")
    ap.add_argument("--tdc-gfx", type=int, help="GFX rail current limit in A")
    ap.add_argument("--tdc-soc", type=int, help="SoC rail current limit in A")
    ap.add_argument("--temp", action="append", metavar="NAME=C", help="temperature limit, e.g. Hotspot=100")
    ap.add_argument("--tbp-m", type=int, help="board-power formula slope (experimental, needs --force)")
    ap.add_argument("--tbp-b", type=int, help="board-power formula offset (experimental, needs --force)")
    ap.add_argument("--zero-rpm", choices=["on", "off"])
    ap.add_argument("--fan-max-rpm", type=int)
    ap.add_argument("--force", action="store_true", help="allow risky edits (temperature above stock, power formula)")
    ap.add_argument("--out", help="write the table here instead of /lib/firmware/amdgpu/custom_sku_<pci>.bin")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--show", action="store_true")
    g.add_argument("--write", action="store_true", help="write the edited table for the patched driver")
    g.add_argument("--send", action="store_true", help="ask the patched driver to send the file now")
    g.add_argument("--remove", action="store_true", help="delete the file (stock table at next driver load)")
    a = ap.parse_args(argv)

    if a.from_pptable:
        with open(a.from_pptable, "rb") as f:
            pci, pp = (a.gpu or "0000:00:00.0"), f.read()
    else:
        cards = rdna4_cards()
        if a.gpu:
            cards = [c for c in cards if c[0] == a.gpu]
        if not cards:
            print("no RDNA4 GPU found (needs amdgpu with an SMU 14.0.2/3 card)")
            return 2
        pci, dev = cards[0]
        with open(os.path.join(dev, "pp_table"), "rb") as f:
            pp = f.read()
    stock = from_pptable(pp)
    path = a.out or os.path.join(FW_DIR, f"custom_sku_{pci}.bin")

    if a.remove:
        try:
            os.unlink(path)
            print(f"removed {path}; the stock table is used from the next driver load / boot")
        except FileNotFoundError:
            print(f"{path} does not exist")
        return 0
    if a.send:
        trig = debugfs_trigger(pci)
        if not trig:
            print("the running amdgpu has no amdgpu_custom_sku debugfs file: the meat3 amdgpu patch isn't loaded")
            return 2
        with open(trig, "w") as f:
            f.write("1")
        log = subprocess.run(["dmesg"], capture_output=True, text=True).stdout.splitlines()
        print("\n".join([ln for ln in log if "custom SKU table" in ln][-3:]) or "(no kernel log line)")
        return 0
    try:
        current = open(path, "rb").read() if os.path.exists(path) and not a.out else stock
        if len(current) != SKU_SIZE:
            current = stock
        new, notes = edit(current, a, stock)
    except ValueError as e:
        print("refused:", e)
        return 2
    if not a.write:
        print(f"GPU {pci} CustomSkuTable ({'saved file ' + path if current is not stock else 'from the card'}):")
        print(describe(new if notes else current))
        if notes:
            print("\nPending edits (add --write to save):\n  " + "\n  ".join(notes))
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(new)
    print(f"wrote {path} ({SKU_SIZE} bytes)")
    for n in notes:
        print("  " + n)
    print("Takes effect at the next amdgpu load (reboot), or now with: sudo meat3-skutable --send\n"
          "If amdgpu is in your initramfs, add this file to it (mkinitcpio FILES=, dracut install_items).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
