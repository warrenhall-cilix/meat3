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

PAGE, VOUT_MODE, VOUT_CAL_OFFSET, VOUT_DROOP = 0x00, 0x20, 0x23, 0x28
READ_VIN, READ_IIN, READ_VOUT, READ_IOUT, READ_TEMP1, READ_POUT = 0x88, 0x89, 0x8B, 0x8C, 0x8D, 0x96
PMBUS_REVISION, MFR_ID, MFR_MODEL = 0x98, 0x99, 0x9A

RAILS = {  # (address, page) -> (key, label)
    (0x22, 0): ("vgfx", "VDD_GFX (GPU core)"),
    (0x24, 0): ("vsoc", "VDD_SOC"),
    (0x24, 1): ("vddc_usr", "VDDC_USR"),
    (0x26, 0): ("vmem", "VMEM (memory)"),
    (0x26, 1): ("vddci", "VDDCI (memory I/O)"),
}
ADDRS = sorted({a for a, _ in RAILS})
STATE = "/var/lib/meat3/vrm_factory.json"


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

    def _xfer(self, addr: int, rw: int, cmd: int, size: int, data: Optional[_Data] = None) -> _Data:
        if addr != self.addr:
            fcntl.ioctl(self.fd, I2C_SLAVE, addr)
            self.addr = addr
        d = data or _Data()
        args = _Args(rw, cmd, size, ctypes.pointer(d))
        fcntl.ioctl(self.fd, I2C_SMBUS, args)
        return d

    def read_byte(self, addr, cmd):
        return self._xfer(addr, SMBUS_READ, cmd, SMBUS_BYTE_DATA).byte

    def read_word(self, addr, cmd):
        return self._xfer(addr, SMBUS_READ, cmd, SMBUS_WORD_DATA).word

    def read_block(self, addr, cmd) -> bytes:
        d = self._xfer(addr, SMBUS_READ, cmd, SMBUS_BLOCK_DATA)
        n = min(d.block[0], 32)
        return bytes(d.block[1:1 + n])

    def write_byte(self, addr, cmd, val):
        d = _Data()
        d.byte = val
        self._xfer(addr, SMBUS_WRITE, cmd, SMBUS_BYTE_DATA, d)

    def write_word(self, addr, cmd, val):
        d = _Data()
        d.word = val
        self._xfer(addr, SMBUS_WRITE, cmd, SMBUS_WORD_DATA, d)

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


def vout_exp(mode: int) -> Optional[int]:
    """Exponent for LINEAR16 VOUT values, or None when VOUT_MODE isn't linear (VID/direct)."""
    if mode >> 5 != 0:
        return None
    e = mode & 0x1F
    return e - 32 if e > 15 else e


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


class Controller:
    def __init__(self, bus_no: int, addr: int, mfr: str, model: str, rev: int):
        self.bus_no, self.addr, self.mfr, self.model, self.rev = bus_no, addr, mfr, model, rev
        self.pages = sorted(p for a, p in RAILS if a == addr)
        self.mode: dict[int, int] = {}


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
                self.controllers.append(Controller(bus_no, addr, mfr, model, rev))
        if not self.controllers:
            self.error = "no PMBus VRM controller answered at 0x22/0x24/0x26"
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
                    mode = bus.read_byte(c.addr, VOUT_MODE)
                    c.mode[page] = mode
                    e = vout_exp(mode)
                    vraw = bus.read_word(c.addr, READ_VOUT)
                    r["vout"] = vraw * 2.0 ** e if e is not None else None
                    r["vout_raw"] = vraw
                    r["iout"] = linear11(bus.read_word(c.addr, READ_IOUT))
                    r["temp"] = linear11(bus.read_word(c.addr, READ_TEMP1))
                    r["pout"] = linear11(bus.read_word(c.addr, READ_POUT))
                    if page == 0:
                        r["vin"] = linear11(bus.read_word(c.addr, READ_VIN))
                        r["iin"] = linear11(bus.read_word(c.addr, READ_IIN))
                    off = bus.read_word(c.addr, VOUT_CAL_OFFSET)
                    r["offset_raw"] = off
                    r["offset_mv"] = (off - 0x10000 if off & 0x8000 else off) * 2.0 ** e * 1000 if e is not None else None
                    k = self._key(c, page)
                    if k not in self.factory:
                        self.factory[k] = off
                        self._save_factory()
                    r["factory_raw"] = self.factory[k]
                except OSError as ex:
                    r["error"] = ex.strerror or str(ex)
                out[(c.addr, page)] = r
            try:
                bus.write_byte(c.addr, PAGE, 0)
            except OSError:
                pass
        self.last, self.last_t = out, now
        return out

    # -------------------------------------------------------------- writes
    def can_write(self, addr: int, page: int) -> tuple[bool, str]:
        if not self.allow_writes:
            return False, "VRM writes are disabled (set amdgpu_vrm.allow_writes in /etc/meat3/config.json)"
        c = next((c for c in self.controllers if c.addr == addr), None)
        if c is None or page not in c.pages:
            return False, "controller not found"
        if vout_exp(c.mode.get(page, 0xFF)) is None:
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
        e = vout_exp(c.mode[page])
        raw = int(round(mv / 1000 / 2.0 ** e)) & 0xFFFF
        return self._write_offset(c, page, raw, f"{mv:+.1f} mV")

    def reset(self, addr: int, page: int) -> tuple[bool, str]:
        ok, why = self.can_write(addr, page)
        if not ok:
            return False, why
        c = next(c for c in self.controllers if c.addr == addr)
        raw = self.factory.get(self._key(c, page))
        if raw is None:
            return False, "factory value unknown"
        return self._write_offset(c, page, raw, "factory value")

    def _write_offset(self, c: Controller, page: int, raw: int, what: str) -> tuple[bool, str]:
        bus = self._bus(c.bus_no)
        try:
            bus.write_byte(c.addr, PAGE, page)
            bus.write_word(c.addr, VOUT_CAL_OFFSET, raw)
            back = bus.read_word(c.addr, VOUT_CAL_OFFSET)
            bus.write_byte(c.addr, PAGE, 0)
        except OSError as e:
            return False, f"I2C error: {e.strerror or e}"
        self.last = {}
        if back != raw:
            return False, f"read back 0x{back:04x}, wrote 0x{raw:04x}"
        return True, f"VOUT_CAL_OFFSET 0x{c.addr:02x}/p{page} = {what} (read back OK)"

    def close(self):
        for b in self.buses.values():
            b.close()
