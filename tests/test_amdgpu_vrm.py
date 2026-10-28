"""Tests for RDNA4 VRM PMBus access (meat3/backends/amdgpu_vrm.py) against fake controllers.

    python3 tests/test_amdgpu_vrm.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(HERE, "fixtures", "amdgpu"))
from meat3.backends import amdgpu as A          # noqa: E402
from meat3.backends import amdgpu_vrm as V      # noqa: E402
import make_fixtures as fx                      # noqa: E402


def lin11(x: float, exp: int = -4) -> int:
    while abs(x / 2.0 ** exp) > 1023:     # keep the 11-bit signed mantissa in range
        exp += 1
    return ((exp & 0x1F) << 11) | (int(round(x / 2.0 ** exp)) & 0x7FF)


class FakeCtl:
    def __init__(self, pages, mode=0x17, vout=1.05, iout=200.0, temp=55.0, off_mv=5.0, mfr="IFX", model="XDPE1A2G5"):
        e = V.vout_exp(mode)
        self.mode, self.page, self.mfr, self.model = mode, 0, mfr, model
        self.regs = {p: {V.READ_VOUT: int(round(vout / 2.0 ** e)) if e is not None else 0x5A,
                         V.READ_IOUT: lin11(iout), V.READ_TEMP1: lin11(temp, 0), V.READ_POUT: lin11(vout * iout, 0),
                         V.READ_VIN: lin11(12.1, -6), V.READ_IIN: lin11(18.0, -4),
                         V.VOUT_CAL_OFFSET: (int(round(off_mv / 1000 / 2.0 ** e)) & 0xFFFF) if e is not None else 0}
                     for p in pages}
        self.writes = []


class FakeBus:
    def __init__(self, ctls):
        self.ctls = ctls

    def _c(self, addr):
        c = self.ctls.get(addr)
        if c is None:
            raise OSError(6, "No such device or address")
        return c

    def read_byte(self, addr, cmd):
        c = self._c(addr)
        return {V.PMBUS_REVISION: 0x33, V.VOUT_MODE: c.mode, V.PAGE: c.page}[cmd]

    def read_word(self, addr, cmd):
        c = self._c(addr)
        return c.regs[c.page][cmd]

    def read_block(self, addr, cmd):
        c = self._c(addr)
        return (c.mfr if cmd == V.MFR_ID else c.model).encode()

    def write_byte(self, addr, cmd, val):
        c = self._c(addr)
        assert cmd == V.PAGE
        c.page = val

    def write_word(self, addr, cmd, val):
        c = self._c(addr)
        c.writes.append((c.page, cmd, val))
        c.regs[c.page][cmd] = val

    def close(self):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meat3-vrm-")
        self.root = os.path.join(self.tmp, "root")
        self.dev = fx.build(self.root, od=True)
        for n, name in ((3, "AMDGPU DM i2c hw bus 0"), (7, "AMDGPU SMU 0"), (8, "AMDGPU SMU 1")):
            os.makedirs(os.path.join(self.dev, f"i2c-{n}"))
            with open(os.path.join(self.dev, f"i2c-{n}", "name"), "w") as f:
                f.write(name + "\n")
        self.ctls = {0x22: FakeCtl([0]), 0x24: FakeCtl([0, 1], vout=0.75, iout=30), 0x26: FakeCtl([0, 1], vout=1.35, iout=10)}
        self.opened = []

    def opener(self, bus):
        self.opened.append(bus)
        return FakeBus(self.ctls if bus == 7 else {})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def vrm(self, **cfg):
        return V.VrmAccess(self.dev, serial="t", opener=self.opener,
                           state_path=os.path.join(self.tmp, "fac.json"), cfg=cfg)


class TestVrm(Base):
    def test_detect_only_smu_buses_and_known_addresses(self):
        v = self.vrm()
        self.assertEqual(sorted(set(self.opened)), [7, 8])                # display DDC bus 3 never opened
        self.assertEqual([(c.bus_no, c.addr) for c in v.controllers], [(7, 0x22), (7, 0x24), (7, 0x26)])
        self.assertEqual(v.controllers[0].model, "XDPE1A2G5")

    def test_telemetry_decode(self):
        d = self.vrm().read_all()
        g = d[(0x22, 0)]
        self.assertAlmostEqual(g["vout"], 1.05, places=2)
        self.assertAlmostEqual(g["iout"], 200.0, places=0)
        self.assertAlmostEqual(g["temp"], 55.0)
        self.assertAlmostEqual(g["vin"], 12.1, places=1)
        self.assertAlmostEqual(g["offset_mv"], 5.86, places=1)          # 3 LSB of 2^-9 V
        self.assertNotIn("vin", d[(0x24, 1)])                          # input rail only on page 0
        self.assertEqual(self.ctls[0x26].page, 0)                       # PAGE left at 0

    def test_writes_disabled_by_default(self):
        v = self.vrm()
        v.read_all()
        ok, why = v.set_offset_mv(0x22, 0, 10)
        self.assertFalse(ok)
        self.assertIn("allow_writes", why)
        self.assertEqual(self.ctls[0x22].writes, [])

    def test_offset_write_clamp_and_factory_reset(self):
        v = self.vrm(allow_writes=True, max_offset_mv=30)
        v.read_all()
        self.assertFalse(v.set_offset_mv(0x22, 0, 40)[0])
        ok, msg = v.set_offset_mv(0x22, 0, 25)
        self.assertTrue(ok, msg)
        self.assertEqual(self.ctls[0x22].writes[-1], (0, V.VOUT_CAL_OFFSET, 13))     # 25 mV / 1.953 mV
        ok, msg = v.set_offset_mv(0x24, 1, -20)
        self.assertTrue(ok, msg)
        self.assertEqual(self.ctls[0x24].writes[-1], (1, V.VOUT_CAL_OFFSET, (-10) & 0xFFFF))
        # factory value survives a daemon restart (state file), reset restores it
        v2 = self.vrm(allow_writes=True)
        v2.read_all()
        self.assertTrue(v2.reset(0x22, 0)[0])
        self.assertEqual(self.ctls[0x22].writes[-1], (0, V.VOUT_CAL_OFFSET, 3))

    def test_non_linear_vout_mode_refuses_writes(self):
        self.ctls[0x22] = FakeCtl([0], mode=0x21)            # VID mode
        v = self.vrm(allow_writes=True)
        d = v.read_all()
        self.assertIsNone(d[(0x22, 0)]["vout"])
        ok, why = v.set_offset_mv(0x22, 0, 5)
        self.assertFalse(ok)
        self.assertIn("non-linear", why)

    def test_backend_sensors_and_controls(self):
        be = A.AmdGpuBackend.detect(self.root)[0]
        be.vrm_opener = self.opener
        ss = {s.id: s for s in be.sensors()}
        self.assertIn("XDPE1A2G5", ss["amdgpu0.vrm.status"].label)
        self.assertAlmostEqual(ss["amdgpu0.vrm.vgfx.vout"].value, 1.05, places=2)
        self.assertAlmostEqual(ss["amdgpu0.vrm.vsoc.iout"].value, 30, places=0)
        cs = {c.id: c for c in be.controls()}
        c = cs["amdgpu0.vrm.vgfx.offset"]
        self.assertFalse(c.available)                       # default config: writes off
        self.assertEqual(c.danger, 2)
        self.assertEqual(c.default, 6)                      # factory offset, rounded
        ok, msg = be.apply("amdgpu0.vrm.vgfx.offset", 10)
        self.assertFalse(ok)
        self.assertEqual(self.ctls[0x22].writes, [])

    def test_no_controllers(self):
        self.ctls.clear()
        v = self.vrm()
        self.assertEqual(v.controllers, [])
        self.assertIn("0x22/0x24/0x26", v.error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
