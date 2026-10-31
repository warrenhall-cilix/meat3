"""RDNA4 VRM controllers over PMBus (I2C through amdgpu's "AMDGPU SMU" adapters, /dev/i2c-N).

The community-documented RDNA4 layout (overclock.net "Software removing RDNA3 & RDNA4 power limits
and adding VID offsets"): controller 0x22 page 0 = VDD_GFX, 0x24 page 0 = VDD_SOC, 0x24 page 1 =
VDDC_USR, 0x26 page 0 = VMEM, 0x26 page 1 = VDDCI; PMBus VOUT_CAL_OFFSET (0x23) shifts the output.

Reads use standard PMBus telemetry commands. Only the three known addresses are touched, only on
adapters that belong to the GPU and are named "AMDGPU SMU" (never the display DDC buses), and the
only write during monitoring is PAGE (0x00), the standard output selector. VOUT_CAL_OFFSET writes
are disabled unless config amdgpu_vrm.allow_writes is true, clamped to amdgpu_vrm.max_offset_mv,
and only offered when the controller reports linear VOUT_MODE (otherwise the encoding is unknown).
The value found the first time a controller is seen is saved as its factory offset (some boards ship
non-zero, e.g. +5 mV, or -70 mV SoC on a Gigabyte Gaming OC) and "reset" restores that value.

Two more writable settings, behind the same switch:
  * VOUT_DROOP (0x28, load line, mOhm): less droop = less voltage sag under load (LLC).
  * IOUT_CAL_GAIN (0x38, current-sense gain, mOhm): raising it by k makes the controller report
    current / k. The firmware's current limit (TDC, 330 A GFX on every RX 9070 XT) is enforced on that
    reported current, so it effectively becomes 330 A * k. This also scales the controller's own
    over-current protection by k. meat3 multiplies its displayed current and power back by k.
"Real power" = max(12 V input VIN*IIN summed over controllers, corrected rail output power / 0.9):
both are measured at the regulators, so they stay right when the board-power shunts are bypassed.

Vendor profiles. RX 9070 XT boards use MPS MP2686A controllers (SkatterBencher; ElmorLabs EVC2
drives them with its MP2857 profile), which are not plain PMBus:
  * "generic": standard PMBus encodings (e.g. Infineon XDPE).
  * "mps": per the Linux mp2856/mp2857 driver, VIN/IIN/IOUT/TEMP/POUT are LINEAR11, but READ_VOUT is
    vendor-coded (10 bits; 1/256 V or 5 mV VID per MFR_VR_CONFIG2 0x5E bit 11) and VOUT_MODE is not
    meaningful. VOUT_OFFSET (0x23) is an 8-bit signed step count whose step size differs between MPS
    parts (6.25 mV on MP2886A, 5 mV reported on RDNA4), so offset writes need amdgpu_vrm.mps_offset_lsb_mv
    set after verifying it on the card. VOUT_DROOP (0x28) is not the load line on MPS (MFR_IDROOP_CTRL
    0x1E is), so no load-line control. IOUT_CAL_GAIN (0x38) is LINEAR11 as on MP2886A; current-scale
    writes additionally need amdgpu_vrm.experimental_mps_writes.
"""
from __future__ import annotations

import ctypes
import fcntl
import glob
import json
import os
import time
from typing import Optional

I2C_SLAVE = 0x0703
I2C_SMBUS = 0x0720
SMBUS_READ, SMBUS_WRITE = 1, 0
SMBUS_BYTE_DATA, SMBUS_WORD_DATA, SMBUS_BLOCK_DATA = 2, 3, 5

PAGE, VOUT_MODE, VOUT_CAL_OFFSET, VOUT_DROOP, IOUT_CAL_GAIN = 0x00, 0x20, 0x23, 0x28, 0x38
READ_VIN, READ_IIN, READ_VOUT, READ_IOUT, READ_TEMP1, READ_POUT = 0x88, 0x89, 0x8B, 0x8C, 0x8D, 0x96
PMBUS_REVISION, MFR_ID, MFR_MODEL = 0x98, 0x99, 0x9A
MPS_MFR_VR_CONFIG2, MPS_VOUT_LINEAR = 0x5E, 1 << 11
# RDNA4 MP2868A telemetry-gain registers on PMBus page 2 (register map published by fpsflow's GPL-3
# power_limit_removal scripts, which the community uses on RX 9070 cards). The firmware's PPT and TDC
# limits act on the current/power the regulator reports, so these gains are the "shunt mod in software":
#   PG   0x0f bits 10:0  power (TBP) telemetry gain      TBP scale  = stock_pg  / pg
#   CG1  0x08 bits 10:0  current (TDC) telemetry gain    TDC scale  = stock_cg1 / cg1
#   CG1a 0x0c bits 10:4  alternate TDC gain              TDCa scale = stock_cg1a / cg1a
#   DRP  0x06            IDROOP (load-line) gain, low 8 bits
# The scripts keep a stock backup in page-1 registers 0x4d (16 bits) and 0x4b (bits 15:11):
#   block(vr) = (reg4d << 5) | (reg4b >> 11 & 0x1f)   (21 bits per controller)
#   packed    = (block(0x22) << 21) | (block(0x24) & 0x1fffff)
#   fields LSB first: cg1a (7), pg of 0x24 (11), cg1 of 0x22 (11), pg of 0x22 (11), valid flag (1)
# meat3 only READS these (to show and correct the scaling); the writes stay with the scripts.
MPS_PAGE2 = 2
MPS_PG, MPS_CG1, MPS_CG1_ALT, MPS_IDROOP_GAIN = 0x0F, 0x08, 0x0C, 0x06
MPS_BACKUP1, MPS_BACKUP2, MPS_BACKUP3 = 0x4B, 0x4D, 0x10
MPS_VID, MPS_TRIM = 0x23, 0x22
I2C_PEC = 0x0708
# The current fpsflow release (rdna4p) stores a 68-bit backup: 60-bit payload + CRC-8 (poly 0x07 over the
# payload bits, MSB first), split into two 34-bit blocks (vr1 = high 34 bits, vr2 = low 34) and each block
# spread over four register fields: p1 = page1 0x4d [15:0], p2 = page1 0x4b [15:11], p3 = page1 0x10 [7:0],
# p4 = page0 0x4b [15:11]. Payload, MSB first: flag(1) pg1(11) cg1(11) pg2(11) gfx_vid(1) soc_vid(1)
# vddci_vid(1) vram_vid(1) trim(7) cg1_alt(7) droop(8).
RDNA4P_FIELDS = [("pg_vr1", 11), ("cg1_vr1", 11), ("pg_vr2", 11), ("gfx_vid", 1), ("soc_vid", 1),
                 ("vddci_vid", 1), ("vram_vid", 1), ("trim_vr1", 7), ("cg1_alt_vr1", 7), ("droop_vr1", 8)]
HARD_MAX_SCALE = 1.5          # the control range never exceeds this whatever the config says
MP2868A_MODEL_BYTE = 0x68
# AMD's SMU firmware stopped exposing the VR bus on its I2C adapters after PMFW 0x684a00
# (linux-firmware 2025-06-20 / release 20251111); 0x684b00 (2025-11-19) and later answer at no address.
SMU14_I2C_LAST_GOOD = 0x684A00


def crc8_bits(value: int, nbits: int, poly: int = 0x07) -> int:
    """CRC-8 over the low `nbits` bits of `value`, MSB first (fpsflow's crc8)."""
    crc = 0
    for i in range(nbits - 1, -1, -1):
        bit = (value >> i) & 1
        c7 = (crc >> 7) & 1
        crc = (crc << 1) & 0xFF
        if bit ^ c7:
            crc ^= poly
    return crc


def encode_rdna4p_backup(f: dict) -> tuple[int, int]:
    """Pack stock values into the rdna4p on-chip backup: (block_vr1, block_vr2), 34 bits each."""
    packed = 1
    for name, bits in RDNA4P_FIELDS:
        packed = (packed << bits) | (int(f[name]) & ((1 << bits) - 1))
    final = (packed << 8) | crc8_bits(packed, 60)
    return final >> 34 & ((1 << 34) - 1), final & ((1 << 34) - 1)


def decode_rdna4p_backup(block_vr1: int, block_vr2: int) -> Optional[dict]:
    """Stock values from the rdna4p backup, or None when there is no valid (flagged, CRC-correct) backup."""
    final = ((block_vr1 & ((1 << 34) - 1)) << 34) | (block_vr2 & ((1 << 34) - 1))
    payload, crc = final >> 8, final & 0xFF
    if crc8_bits(payload, 60) != crc or not (payload >> 59) & 1:
        return None
    out: dict = {}
    for name, bits in reversed(RDNA4P_FIELDS):
        out[name] = payload & ((1 << bits) - 1)
        payload >>= bits
    return out


def split_vr_block(block: int) -> tuple[int, int, int, int]:
    """34-bit block -> (p1 16 bits, p2 5 bits, p3 8 bits, p4 5 bits) register fields."""
    return block >> 18 & 0xFFFF, block >> 13 & 0x1F, block >> 5 & 0xFF, block & 0x1F


def join_vr_block(p1: int, p2: int, p3: int, p4: int) -> int:
    return (p1 & 0xFFFF) << 18 | (p2 & 0x1F) << 13 | (p3 & 0xFF) << 5 | (p4 & 0x1F)


def decode_rdna4_backup(block_vr1: int, block_vr2: int) -> Optional[dict]:
    """Stock gains from the two 21-bit backup blocks, or None when the valid flag is clear."""
    data = ((block_vr1 & 0x1FFFFF) << 21) | (block_vr2 & 0x1FFFFF)
    cg1a = data & 0x7F; data >>= 7
    pg2 = data & 0x7FF; data >>= 11
    cg1 = data & 0x7FF; data >>= 11
    pg1 = data & 0x7FF; data >>= 11
    if not data & 1:
        return None
    return {"pg_vr1": pg1, "cg1_vr1": cg1, "pg_vr2": pg2, "cg1_alt_vr1": cg1a}

RAILS = {  # (address, page) -> (key, label)
    (0x22, 0): ("vgfx", "VDD_GFX (GPU core)"),
    (0x24, 0): ("vsoc", "VDD_SOC"),
    (0x24, 1): ("vddc_usr", "VDDC_USR"),
    (0x26, 0): ("vmem", "VMEM (memory)"),
    (0x26, 1): ("vddci", "VDDCI (memory I/O)"),
}
ADDRS = sorted({a for a, _ in RAILS})
STATE = "/var/lib/meat3/vrm_factory.json"
VRM_EFFICIENCY = 0.90         # output -> input estimate when VIN/IIN aren't usable


class _Data(ctypes.Union):
    _fields_ = [("byte", ctypes.c_uint8), ("word", ctypes.c_uint16), ("block", ctypes.c_uint8 * 34)]


class _Args(ctypes.Structure):
    _fields_ = [("read_write", ctypes.c_uint8), ("command", ctypes.c_uint8), ("size", ctypes.c_uint32),
                ("data", ctypes.POINTER(_Data))]


class SMBus:
    """Minimal SMBus over /dev/i2c-N (i2c-dev)."""

    def __init__(self, bus: int):
        self.fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        self.addr = None
        self.pec = False

    def _xfer(self, addr: int, rw: int, cmd: int, size: int, data: Optional[_Data] = None,
              pec: bool = False) -> _Data:
        if addr != self.addr:
            fcntl.ioctl(self.fd, I2C_SLAVE, addr)
            self.addr = addr
        if pec != self.pec:                       # packet error checking, as the fpsflow scripts use (i2cget/set 'wp')
            fcntl.ioctl(self.fd, I2C_PEC, 1 if pec else 0)
            self.pec = pec
        d = data or _Data()
        args = _Args(rw, cmd, size, ctypes.pointer(d))
        fcntl.ioctl(self.fd, I2C_SMBUS, args)
        return d

    def read_byte(self, addr, cmd):
        return self._xfer(addr, SMBUS_READ, cmd, SMBUS_BYTE_DATA).byte

    def read_word(self, addr, cmd, pec: bool = False):
        return self._xfer(addr, SMBUS_READ, cmd, SMBUS_WORD_DATA, pec=pec).word

    def read_block(self, addr, cmd) -> bytes:
        d = self._xfer(addr, SMBUS_READ, cmd, SMBUS_BLOCK_DATA)
        n = min(d.block[0], 32)
        return bytes(d.block[1:1 + n])

    def write_byte(self, addr, cmd, val):
        d = _Data()
        d.byte = val
        self._xfer(addr, SMBUS_WRITE, cmd, SMBUS_BYTE_DATA, d)

    def write_word(self, addr, cmd, val, pec: bool = False):
        d = _Data()
        d.word = val
        self._xfer(addr, SMBUS_WRITE, cmd, SMBUS_WORD_DATA, d, pec=pec)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


def linear11(raw: int) -> float:
    exp = (raw >> 11) & 0x1F
    mant = raw & 0x7FF
    if exp > 15:
        exp -= 32
    if mant > 1023:
        mant -= 2048
    return mant * 2.0 ** exp


def to_linear11(x: float, exp_hint: int = 0) -> int:
    """Encode in LINEAR11, keeping the controller's exponent when the mantissa fits."""
    exp = exp_hint
    while abs(x / 2.0 ** exp) > 1023 and exp < 15:
        exp += 1
    return ((exp & 0x1F) << 11) | (int(round(x / 2.0 ** exp)) & 0x7FF)


def l11_exp(raw: int) -> int:
    e = (raw >> 11) & 0x1F
    return e - 32 if e > 15 else e


def vout_exp(mode: int) -> Optional[int]:
    """Exponent for LINEAR16 VOUT values, or None when VOUT_MODE isn't linear (VID/direct)."""
    if mode >> 5 != 0:
        return None
    e = mode & 0x1F
    return e - 32 if e > 15 else e


def shunt_bypassed(board_w: Optional[float], real_w: Optional[float]) -> bool:
    """Board power far below what the regulators deliver = shunt sense shorted / bypassed."""
    return board_w is not None and real_w is not None and real_w > 100 and board_w < 0.25 * real_w


def find_adapters(pci_dir: str) -> list[tuple[int, str]]:
    """(bus number, name) of the GPU's own SMU I2C adapters."""
    out = []
    for d in sorted(glob.glob(os.path.join(pci_dir, "i2c-*"))):
        try:
            with open(os.path.join(d, "name")) as f:
                name = f.read().strip()
            bus = int(os.path.basename(d)[4:])
        except (OSError, ValueError):
            continue
        if "SMU" in name.upper():
            out.append((bus, name))
    return out


def detect_profile(mfr: str, model: str) -> str:
    m = f"{mfr} {model}".upper()
    return "mps" if ("MPS" in m or model.upper().startswith("MP2")) else "generic"


def mps_vout(raw: int, cfg2: Optional[int]) -> Optional[float]:
    """READ_VOUT on MPS multiphase controllers (Linux mp2856.c): 10-bit, linear 1/256 V or VR13 VID."""
    v = raw & 0x3FF
    if cfg2 is None:
        return None
    if cfg2 & MPS_VOUT_LINEAR:
        return v / 256.0
    return (v + 49) * 0.005 if v else 0.0


class Controller:
    def __init__(self, bus_no: int, addr: int, mfr: str, model: str, rev: int, profile: str = "generic"):
        self.bus_no, self.addr, self.mfr, self.model, self.rev = bus_no, addr, mfr, model, rev
        self.profile = profile
        self.pages = sorted(p for a, p in RAILS if a == addr)
        self.mode: dict[int, int] = {}
        self.mp2868a = False          # RDNA4 controller: page-2 gain registers are meaningful


class VrmAccess:
    """Detects the GPU's VRM controllers and reads/writes them. `opener(bus) -> SMBus-like` for tests."""

    def __init__(self, pci_dir: str, serial: str = "", opener=None, state_path: str = STATE,
                 cfg: Optional[dict] = None):
        self.pci_dir = pci_dir
        self.serial = serial or os.path.basename(os.path.realpath(pci_dir))
        self.opener = opener or SMBus
        self.state_path = state_path
        cfg = cfg or {}
        self.allow_writes = bool(cfg.get("allow_writes", False))
        self.max_offset_mv = float(cfg.get("max_offset_mv", 50))
        self.max_current_scale = float(cfg.get("max_current_scale", 1.5))
        self.profile_override = cfg.get("profile", "auto")
        self.mps_lsb_mv = cfg.get("mps_offset_lsb_mv")            # None until verified on the card
        self.mps_experimental = bool(cfg.get("experimental_mps_writes", False))
        self.max_tbp_scale = min(HARD_MAX_SCALE, float(cfg.get("max_tbp_scale", 1.3)))
        self.max_tdc_scale = min(HARD_MAX_SCALE, float(cfg.get("max_tdc_scale", 1.3)))
        self.restore_on_stop = bool(cfg.get("restore_on_stop", True))
        self.gains_touched = False    # meat3 changed the RDNA4 gains in this daemon's lifetime
        self.buses: dict[int, object] = {}
        self.controllers: list[Controller] = []
        self.error: Optional[str] = None
        self.factory: dict[str, int] = {}
        self.last: dict = {}
        self.last_t = 0.0
        self._detect()

    # -------------------------------------------------------------- setup
    def _bus(self, n):
        b = self.buses.get(n)
        if b is None:
            b = self.buses[n] = self.opener(n)
        return b

    def _detect(self):
        adapters = find_adapters(self.pci_dir)
        if not adapters:
            self.error = "no AMDGPU SMU I2C adapter for this GPU (needs amdgpu with SMU I2C)"
            return
        if self.opener is SMBus and not os.path.exists(f"/dev/i2c-{adapters[0][0]}"):
            self.error = "i2c-dev not loaded (modprobe i2c-dev)"
            return
        for bus_no, _name in adapters:
            try:
                bus = self._bus(bus_no)
            except OSError as e:
                self.error = f"/dev/i2c-{bus_no}: {e.strerror}"
                continue
            for addr in ADDRS:
                try:
                    rev = bus.read_byte(addr, PMBUS_REVISION)
                except OSError:
                    continue              # nothing (NAK) at this address on this bus
                mfr = model = ""
                try:
                    mfr = bus.read_block(addr, MFR_ID).decode(errors="replace").strip("\x00 ")
                    model = bus.read_block(addr, MFR_MODEL).decode(errors="replace").strip("\x00 ")
                except OSError:
                    pass
                prof = self.profile_override if self.profile_override in ("mps", "generic") else \
                    detect_profile(mfr, model)
                c = Controller(bus_no, addr, mfr, model, rev, prof)
                try:                      # MP2868A reports 0x68 in a byte of MFR_MODEL read as a word
                    w = bus.read_word(addr, MFR_MODEL)
                    c.mp2868a = addr in (0x22, 0x24) and MP2868A_MODEL_BYTE in (w & 0xFF, w >> 8 & 0xFF)
                except OSError:
                    pass
                if c.mp2868a and prof == "generic":
                    c.profile = "mps"
                self.controllers.append(c)
        if not self.controllers:
            # Real ASUS RX 9070 XT (kernel 7.2): a full read scan of both "AMDGPU SMU" buses found no
            # device at any address, so on RDNA4 the regulators look reachable only by the GPU firmware.
            self.error = ("no PMBus VRM controller answered at 0x22/0x24/0x26. On a real RX 9070 XT the GPU's "
                          "SMU I2C buses answer at no address at all, so RDNA4 regulators appear to be "
                          "reachable only by the GPU firmware. VRM telemetry and offsets are unavailable; "
                          "the driver's own voltage offset (Overclock tab, meat3-gputune) still works")
        self._load_factory()

    def _key(self, c: Controller, page: int) -> str:
        return f"{self.serial}/{c.addr:02x}/{page}"

    def _load_factory(self):
        try:
            with open(self.state_path) as f:
                self.factory = json.load(f)
        except (OSError, ValueError):
            self.factory = {}

    def _save_factory(self):
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.factory, f, indent=1)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    # -------------------------------------------------------------- telemetry
    # ------------------------------------------------------------ RDNA4 (MP2868A) telemetry gains
    def _rdna4_pair(self):
        c1 = next((c for c in self.controllers if c.mp2868a and c.addr == 0x22), None)
        c2 = next((c for c in self.controllers if c.mp2868a and c.addr == 0x24), None)
        return c1, c2

    def _rd(self, c: Controller, page: int, cmd: int) -> int:
        bus = self._bus(c.bus_no)
        bus.write_byte(c.addr, PAGE, page)
        try:
            return bus.read_word(c.addr, cmd, pec=True)
        finally:
            bus.write_byte(c.addr, PAGE, 0)

    def _wr(self, c: Controller, page: int, cmd: int, val: int, what: str) -> None:
        """Write a word with PEC and verify the read-back (raises OSError on mismatch)."""
        bus = self._bus(c.bus_no)
        bus.write_byte(c.addr, PAGE, page)
        try:
            bus.write_word(c.addr, cmd, val & 0xFFFF, pec=True)
            back = bus.read_word(c.addr, cmd, pec=True)
        finally:
            bus.write_byte(c.addr, PAGE, 0)
        if back != val & 0xFFFF:
            raise OSError(0, f"{what}: read back 0x{back:04x}, wrote 0x{val & 0xFFFF:04x} (write-protected?)")
        self.last = {}

    def _rd_vr_block(self, c: Controller) -> int:
        p1 = self._rd(c, 1, MPS_BACKUP2)
        p2 = self._rd(c, 1, MPS_BACKUP1) >> 11 & 0x1F
        p3 = self._rd(c, 1, MPS_BACKUP3) & 0xFF
        p4 = self._rd(c, 0, MPS_BACKUP1) >> 11 & 0x1F
        return join_vr_block(p1, p2, p3, p4)

    def _wr_vr_block(self, c: Controller, block: int) -> None:
        p1, p2, p3, p4 = split_vr_block(block)
        self._wr(c, 1, MPS_BACKUP2, p1, "backup2")
        r = self._rd(c, 1, MPS_BACKUP1) & ~(0x1F << 11)
        self._wr(c, 1, MPS_BACKUP1, r | p2 << 11, "backup1 p1")
        r = self._rd(c, 1, MPS_BACKUP3) & ~0xFF
        self._wr(c, 1, MPS_BACKUP3, r | p3, "backup3")
        r = self._rd(c, 0, MPS_BACKUP1) & ~(0x1F << 11)
        self._wr(c, 0, MPS_BACKUP1, r | p4 << 11, "backup1 p0")

    def rdna4_gains(self) -> Optional[dict]:
        """RDNA4 (MP2868A) telemetry gains: current page-2 values, the stock values from the on-chip backup
        (rdna4p 68-bit format first, then the older 21-bit one) or from meat3's first-seen record, and the
        resulting multipliers (stock / current). PAGE is restored to 0."""
        c1, c2 = self._rdna4_pair()
        if c1 is None:
            return None
        out: dict = {}
        try:
            out["pg_vr1_raw"] = self._rd(c1, MPS_PAGE2, MPS_PG)
            out["cg1_vr1_raw"] = self._rd(c1, MPS_PAGE2, MPS_CG1)
            out["cg1_alt_vr1_raw"] = self._rd(c1, MPS_PAGE2, MPS_CG1_ALT)
            out["idroop_raw"] = self._rd(c1, MPS_PAGE2, MPS_IDROOP_GAIN)
            out["legacy_power_offset"] = bool(self._rd(c1, 1, MPS_BACKUP3) >> 8 & 0xFF)
            blk1 = self._rd_vr_block(c1)
            blk2 = 0
            if c2 is not None:
                out["pg_vr2_raw"] = self._rd(c2, MPS_PAGE2, MPS_PG)
                out["legacy_power_offset"] |= bool(self._rd(c2, 1, MPS_BACKUP3) >> 8 & 0xFF)
                blk2 = self._rd_vr_block(c2)
        except OSError as e:
            for c in (c1, c2):
                if c is not None:
                    try:
                        self._bus(c.bus_no).write_byte(c.addr, PAGE, 0)
                    except OSError:
                        pass
            out["error"] = e.strerror or str(e)
            return out
        out["pg_vr1"] = out["pg_vr1_raw"] & 0x7FF
        out["cg1_vr1"] = out["cg1_vr1_raw"] & 0x7FF
        out["cg1_alt_vr1"] = out["cg1_alt_vr1_raw"] >> 4 & 0x7F
        if "pg_vr2_raw" in out:
            out["pg_vr2"] = out["pg_vr2_raw"] & 0x7FF
        stock = decode_rdna4p_backup(blk1, blk2)
        src = "scripts backup (rdna4p)"
        if stock is None:
            # older scripts: 21-bit blocks in page-1 0x4d/0x4b only
            old1 = (blk1 >> 18 & 0xFFFF) << 5 | (blk1 >> 13 & 0x1F)
            old2 = (blk2 >> 18 & 0xFFFF) << 5 | (blk2 >> 13 & 0x1F)
            stock = decode_rdna4_backup(old1, old2)
            src = "scripts backup (legacy)"
        out["stock"] = stock
        if stock is None:
            stock = {k: self._factory(c1, MPS_PAGE2, "/" + k, out[k]) for k in ("pg_vr1", "cg1_vr1", "cg1_alt_vr1")}
            if "pg_vr2" in out:
                stock["pg_vr2"] = self._factory(c2, MPS_PAGE2, "/pg_vr2", out["pg_vr2"])
            src = "meat3 first-seen"
        else:
            for k in ("pg_vr1", "cg1_vr1", "cg1_alt_vr1", "pg_vr2"):
                if k in stock:                      # keep meat3's copy in step with the chip's backup
                    self.factory[self._key(c1 if k != "pg_vr2" else c2, MPS_PAGE2) + "/" + k] = stock[k]
            self._save_factory()
        out["stock_source"] = src
        out["stock_values"] = {k: stock.get(k) for k in ("pg_vr1", "cg1_vr1", "cg1_alt_vr1", "pg_vr2")}
        div = lambda s_, cur: (s_ / cur) if s_ and cur else None
        out["tbp_scale"] = div(stock.get("pg_vr1"), out["pg_vr1"])
        out["tdc_scale"] = div(stock.get("cg1_vr1"), out["cg1_vr1"])
        out["tdc_alt_scale"] = div(stock.get("cg1_alt_vr1"), out["cg1_alt_vr1"])
        return out

    def can_scale(self) -> tuple[bool, str]:
        if not self.allow_writes:
            return False, "VRM writes are disabled (set amdgpu_vrm.allow_writes in /etc/meat3/config.json)"
        c1, c2 = self._rdna4_pair()
        if c1 is None or c2 is None:
            return False, "no MP2868A pair (0x22 + 0x24) on the SMU bus"
        return True, ""

    def _ensure_rdna4_backup(self, c1: Controller, c2: Controller, g: dict) -> dict:
        """Make sure the chip carries a valid rdna4p backup of the stock values; returns the stock dict."""
        if g.get("stock") is not None and g.get("stock_source", "").endswith("(rdna4p)"):
            return g["stock"]
        st = g["stock_values"]
        if any(st.get(k) is None for k in ("pg_vr1", "cg1_vr1", "cg1_alt_vr1", "pg_vr2")):
            raise OSError(0, "stock gains unknown; cannot create a backup")
        fields = dict(st)
        fields["gfx_vid"] = self._rd(c1, 0, MPS_VID) & 1
        fields["soc_vid"] = self._rd(c2, 0, MPS_VID) & 1
        fields["vddci_vid"] = self._rd(c1, 1, MPS_VID) & 1
        fields["vram_vid"] = self._rd(c2, 1, MPS_VID) & 1
        fields["trim_vr1"] = self._rd(c1, 0, MPS_TRIM) & 0x7F
        fields["droop_vr1"] = g["idroop_raw"] & 0xFF
        b1, b2 = encode_rdna4p_backup(fields)
        self._wr_vr_block(c1, b1)
        self._wr_vr_block(c2, b2)
        if decode_rdna4p_backup(self._rd_vr_block(c1), self._rd_vr_block(c2)) is None:
            raise OSError(0, "backup read-back failed CRC check")
        return fields

    def set_rdna4_scale(self, kind: str, k: float) -> tuple[bool, str]:
        """kind 'tbp': PG on both controllers = stock / k (firmware power limit becomes PPT x k).
        kind 'tdc': CG1 and CG1_alt on the GFX controller = stock / k (TDC limit becomes 330 A x k).
        The gains are telemetry scalers: the SMU and the driver then see real / k."""
        ok, why = self.can_scale()
        if not ok:
            return False, why
        cap = self.max_tbp_scale if kind == "tbp" else self.max_tdc_scale
        if kind not in ("tbp", "tdc"):
            return False, f"unknown scale {kind!r}"
        if not 1.0 <= float(k) <= cap:
            return False, f"scale must be 1.0..{cap:g} (amdgpu_vrm.max_{kind}_scale, hard cap {HARD_MAX_SCALE:g})"
        c1, c2 = self._rdna4_pair()
        g = self.rdna4_gains()
        if not g or "error" in g:
            return False, f"cannot read gains: {(g or {}).get('error', 'no controller')}"
        if g.get("legacy_power_offset"):
            return False, ("an older fpsflow script left a power offset in page-1 register 0x10; remove it with "
                           "that script and power-cycle before scaling")
        try:
            stock = self._ensure_rdna4_backup(c1, c2, g)
            writes = []
            if kind == "tbp":
                for c, key, raw in ((c1, "pg_vr1", g["pg_vr1_raw"]), (c2, "pg_vr2", g["pg_vr2_raw"])):
                    val = (raw & ~0x7FF) | min(0x7FF, int(stock[key] / k))
                    self._wr(c, MPS_PAGE2, MPS_PG, val, f"PG 0x{c.addr:02x}")
                    writes.append(f"PG 0x{c.addr:02x}=0x{val & 0x7FF:03x}")
            else:
                val = (g["cg1_vr1_raw"] & ~0x7FF) | min(0x7FF, int(stock["cg1_vr1"] / k))
                self._wr(c1, MPS_PAGE2, MPS_CG1, val, "CG1")
                writes.append(f"CG1=0x{val & 0x7FF:03x}")
                alt = (g["cg1_alt_vr1_raw"] & ~0x7F0) | (min(0x7F, int(stock["cg1_alt_vr1"] / k)) << 4)
                self._wr(c1, MPS_PAGE2, MPS_CG1_ALT, alt, "CG1 alt")
                writes.append(f"CG1alt=0x{alt >> 4 & 0x7F:02x}")
        except OSError as e:
            return False, f"I2C error: {e.strerror or e}"
        self.gains_touched = True
        self.last = {}
        return True, f"{kind.upper()} telemetry scale x{k:.2f} (stock/{k:.2f}): " + ", ".join(writes) + " (read back OK)"

    def reset_rdna4_gains(self) -> tuple[bool, str]:
        ok, why = self.can_scale()
        if not ok:
            return False, why
        c1, c2 = self._rdna4_pair()
        g = self.rdna4_gains()
        if not g or "error" in g:
            return False, f"cannot read gains: {(g or {}).get('error', 'no controller')}"
        st = g["stock_values"]
        try:
            self._wr(c1, MPS_PAGE2, MPS_PG, (g["pg_vr1_raw"] & ~0x7FF) | st["pg_vr1"], "PG vr1")
            self._wr(c1, MPS_PAGE2, MPS_CG1, (g["cg1_vr1_raw"] & ~0x7FF) | st["cg1_vr1"], "CG1")
            self._wr(c1, MPS_PAGE2, MPS_CG1_ALT, (g["cg1_alt_vr1_raw"] & ~0x7F0) | st["cg1_alt_vr1"] << 4, "CG1 alt")
            if "pg_vr2_raw" in g and st.get("pg_vr2") is not None:
                self._wr(c2, MPS_PAGE2, MPS_PG, (g["pg_vr2_raw"] & ~0x7FF) | st["pg_vr2"], "PG vr2")
        except OSError as e:
            return False, f"I2C error: {e.strerror or e}"
        self.gains_touched = False
        self.last = {}
        return True, f"TBP/TDC telemetry gains back to stock ({g['stock_source']})"

    def read_all(self, min_interval: float = 2.0) -> dict:
        """{(addr, page): {...}} cached for min_interval seconds (each read is an SMU I2C transfer)."""
        now = time.monotonic()
        if self.last and now - self.last_t < min_interval:
            return self.last
        out = {}
        for c in self.controllers:
            bus = self._bus(c.bus_no)
            for page in c.pages:
                r: dict = {}
                try:
                    bus.write_byte(c.addr, PAGE, page)
                    vraw = bus.read_word(c.addr, READ_VOUT)
                    r["vout_raw"] = vraw
                    if c.profile == "mps":
                        try:
                            cfg2 = bus.read_word(c.addr, MPS_MFR_VR_CONFIG2)
                        except OSError:
                            cfg2 = None
                        r["vout"] = mps_vout(vraw, cfg2)
                        e = None
                        c.mode[page] = 0xFF            # VOUT_MODE not meaningful on MPS
                    else:
                        mode = bus.read_byte(c.addr, VOUT_MODE)
                        c.mode[page] = mode
                        e = vout_exp(mode)
                        r["vout"] = vraw * 2.0 ** e if e is not None else None
                    r["iout"] = linear11(bus.read_word(c.addr, READ_IOUT))
                    r["temp"] = linear11(bus.read_word(c.addr, READ_TEMP1))
                    r["pout"] = linear11(bus.read_word(c.addr, READ_POUT))
                    if page == 0:
                        r["vin"] = linear11(bus.read_word(c.addr, READ_VIN))
                        r["iin"] = linear11(bus.read_word(c.addr, READ_IIN))
                    off = bus.read_word(c.addr, VOUT_CAL_OFFSET)
                    r["offset_raw"] = off
                    if c.profile == "mps":
                        steps = (off & 0xFF) - 0x100 if off & 0x80 else off & 0xFF
                        r["offset_steps"] = steps
                        r["offset_mv"] = steps * float(self.mps_lsb_mv) if self.mps_lsb_mv else None
                    else:
                        r["offset_mv"] = (off - 0x10000 if off & 0x8000 else off) * 2.0 ** e * 1000 if e is not None else None
                    r["factory_raw"] = self._factory(c, page, "", off)
                    extra = ((IOUT_CAL_GAIN, "gain"),) if c.profile == "mps" else \
                        ((IOUT_CAL_GAIN, "gain"), (VOUT_DROOP, "droop"))
                    for cmd, name in extra:
                        try:
                            raw = bus.read_word(c.addr, cmd)
                        except OSError:
                            continue              # optional command not implemented
                        r[name + "_raw"] = raw
                        r[name + "_mohm"] = linear11(raw)
                        r[name + "_factory_raw"] = self._factory(c, page, "/" + name, raw)
                    fg = linear11(r["gain_factory_raw"]) if "gain_factory_raw" in r else 0
                    r["scale"] = r["gain_mohm"] / fg if fg > 0 and r.get("gain_mohm", 0) > 0 else 1.0
                    r["iout_real"] = r["iout"] * r["scale"]
                    r["pout_real"] = r["pout"] * r["scale"]
                except OSError as ex:
                    r["error"] = ex.strerror or str(ex)
                out[(c.addr, page)] = r
            try:
                bus.write_byte(c.addr, PAGE, 0)
            except OSError:
                pass
        self.last, self.last_t = out, now
        return out

    def _factory(self, c: Controller, page: int, suffix: str, raw: int) -> int:
        k = self._key(c, page) + suffix
        if k not in self.factory:
            self.factory[k] = raw
            self._save_factory()
        return self.factory[k]

    @staticmethod
    def power_summary(data: dict) -> dict:
        """Real power from regulator measurements (independent of the board-power shunts)."""
        rails = [r.get("pout_real") for r in data.values() if "error" not in r]
        rails_w = sum(x for x in rails if x is not None) if rails else None
        ins = [(r.get("vin"), r.get("iin")) for (a, p), r in data.items() if p == 0 and "error" not in r]
        in_w = sum(v * i for v, i in ins) if ins and all(v and i is not None for v, i in ins) else None
        cand = [x for x in (in_w, rails_w / VRM_EFFICIENCY if rails_w else None) if x is not None]
        return {"input_w": in_w, "rails_w": rails_w, "real_w": max(cand) if cand else None}

    # -------------------------------------------------------------- writes
    def can_write(self, addr: int, page: int, need_linear_vout: bool = True, what: str = "offset") -> tuple[bool, str]:
        if not self.allow_writes:
            return False, "VRM writes are disabled (set amdgpu_vrm.allow_writes in /etc/meat3/config.json)"
        c = next((c for c in self.controllers if c.addr == addr), None)
        if c is None or page not in c.pages:
            return False, "controller not found"
        if c.profile == "mps":
            if what == "offset":
                if not self.mps_lsb_mv:
                    return False, ("MPS controller: VOUT_OFFSET step size not verified on this card "
                                   "(set amdgpu_vrm.mps_offset_lsb_mv once confirmed, e.g. 5 or 6.25)")
                return True, ""
            if what == "llc":
                return False, "MPS controller: load line is a vendor register (MFR_IDROOP_CTRL), not supported"
            if not self.mps_experimental:
                return False, ("MPS controller: current-sense gain writes need "
                               "amdgpu_vrm.experimental_mps_writes (encoding verified only on MP2886A)")
            return True, ""
        if need_linear_vout and vout_exp(c.mode.get(page, 0xFF)) is None:
            return False, (f"controller uses non-linear VOUT_MODE 0x{c.mode.get(page, 0):02x}; "
                           "offset encoding unknown, refusing to write")
        return True, ""

    def set_offset_mv(self, addr: int, page: int, mv: float) -> tuple[bool, str]:
        ok, why = self.can_write(addr, page)
        if not ok:
            return False, why
        if abs(mv) > self.max_offset_mv:
            return False, f"|offset| > amdgpu_vrm.max_offset_mv ({self.max_offset_mv:g} mV)"
        c = next(c for c in self.controllers if c.addr == addr)
        if c.profile == "mps":
            lsb = float(self.mps_lsb_mv)
            steps = int(round(mv / lsb))
            if not -128 <= steps <= 127:
                return False, "offset out of the 8-bit range"
            raw = (self.factory.get(self._key(c, page), 0) & 0xFF00) | (steps & 0xFF)
            return self._write_offset(c, page, raw, f"{steps:+d} steps x {lsb:g} mV")
        e = vout_exp(c.mode[page])
        raw = int(round(mv / 1000 / 2.0 ** e)) & 0xFFFF
        return self._write_offset(c, page, raw, f"{mv:+.1f} mV")

    def set_current_scale(self, addr: int, page: int, k: float) -> tuple[bool, str]:
        """Under-report current by k (raises the firmware's effective current limit k times)."""
        ok, why = self.can_write(addr, page, need_linear_vout=False, what="gain")
        if not ok:
            return False, why
        if not 1.0 <= k <= self.max_current_scale:
            return False, f"scale must be 1.0..{self.max_current_scale:g} (amdgpu_vrm.max_current_scale)"
        c = next(c for c in self.controllers if c.addr == addr)
        fac = self.factory.get(self._key(c, page) + "/gain")
        if fac is None or linear11(fac) <= 0:
            return False, "factory IOUT_CAL_GAIN unknown (controller doesn't report it)"
        raw = fac if k == 1.0 else to_linear11(linear11(fac) * k, l11_exp(fac))
        return self._write_word(c, page, IOUT_CAL_GAIN, raw, f"IOUT_CAL_GAIN x{k:.2f}")

    def set_droop_mohm(self, addr: int, page: int, mohm: float) -> tuple[bool, str]:
        ok, why = self.can_write(addr, page, need_linear_vout=False, what="llc")
        if not ok:
            return False, why
        c = next(c for c in self.controllers if c.addr == addr)
        fac = self.factory.get(self._key(c, page) + "/droop")
        if fac is None:
            return False, "factory VOUT_DROOP unknown (controller doesn't report it)"
        if not 0 <= mohm <= 2 * linear11(fac) + 1e-9:
            return False, f"load line must be 0..{2 * linear11(fac):.3f} mOhm (2x factory)"
        return self._write_word(c, page, VOUT_DROOP, to_linear11(mohm, l11_exp(fac)), f"VOUT_DROOP {mohm:.3f} mOhm")

    def reset(self, addr: int, page: int) -> tuple[bool, str]:
        """Restore factory offset, current-sense gain and load line (whatever was ever read)."""
        if not self.allow_writes:
            return False, "VRM writes are disabled (set amdgpu_vrm.allow_writes in /etc/meat3/config.json)"
        c = next((c for c in self.controllers if c.addr == addr), None)
        if c is None or page not in c.pages:
            return False, "controller not found"
        msgs = []
        for suffix, cmd in (("", VOUT_CAL_OFFSET), ("/gain", IOUT_CAL_GAIN), ("/droop", VOUT_DROOP)):
            raw = self.factory.get(self._key(c, page) + suffix)
            if raw is None:
                continue
            ok, msg = self._write_word(c, page, cmd, raw, "factory")
            if not ok:
                return False, msg
            msgs.append(msg)
        return (True, "; ".join(msgs)) if msgs else (False, "factory values unknown")

    def _write_offset(self, c: Controller, page: int, raw: int, what: str) -> tuple[bool, str]:
        return self._write_word(c, page, VOUT_CAL_OFFSET, raw, f"VOUT_CAL_OFFSET {what}")

    def _write_word(self, c: Controller, page: int, cmd: int, raw: int, what: str) -> tuple[bool, str]:
        bus = self._bus(c.bus_no)
        try:
            bus.write_byte(c.addr, PAGE, page)
            bus.write_word(c.addr, cmd, raw)
            back = bus.read_word(c.addr, cmd)
            bus.write_byte(c.addr, PAGE, 0)
        except OSError as e:
            return False, f"I2C error: {e.strerror or e}"
        self.last = {}
        if back != raw:
            return False, f"read back 0x{back:04x}, wrote 0x{raw:04x} (write-protected?)"
        return True, f"0x{c.addr:02x}/p{page} {what} (read back OK)"

    def close(self):
        if self.gains_touched and self.restore_on_stop:
            try:
                self.reset_rdna4_gains()        # never leave scaled gains behind when meat3d stops
            except Exception:
                pass
        for b in self.buses.values():
            b.close()
