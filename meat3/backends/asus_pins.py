"""ASUS ROG Astral 12V-2x6 per-pin monitor (ITE IT8915FN @ 0x2B on 'NVIDIA i2c adapter 1').

Read-only SMBus byte-data reads (the chip doesn't support block reads):
  0x80..0x97 : 6 rails x (mV BE16, mA BE16); rail 0 = pin 6 ... rail 5 = pin 1
  0x98..0x9B, 0xA0..0xA3 : two extra 12 V input channels (mV, mA) - likely PCIe slot / aux
These come from ASUS's own sense resistors, so they are NOT affected by board shunt mods.
Supported boards (PCI subsystem): RTX 5090 Astral 1043:89E3/89EA/8A61/89EC, RTX 5080 Astral 1043:89DE/8A2B/8A45.
"""
from __future__ import annotations

import ctypes
import fcntl
import glob
import os

ASTRAL_SUBSYS = {"1043:89e3", "1043:89ea", "1043:8a61", "1043:89ec", "1043:89de", "1043:8a2b", "1043:8a45"}
I2C_SLAVE, I2C_SMBUS, I2C_SMBUS_READ, I2C_SMBUS_BYTE_DATA = 0x0703, 0x0720, 1, 2


class _Data(ctypes.Union):
    _fields_ = [("byte", ctypes.c_uint8), ("word", ctypes.c_uint16), ("block", ctypes.c_uint8 * 34)]


class _Args(ctypes.Structure):
    _fields_ = [("read_write", ctypes.c_uint8), ("command", ctypes.c_uint8),
                ("size", ctypes.c_uint32), ("data", ctypes.POINTER(_Data))]


def is_astral(pci_dev_path: str) -> bool:
    try:
        sv = open(os.path.join(pci_dev_path, "subsystem_vendor")).read().strip()[2:]
        sd = open(os.path.join(pci_dev_path, "subsystem_device")).read().strip()[2:]
        return f"{sv}:{sd}".lower() in ASTRAL_SUBSYS
    except OSError:
        return False


def find_bus(pci_short: str) -> str | None:
    """pci_short like '1:00.0' (as in the adapter name)."""
    for d in glob.glob("/sys/bus/i2c/devices/i2c-*/name"):
        try:
            if open(d).read().strip() == f"NVIDIA i2c adapter 1 at {pci_short}":
                return "/dev/" + d.split("/")[-2]
        except OSError:
            pass
    return None


class AstralPins:
    def __init__(self, dev: str, addr: int = 0x2B):
        self.fd = os.open(dev, os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, addr)

    def _rb(self, reg: int) -> int:
        d = _Data()
        a = _Args(I2C_SMBUS_READ, reg, I2C_SMBUS_BYTE_DATA, ctypes.pointer(d))
        fcntl.ioctl(self.fd, I2C_SMBUS, a)
        return d.byte

    def _pair(self, base: int) -> tuple[int, int]:
        r = [self._rb(base + i) for i in range(4)]
        return r[0] << 8 | r[1], r[2] << 8 | r[3]

    def read(self) -> dict | None:
        """{'pins': {1..6: (V, A)}, 'extra': [(V, A), (V, A)]} or None for an implausible frame."""
        pins = {}
        for rail in range(6):
            mv, ma = self._pair(0x80 + 4 * rail)
            pins[6 - rail] = (mv / 1000.0, ma / 1000.0)
        if not all(6.0 <= v <= 13.0 for v, _ in pins.values()):
            return None
        extra = []
        for base in (0x98, 0xA0):
            mv, ma = self._pair(base)
            extra.append((mv / 1000.0, ma / 1000.0) if 6000 <= mv <= 13000 else (None, None))
        return {"pins": pins, "extra": extra}

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass
