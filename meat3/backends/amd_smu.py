"""ryzen_smu sysfs access + (experimental, gated) SMU mailbox controls for AMD Zen.

Read path (always): codename / version / pm_table_version / pm_table_size / pm_table files.

Mailbox path (only with MEAT3_EXPERIMENTAL_SMU=1): writing `smu_args` (24 bytes = 6 x u32 LE)
then a u32 command id to `rsmu_cmd` / `mp1_smu_cmd` makes the driver run the command
synchronously; reading the command file back returns the status (1 = OK) and `smu_args` the
reply. This module never touches those files unless the env gate is set, and sensors() never
does at all.

Granite Ridge command ids (irusanov/ZenStates-Core Zen4Settings, inherited by Zen5Settings;
MP1 limit ids confirmed by Kyworn/gnr-smu write-back tests on a 9800X3D):
  MP1  0x3E SetPPTLimit (mW)   0x3C SetTDCLimit (mA)   0x3D SetEDCLimit (mA)
  MP1  0x35 SetDldoPsmMargin (per-core CO)   0x36 SetAllDldoPsmMargin (all-core CO)
  RSMU 0xD5 GetDldoPsmMargin (per-core CO read; arg0 = core mask, reply = s16 in arg0[15:0])
Per-core CO argument (ZenStates Cpu.cs / Utils.MakePsmMarginArg, family > 17h):
  arg0 = (ccd << 28) | ((core % 8) << 20) | (margin & 0xFFFF)
where ccd/core are the *physical* CCD and core slot (fused-off slots keep their index).
There is no "get PPT/TDC/EDC" command; current limits are read from the PM table.
"""
from __future__ import annotations

import os
import struct
from typing import Any, Optional

from meat3.core.model import Control
from meat3.backends._cpu_common import read_str

ENV_GATE = "MEAT3_EXPERIMENTAL_SMU"

CODENAMES = ["Undefined", "Colfax", "Renoir", "Picasso", "Matisse", "Threadripper", "CastlePeak", "RavenRidge",
             "RavenRidge2", "SummitRidge", "PinnacleRidge", "Rembrandt", "Vermeer", "VanGogh", "Cezanne", "Milan",
             "Dali", "Lucienne", "Naples", "Chagall", "Raphael", "Phoenix", "StrixPoint", "GraniteRidge",
             "HawkPoint", "StormPeak", "StrixHalo"]

SMU_OK = 0x01
SMU_STATUS = {0x01: "OK", 0xFF: "failed", 0xFE: "unknown command", 0xFD: "rejected (prerequisite)",
              0xFC: "rejected (busy)", 0xFB: "timeout"}

# Command table per codename: {name: (mailbox, id)}
GRANITE_RIDGE_CMDS = {
    "set_ppt": ("mp1", 0x3E), "set_tdc": ("mp1", 0x3C), "set_edc": ("mp1", 0x3D),
    "set_co_core": ("mp1", 0x35), "set_co_all": ("mp1", 0x36),
    "get_co_core": ("rsmu", 0xD5),
}
COMMANDS = {"GraniteRidge": GRANITE_RIDGE_CMDS}

CO_MIN, CO_MAX = -50, 30          # conservative: SMU accepts +-50, BIOS UIs cap positive at +30
LIMITS = {                         # control key -> (pm-table field, cmd, unit, min, max, scale to SMU units)
    "ppt_limit": ("ppt_limit", "set_ppt", "W", 35, 400, 1000),     # the CPU still holds its own 95 °C limit
    "tdc_limit": ("tdc_limit", "set_tdc", "A", 30, 300, 1000),
    "edc_limit": ("edc_limit", "set_edc", "A", 30, 350, 1000),
}


class SmuDisabled(Exception):
    pass


class RyzenSmu:
    def __init__(self, root: str = "/sys/kernel/ryzen_smu_drv", env: Optional[dict] = None):
        self.root = root
        self.env = os.environ if env is None else env
        self.error = ""
        self.co_cache: dict[int, Optional[int]] = {}   # slot -> last read/applied CO margin

    def _p(self, n: str) -> str:
        return os.path.join(self.root, n)

    def present(self) -> bool:
        return os.path.isfile(self._p("pm_table_version")) or os.path.isfile(self._p("version"))

    def codename(self) -> str:
        s = read_str(self._p("codename"))
        try:
            i = int(s)
            return CODENAMES[i] if 0 <= i < len(CODENAMES) else f"#{i}"
        except (TypeError, ValueError):
            return "?"

    def fw_version(self) -> Optional[str]:
        return read_str(self._p("version"))

    def drv_version(self) -> Optional[str]:
        return read_str(self._p("drv_version"))

    def _read_bin(self, n: str) -> Optional[bytes]:
        try:
            with open(self._p(n), "rb") as f:
                return f.read()
        except OSError as e:
            self.error = f"{n}: {e.strerror}"
            return None

    def pm_table_version(self) -> Optional[int]:
        b = self._read_bin("pm_table_version")
        return struct.unpack("<I", b[:4])[0] if b and len(b) >= 4 else None

    def pm_table_size(self) -> Optional[int]:
        b = self._read_bin("pm_table_size")
        if not b:
            return None
        return struct.unpack("<Q", b[:8])[0] if len(b) >= 8 else struct.unpack("<I", b[:4])[0]

    def read_pm_table(self) -> Optional[bytes]:
        return self._read_bin("pm_table")

    # ------------------------------------------------------------ mailbox (gated)
    def experimental(self) -> bool:
        return self.env.get(ENV_GATE) == "1"

    def commands(self) -> dict:
        return COMMANDS.get(self.codename(), {})

    def send(self, name: str, args: list[int]) -> tuple[int, list[int]]:
        """Run one SMU command. Raises SmuDisabled unless MEAT3_EXPERIMENTAL_SMU=1."""
        if not self.experimental():
            raise SmuDisabled(f"SMU mailbox disabled (set {ENV_GATE}=1)")
        cmd = self.commands().get(name)
        if cmd is None:
            raise SmuDisabled(f"command {name} not known for codename {self.codename()}")
        mailbox, cid = cmd
        cmd_file = self._p({"rsmu": "rsmu_cmd", "mp1": "mp1_smu_cmd"}[mailbox])
        if not os.path.exists(cmd_file):
            raise SmuDisabled(f"{os.path.basename(cmd_file)} not exposed by ryzen_smu")
        a = (list(args) + [0] * 6)[:6]
        if any(not (0 <= x <= 0xFFFFFFFF) for x in a):
            raise ValueError("SMU args must be u32")
        with open(self._p("smu_args"), "wb") as f:
            f.write(struct.pack("<6I", *a))
        with open(cmd_file, "wb") as f:
            f.write(struct.pack("<I", cid))
        with open(cmd_file, "rb") as f:
            st = f.read()
        status = struct.unpack("<I", st[:4])[0] if len(st) >= 4 else (st[0] if st else 0)
        with open(self._p("smu_args"), "rb") as f:
            rb = f.read()
        reply = list(struct.unpack("<6I", rb[:24])) if len(rb) >= 24 else []
        return status, reply


# ---------------------------------------------------------------------- encodings

def co_margin_bits(margin: int) -> int:
    """Signed CO margin -> 16-bit two's complement (ZenStates MakePsmMarginArg)."""
    return (margin + (0x100000 if margin < 0 else 0)) & 0xFFFF


def co_core_arg(slot: int, margin: int) -> int:
    """slot = physical core slot over the package (ccd = slot // 8, core = slot % 8)."""
    ccd, core = slot // 8, slot % 8
    return ((ccd << 28) | (core << 20)) & 0xFFF00000 | co_margin_bits(margin)


def co_core_mask(slot: int) -> int:
    return ((slot // 8) << 28) | ((slot % 8) << 20)


def decode_co_reply(arg0: int) -> int:
    v = arg0 & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


# ---------------------------------------------------------------------- controls

def _gate_reason(smu: RyzenSmu) -> str:
    if not smu.present():
        return "ryzen_smu not loaded"
    if not smu.commands():
        return f"no verified SMU command table for codename {smu.codename()}"
    if not smu.experimental():
        return f"experimental SMU writes disabled: start the daemon with {ENV_GATE}=1"
    return ""


def _read_co(smu: RyzenSmu, slot: int) -> Optional[int]:
    """Per-core CO read through RSMU 0xD5 - only when the experimental gate is on."""
    if _gate_reason(smu):
        return None
    if slot in smu.co_cache:
        return smu.co_cache[slot]
    smu.co_cache[slot] = None       # read at most once per process (controls() may be polled)
    try:
        st, reply = smu.send("get_co_core", [co_core_mask(slot)])
    except (OSError, SmuDisabled, ValueError):
        return None
    v = decode_co_reply(reply[0]) if st == SMU_OK and reply else None
    smu.co_cache[slot] = v
    return v


def smu_controls(smu: RyzenSmu, pmt, prefix: str, group: str, ncores: int) -> list[Control]:
    g = f"{group}/PBO & Curve Optimizer"
    reason = _gate_reason(smu)
    avail = not reason
    warn = ("DANGEROUS: raw SMU mailbox write, not persisted across reboot, can crash or damage "
            "the system; no read-back for limits other than the PM table.")
    out = []
    for key, (field, _cmd, unit, lo, hi, _sc) in LIMITS.items():
        cur = pmt.limit(field) if pmt is not None else None
        out.append(Control(f"{prefix}.smu.{key}", g, f"PBO {key.split('_')[0].upper()} limit", "int",
                           None if cur is None else round(cur), min=lo, max=hi, step=1, unit=unit,
                           danger=2, available=avail,
                           hint=(reason + ". " if reason else "") + warn +
                           " Current value read from PM table."))
    slots = (pmt.slots if pmt is not None and pmt.slots else list(range(ncores)))
    per_core = [_read_co(smu, s_) for s_ in slots[:ncores]] if avail else []
    all_val = per_core[0] if per_core and None not in per_core and len(set(per_core)) == 1 else None
    out.append(Control(f"{prefix}.smu.co_all", g, "Curve Optimizer (all cores)", "int", all_val, min=CO_MIN,
                       max=CO_MAX, step=1, unit="steps", default=0, danger=2, available=avail,
                       hint=(reason + ". " if reason else "") + warn +
                       " SetAllDldoPsmMargin; overrides per-core values. Shows a value when all cores match."))
    for k, s in enumerate(slots[:ncores]):
        out.append(Control(f"{prefix}.smu.co_core{k}", g, f"Curve Optimizer core {k}", "int",
                           per_core[k] if k < len(per_core) else _read_co(smu, s), min=CO_MIN, max=CO_MAX, step=1, unit="steps", default=0,
                           danger=2, available=avail,
                           hint=(reason + ". " if reason else "") + warn +
                           f" SetDldoPsmMargin, physical slot {s}; value read via GetDldoPsmMargin (RSMU 0xD5)."))
    return out


def smu_apply(smu: RyzenSmu, pmt, prefix: str, control_id: str, value: Any, ncores: int) -> tuple[bool, str]:
    key = control_id[len(prefix) + len(".smu."):]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        return False, "value must be an integer"
    v = int(value)
    if key in LIMITS:
        _field, cmd, unit, lo, hi, scale = LIMITS[key]
        if not lo <= v <= hi:
            return False, f"{v} {unit} outside {lo}..{hi}"
        args = [v * scale]
    elif key == "co_all":
        if not CO_MIN <= v <= CO_MAX:
            return False, f"{v} outside {CO_MIN}..{CO_MAX}"
        cmd, args = "set_co_all", [co_margin_bits(v)]
    elif key.startswith("co_core") and key[7:].isdigit():
        k = int(key[7:])
        slots = (pmt.slots if pmt is not None and pmt.slots else list(range(ncores)))
        if not 0 <= k < min(ncores, len(slots)):
            return False, f"no core {k}"
        if not CO_MIN <= v <= CO_MAX:
            return False, f"{v} outside {CO_MIN}..{CO_MAX}"
        cmd, args = "set_co_core", [co_core_arg(slots[k], v)]
        slot = slots[k]
    else:
        return False, f"unknown SMU control {control_id}"
    reason = _gate_reason(smu)
    if reason:
        return False, reason
    try:
        st, _ = smu.send(cmd, args)
    except (OSError, SmuDisabled, ValueError) as e:
        return False, f"SMU {cmd} failed: {e}"
    if st != SMU_OK:
        return False, f"SMU {cmd} returned {st:#x} ({SMU_STATUS.get(st, '?')})"
    if cmd == "set_co_core":
        smu.co_cache[slot] = v
    elif cmd == "set_co_all":
        smu.co_cache.clear()
    return True, f"SMU {cmd} arg0={args[0]:#010x} OK"
