"""meat3-fasttiming: AMD's "Fast Timing" memory-timing mode on RDNA4 under Linux.

    sudo meat3-fasttiming status             # card, kernel interface, memory DPM levels, TA state
    sudo meat3-fasttiming load               # load AMD's Powerplay trusted application into the PSP
    sudo meat3-fasttiming query              # ask it for the timing ranges (proves it runs and has a table)
    sudo meat3-fasttiming enable [--level 1] # apply fast timings (level N = N timing bins tighter)
    sudo meat3-fasttiming disable            # back to default timings
    sudo meat3-fasttiming unload

How Windows does it (reverse-engineered from the Adrenalin kernel driver): memory timings are not an
SMU setting. The driver loads a PSP trusted application ("AMD POWERPLAY Application"), pins the memory
clock to the next-lower DPM level, asks the TA to program the UMC timing registers from the VBIOS
timing table for a tighter bin, and releases the clock. The same TA images are shipped with meat3
(meat3/firmware/ppta) and the Linux kernel's own debugfs `ta_if` interface (kernel 6.1+) can load and
invoke them, so no kernel patch is needed. Everything is volatile: a reboot restores stock timings.

EXPERIMENTAL and untested as of writing. Possible failure modes: the PSP refuses the image (clean
error), the TA reports an error (clean), or the memory controller is programmed badly and the screen
freezes (reboot). Run it on the desktop with nothing important open.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import struct
import subprocess
import sys
import time
from typing import Optional

TA_TYPE_RAS = 2                   # the only TA type the kernel's ta_if accepts; it only selects a context
IMAGES = ["ppta_a.bin", "ppta_b.bin", "ppta_c.bin"]
FW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "firmware", "ppta")
STATE = "/var/lib/meat3/fasttiming.json"
DEBUGFS = "/sys/kernel/debug/dri"
# TA shared-buffer layout (AMD TA convention): cmd, resp (cmd|0x80000000 when done), status, pad,
# input at +0x10, output at +0x48
IN_OFF, OUT_OFF, OUT_LEN, BUF_LEN = 0x10, 0x48, 0x40, 0x100
# TA commands (from the driver's SW_PSPM_CallPowerplayTA -> psp_powerplay_* mapping)
CMD_ENABLE, CMD_DISABLE, CMD_QUERY, CMD_FULL_UPDATE, CMD_UMC_CONFIG, CMD_UMC_PLL, CMD_CUSTOM = range(7)
CLK_UNIT = 100                    # VBIOS umc timing blocks use 10 kHz units: MHz * 100


class Card:
    def __init__(self, dev: str, minor: int):
        self.dev, self.minor = dev, minor

    @property
    def dbg(self) -> Optional[str]:
        for d in (os.path.join(DEBUGFS, str(self.minor)), os.path.join(DEBUGFS, os.path.basename(os.path.realpath(self.dev)))):
            if os.path.isdir(os.path.join(d, "ta_if")):
                return os.path.join(d, "ta_if")
        return None

    def read(self, name: str) -> str:
        try:
            with open(os.path.join(self.dev, name)) as f:
                return f.read()
        except OSError:
            return ""

    def write(self, name: str, val: str) -> None:
        with open(os.path.join(self.dev, name), "w") as f:
            f.write(val)

    def mclk_levels(self) -> tuple[list[int], Optional[int]]:
        levels, cur = [], None
        for line in self.read("pp_dpm_mclk").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].rstrip(":").isdigit():
                mhz = int("".join(ch for ch in parts[1] if ch.isdigit()) or 0)
                levels.append(mhz)
                if line.rstrip().endswith("*"):
                    cur = len(levels) - 1
        return levels, cur


def find_card(root: str = "/") -> Optional[Card]:
    for card in sorted(glob.glob(os.path.join(root, "sys/class/drm/card*"))):
        name = os.path.basename(card)
        if not name[4:].isdigit():
            continue
        dev = os.path.join(card, "device")
        try:
            with open(os.path.join(dev, "vendor")) as f:
                if f.read().strip().lower() != "0x1002":
                    continue
            with open(os.path.join(dev, "ip_discovery/die/0/MP1/0/major")) as f:
                if int(f.read().strip()) != 14:
                    continue
        except OSError:
            continue
        return Card(dev, int(name[4:]))
    return None


class TaIf:
    """The kernel's /sys/kernel/debug/dri/N/ta_if interface (amdgpu_psp_ta.c)."""

    def __init__(self, path: str):
        self.path = path

    def _write(self, name: str, buf: bytearray) -> int:
        fd = os.open(os.path.join(self.path, name), os.O_WRONLY)
        try:
            return os.write(fd, memoryview(buf))      # the kernel writes results back into this buffer
        finally:
            os.close(fd)

    def load(self, image: bytes) -> int:
        buf = bytearray(struct.pack("<II", TA_TYPE_RAS, len(image)) + image)
        self._write("ta_load", buf)
        return struct.unpack_from("<I", buf, 0)[0]          # session id

    def unload(self, session: int) -> None:
        self._write("ta_unload", bytearray(struct.pack("<II", TA_TYPE_RAS, session)))

    def invoke(self, session: int, cmd: int, data: bytes) -> tuple[int, int, bytes, bytes]:
        shared = bytearray(BUF_LEN)
        struct.pack_into("<I", shared, 0, cmd)
        shared[IN_OFF:IN_OFF + len(data)] = data
        buf = bytearray(struct.pack("<IIII", TA_TYPE_RAS, session, cmd, BUF_LEN) + bytes(shared))
        self._write("ta_invoke", buf)
        back = bytes(buf[16:16 + BUF_LEN])
        resp, status = struct.unpack_from("<II", back, 4)
        return status, resp, back[OUT_OFF:OUT_OFF + OUT_LEN], back


def load_state() -> dict:
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE + ".tmp", "w") as f:
        json.dump(st, f)
    os.replace(STATE + ".tmp", STATE)


def dmesg_tail(n: int = 6) -> str:
    try:
        r = subprocess.run(["journalctl", "-k", "-n", str(n), "--no-pager", "-o", "short"], capture_output=True, text=True, timeout=10)
        return "\n".join("    " + line for line in r.stdout.splitlines()[-n:])
    except (OSError, subprocess.SubprocessError):
        return ""


def hexwords(b: bytes, n: int = 16) -> str:
    return " ".join(f"{w:08x}" for w in struct.unpack_from(f"<{n}I", b, 0))


def cmd_status(card: Card, st: dict) -> int:
    print(f"card: {card.dev} (drm minor {card.minor}), SMU fw {card.read('fw_version/smc_fw_version').strip() or '?'}")
    dbg = card.dbg
    print(f"kernel ta_if: {dbg or 'NOT FOUND (debugfs mounted? kernel with amdgpu_psp_ta.c?)'}")
    levels, cur = card.mclk_levels()
    print(f"memory DPM levels (MHz): {levels} current {cur}; perf level {card.read('power_dpm_force_performance_level').strip()}")
    imgs = [(n, os.path.getsize(os.path.join(FW_DIR, n))) for n in IMAGES if os.path.exists(os.path.join(FW_DIR, n))]
    print(f"TA images: {imgs}")
    print(f"state: {st or 'nothing loaded'}")
    return 0


def cmd_load(card: Card, st: dict, which: str) -> int:
    dbg = card.dbg
    if not dbg:
        print("ta_if not available"); return 2
    ta = TaIf(dbg)
    if st.get("session") is not None:
        print(f"already loaded (session {st['session']}, {st.get('image')}); unload first")
        return 1
    order = IMAGES if which == "auto" else [which]
    for name in order:
        path = os.path.join(FW_DIR, name)
        if not os.path.exists(path):
            print(f"{name}: missing"); continue
        with open(path, "rb") as f:
            img = f.read()
        print(f"loading {name} ({len(img):#x} bytes) ...")
        try:
            session = ta.load(img)
        except OSError as e:
            print(f"   refused: {e.strerror or e}")
            print(dmesg_tail(4))
            continue
        print(f"   loaded, session id {session:#x}")
        print(dmesg_tail(3))
        st.update({"session": session, "image": name, "t": time.time()})
        save_state(st)
        return 0
    print("no image loaded")
    return 1


def need_session(card: Card, st: dict):
    dbg = card.dbg
    if not dbg or st.get("session") is None:
        print("TA not loaded (run: meat3-fasttiming load)")
        return None
    return TaIf(dbg)


def top_uclk(card: Card) -> int:
    levels, _ = card.mclk_levels()
    if not levels:
        raise SystemExit("cannot read pp_dpm_mclk")
    return max(levels)


def cmd_query(card: Card, st: dict, unit: int) -> int:
    ta = need_session(card, st)
    if not ta:
        return 2
    data = struct.pack("<III", top_uclk(card) * unit, 0xFFFFFFFF, 0xFFFFFFFF)
    status, resp, out, raw = ta.invoke(st["session"], CMD_QUERY, data)
    print(f"query: status {status:#x} resp {resp:#x}")
    print("output:", hexwords(out))
    print(dmesg_tail(3))
    st["last_query"] = {"status": status, "out": out.hex()}
    save_state(st)
    return 0 if status == 0 else 1


def pin_mclk(card: Card, level: int) -> dict:
    prev = {"perf": card.read("power_dpm_force_performance_level").strip()}
    card.write("power_dpm_force_performance_level", "manual")
    card.write("pp_dpm_mclk", str(level))
    time.sleep(0.3)
    return prev


def unpin_mclk(card: Card, prev: dict) -> None:
    try:
        card.write("power_dpm_force_performance_level", prev.get("perf") or "auto")
    except OSError as e:
        print(f"!! could not restore performance level: {e}")


def cmd_set(card: Card, st: dict, level: int, unit: int, no_pin: bool) -> int:
    ta = need_session(card, st)
    if not ta:
        return 2
    levels, _ = card.mclk_levels()
    uclk = max(levels) * unit
    # the Windows driver holds UCLK one DPM level below the top while the TA programs the registers
    pin_level = max(0, len(levels) - 2)
    data = struct.pack("<8I", 0, 0, 0, 0, 0, 0, uclk, level)
    cmd = CMD_ENABLE if level > 0 else CMD_DISABLE
    prev = None
    if not no_pin:
        prev = pin_mclk(card, pin_level)
        print(f"memory clock pinned to DPM level {pin_level} ({levels[pin_level]} MHz)")
    try:
        status, resp, out, raw = ta.invoke(st["session"], cmd, data)
    finally:
        if prev is not None:
            unpin_mclk(card, prev)
            print("memory clock released")
    name = "enable" if level > 0 else "disable"
    print(f"{name} level {level} (uclk {uclk}): status {status:#x} resp {resp:#x}")
    print("output:", hexwords(out, 8))
    print(dmesg_tail(3))
    st["level"] = level if status == 0 else st.get("level")
    save_state(st)
    if status != 0:
        print("TA refused (status 1 usually means: no timing table for this clock, or unsupported ASIC id)")
        return 1
    print("done. Check memory bandwidth / FPS; `disable` or a reboot restores stock timings.")
    return 0


def cmd_raw(card: Card, st: dict, cmd: int, hexdata: str) -> int:
    ta = need_session(card, st)
    if not ta:
        return 2
    data = bytes.fromhex(hexdata or "")
    status, resp, out, raw = ta.invoke(st["session"], cmd, data)
    print(f"cmd {cmd}: status {status:#x} resp {resp:#x}")
    print("shared buffer:")
    for i in range(0, BUF_LEN, 32):
        print(f"  {i:03x}: {raw[i:i + 32].hex()}")
    print(dmesg_tail(3))
    return 0


def cmd_unload(card: Card, st: dict) -> int:
    ta = need_session(card, st)
    if not ta:
        return 2
    try:
        ta.unload(st["session"])
        print("unloaded")
    except OSError as e:
        print(f"unload failed: {e.strerror or e}")
    st.pop("session", None); st.pop("image", None)
    save_state(st)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="meat3-fasttiming", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("load"); p.add_argument("--image", default="auto", help="auto (try each) or one of " + ",".join(IMAGES))
    p = sub.add_parser("query"); p.add_argument("--unit", type=int, default=CLK_UNIT, help="clock unit multiplier for MHz (default 100 = 10 kHz)")
    p = sub.add_parser("enable"); p.add_argument("--level", type=int, default=1); p.add_argument("--unit", type=int, default=CLK_UNIT)
    p.add_argument("--no-pin", action="store_true", help="do not pin the memory clock while programming")
    p = sub.add_parser("disable"); p.add_argument("--unit", type=int, default=CLK_UNIT); p.add_argument("--no-pin", action="store_true")
    p = sub.add_parser("raw"); p.add_argument("ta_cmd", type=int); p.add_argument("hexdata", nargs="?", default="")
    sub.add_parser("unload")
    a = ap.parse_args(argv)
    if os.geteuid() != 0:
        print("run as root (sudo meat3-fasttiming ...)"); return 2
    card = find_card()
    if card is None:
        print("no RDNA4 (SMU 14) AMD GPU found"); return 2
    st = load_state()
    if a.cmd == "status":
        return cmd_status(card, st)
    if a.cmd == "load":
        return cmd_load(card, st, a.image)
    if a.cmd == "query":
        return cmd_query(card, st, a.unit)
    if a.cmd == "enable":
        return cmd_set(card, st, a.level, a.unit, a.no_pin)
    if a.cmd == "disable":
        return cmd_set(card, st, 0, a.unit, a.no_pin)
    if a.cmd == "raw":
        return cmd_raw(card, st, a.ta_cmd, a.hexdata)
    if a.cmd == "unload":
        return cmd_unload(card, st)
    return 2


if __name__ == "__main__":
    sys.exit(main())
