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


GAIN0 = 0.5          # factory IOUT_CAL_GAIN, mOhm
DROOP0 = 0.25        # factory VOUT_DROOP, mOhm


class FakeCtl:
    """PMBus regulator: reported IOUT/POUT shrink when IOUT_CAL_GAIN is raised (like real silicon)."""

    def __init__(self, pages, mode=0x17, vout=1.05, iout=200.0, temp=55.0, off_mv=5.0, mfr="IFX", model="XDPE1A2G5",
                 gain=True):
        e = V.vout_exp(mode)
        self.mode, self.page, self.mfr, self.model = mode, 0, mfr, model
        self.real = {p: (vout, iout) for p in pages}
        self.regs = {p: {V.READ_VOUT: int(round(vout / 2.0 ** e)) if e is not None else 0x5A,
                         V.READ_TEMP1: lin11(temp, 0), V.READ_VIN: lin11(12.1, -6), V.READ_IIN: lin11(18.0, -4),
                         V.VOUT_CAL_OFFSET: (int(round(off_mv / 1000 / 2.0 ** e)) & 0xFFFF) if e is not None else 0,
                         **({V.IOUT_CAL_GAIN: lin11(GAIN0, -10), V.VOUT_DROOP: lin11(DROOP0, -10)} if gain else {})}
                     for p in pages}
        self.writes = []

    def read(self, cmd):
        r = self.regs[self.page]
        vout, iout = self.real[self.page]
        k = V.linear11(r[V.IOUT_CAL_GAIN]) / GAIN0 if V.IOUT_CAL_GAIN in r else 1.0
        if cmd == V.READ_IOUT:
            return lin11(iout / k)
        if cmd == V.READ_POUT:
            return lin11(vout * iout / k, 0)
        if cmd not in r:
            raise OSError(121, "Remote I/O error")          # NAK: command not supported
        return r[cmd]


class FakeMps(FakeCtl):
    """MPS MP2686A-style controller: vendor READ_VOUT, 8-bit VOUT_OFFSET steps, no VOUT_DROOP."""

    def __init__(self, pages, vout=1.05, iout=200.0, linear_vout=True, offset_steps=1):
        super().__init__(pages, mode=0x17, vout=vout, iout=iout, mfr="MPS", model="MP2686A")
        for p in pages:
            r = self.regs[p]
            r[V.READ_VOUT] = 0xE000 | (int(round(vout * 256)) if linear_vout else int(round(vout / 0.005)) - 49)
            r[V.MPS_MFR_VR_CONFIG2] = V.MPS_VOUT_LINEAR if linear_vout else 0
            r[V.VOUT_CAL_OFFSET] = offset_steps & 0xFF
            r.pop(V.VOUT_DROOP)
        self.mode = 0x40          # VOUT_MODE garbage: must be ignored


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

    def read_word(self, addr, cmd, pec=False):
        return self._c(addr).read(cmd)

    def read_block(self, addr, cmd):
        c = self._c(addr)
        return (c.mfr if cmd == V.MFR_ID else c.model).encode()

    def write_byte(self, addr, cmd, val):
        c = self._c(addr)
        assert cmd == V.PAGE
        c.page = val

    def write_word(self, addr, cmd, val, pec=False):
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
        offs = [w for w in self.ctls[0x22].writes if w[1] == V.VOUT_CAL_OFFSET]
        self.assertEqual(offs[-1], (0, V.VOUT_CAL_OFFSET, 3))

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

    def test_current_scale_raises_effective_limit_and_display_stays_true(self):
        v = self.vrm(allow_writes=True, max_current_scale=1.6)
        d = v.read_all()
        self.assertAlmostEqual(d[(0x22, 0)]["scale"], 1.0)
        self.assertFalse(v.set_current_scale(0x22, 0, 1.8)[0])            # above max_current_scale
        self.assertFalse(v.set_current_scale(0x22, 0, 0.8)[0])            # over-reporting not offered
        ok, msg = v.set_current_scale(0x22, 0, 1.4)
        self.assertTrue(ok, msg)
        d = v.read_all()
        g = d[(0x22, 0)]
        self.assertAlmostEqual(g["scale"], 1.4, places=2)
        self.assertAlmostEqual(g["iout"], 200 / 1.4, delta=0.5)            # what the firmware now sees
        self.assertAlmostEqual(g["iout_real"], 200, delta=1.0)             # what meat3 shows
        self.assertAlmostEqual(g["pout_real"], 210, delta=2.0)
        ok, msg = v.reset(0x22, 0)                                         # offset + gain + droop back
        self.assertTrue(ok, msg)
        self.assertAlmostEqual(v.read_all()[(0x22, 0)]["scale"], 1.0)

    def test_load_line(self):
        v = self.vrm(allow_writes=True)
        v.read_all()
        self.assertFalse(v.set_droop_mohm(0x22, 0, 0.6)[0])                # > 2x factory
        ok, msg = v.set_droop_mohm(0x22, 0, 0.125)
        self.assertTrue(ok, msg)
        self.assertAlmostEqual(v.read_all()[(0x22, 0)]["droop_mohm"], 0.125, places=3)

    def test_controller_without_gain_or_droop(self):
        self.ctls[0x22] = FakeCtl([0], gain=False)
        v = self.vrm(allow_writes=True)
        d = v.read_all()
        self.assertNotIn("gain_mohm", d[(0x22, 0)])
        self.assertAlmostEqual(d[(0x22, 0)]["iout_real"], 200, delta=1.0)
        self.assertIn("unknown", v.set_current_scale(0x22, 0, 1.2)[1])

    def test_real_power(self):
        d = self.vrm().read_all()
        p = V.VrmAccess.power_summary(d)
        self.assertAlmostEqual(p["input_w"], 3 * 12.1 * 18.0, delta=2)    # 12 V in, all three controllers
        self.assertAlmostEqual(p["rails_w"], 210 + 2 * 22.5 + 2 * 13.5, delta=3)
        self.assertEqual(p["real_w"], max(p["input_w"], p["rails_w"] / V.VRM_EFFICIENCY))
        self.assertTrue(V.shunt_bypassed(board_w=3.0, real_w=380.0))
        self.assertFalse(V.shunt_bypassed(board_w=300.0, real_w=330.0))
        self.assertFalse(V.shunt_bypassed(board_w=3.0, real_w=40.0))         # idle: can't tell

    def test_backend_real_power_and_new_controls(self):
        be = A.AmdGpuBackend.detect(self.root)[0]
        be.vrm_opener = self.opener
        ss = {s.id: s for s in be.sensors()}
        self.assertGreater(ss["amdgpu0.vrm.real_power"].value, 300)
        self.assertIn("amdgpu0.vrm.vgfx.current_scale", ss)
        self.assertIn("amdgpu0.vrm.vgfx.llc", ss)
        cs = {c.id: c for c in be.controls()}
        self.assertEqual(cs["amdgpu0.vrm.vgfx.current_scale"].danger, 2)
        self.assertFalse(cs["amdgpu0.vrm.vgfx.current_scale"].available)    # writes off by default
        self.assertFalse(be.apply("amdgpu0.vrm.vgfx.current_scale", 1.3)[0])

    def test_mps_profile_decode_and_write_gating(self):
        self.ctls[0x22] = FakeMps([0], vout=1.05)
        self.ctls[0x24] = FakeMps([0, 1], vout=0.75, iout=30, linear_vout=False)
        v = self.vrm(allow_writes=True)
        self.assertEqual([c.profile for c in v.controllers], ["mps", "mps", "generic"])
        d = v.read_all()
        self.assertAlmostEqual(d[(0x22, 0)]["vout"], 1.05, places=2)          # 1/256 V linear
        self.assertAlmostEqual(d[(0x24, 0)]["vout"], 0.75, places=3)          # VR13 VID 5 mV
        self.assertEqual(d[(0x22, 0)]["offset_steps"], 1)
        self.assertIsNone(d[(0x22, 0)]["offset_mv"])                          # step size unverified
        self.assertNotIn("droop_mohm", d[(0x22, 0)])                          # 0x28 never read on MPS
        ok, why = v.set_offset_mv(0x22, 0, 10)
        self.assertFalse(ok)
        self.assertIn("mps_offset_lsb_mv", why)
        ok, why = v.set_current_scale(0x22, 0, 1.2)
        self.assertFalse(ok)
        self.assertIn("experimental_mps_writes", why)
        self.assertFalse(v.set_droop_mohm(0x22, 0, 0.1)[0])
        self.assertEqual(self.ctls[0x22].writes, [])

    def test_mps_writes_once_verified(self):
        self.ctls[0x22] = FakeMps([0], offset_steps=1)
        v = self.vrm(allow_writes=True, mps_offset_lsb_mv=5, experimental_mps_writes=True)
        d = v.read_all()
        self.assertEqual(d[(0x22, 0)]["offset_mv"], 5.0)
        ok, msg = v.set_offset_mv(0x22, 0, 25)
        self.assertTrue(ok, msg)
        self.assertEqual(self.ctls[0x22].writes[-1], (0, V.VOUT_CAL_OFFSET, 5))
        ok, msg = v.set_offset_mv(0x22, 0, -10)
        self.assertTrue(ok, msg)
        self.assertEqual(self.ctls[0x22].writes[-1], (0, V.VOUT_CAL_OFFSET, 0xFE))
        ok, msg = v.set_current_scale(0x22, 0, 1.3)
        self.assertTrue(ok, msg)
        self.assertAlmostEqual(v.read_all()[(0x22, 0)]["iout_real"], 200, delta=1.0)
        self.assertTrue(v.reset(0x22, 0)[0])
        self.assertEqual([w for w in self.ctls[0x22].writes if w[1] == V.VOUT_CAL_OFFSET][-1][2], 1)

    def test_no_controllers(self):
        self.ctls.clear()
        v = self.vrm()
        self.assertEqual(v.controllers, [])
        self.assertIn("0x22/0x24/0x26", v.error)

    def test_backend_names_the_firmware_when_bus_is_dead(self):
        """Fixture SMU firmware 0x685300 is newer than the last version exposing the VR bus."""
        self.ctls.clear()
        be = A.AmdGpuBackend.detect(self.root)[0]
        be.vrm_opener = self.opener
        ss = {s.id: s for s in be.sensors()}
        self.assertIn("0x685300", ss["amdgpu0.vrm.status"].hint)
        self.assertIn("0x684a00", ss["amdgpu0.vrm.status"].hint)

    def test_rdna4_gain_registers(self):
        """MP2868A pair: page-2 gains and the scripts' stock backup give the TBP/TDC scales."""
        stock = {"pg_vr1": 0x400, "cg1_vr1": 0x300, "pg_vr2": 0x380, "cg1_alt_vr1": 0x40}
        packed = (1 << 40) | (stock["pg_vr1"] << 29) | (stock["cg1_vr1"] << 18) | (stock["pg_vr2"] << 7) | stock["cg1_alt_vr1"]
        b1, b2 = packed >> 21 & 0x1FFFFF, packed & 0x1FFFFF
        self.assertEqual(V.decode_rdna4_backup(b1, b2), stock)
        self.assertIsNone(V.decode_rdna4_backup(b1 & ~(1 << 19), b2))          # valid flag clear

        def mp2868a(pages, pg, cg1, cg1a, block):
            c = FakeMps(pages, vout=1.0, iout=100)
            c.regs[0][V.MFR_MODEL] = 0x6868                                   # fpsflow's RDNA4 check byte
            c.regs.setdefault(1, {})                                          # backup lives on page 1
            c.real.setdefault(1, (1.0, 100))
            c.regs[1][V.MPS_BACKUP2] = block >> 5 & 0xFFFF
            c.regs[1][V.MPS_BACKUP1] = (block & 0x1F) << 11
            c.regs[1][V.MPS_BACKUP3] = 0
            c.regs[0][V.MPS_BACKUP1] = 0
            c.regs[2] = {V.MPS_PG: 0x8000 | pg, V.MPS_CG1: cg1, V.MPS_CG1_ALT: cg1a << 4 | 0x3, V.MPS_IDROOP_GAIN: 0x1234}
            c.real[2] = (1.0, 100)
            return c
        # current PG of vr1 = stock / 1.25, CG1 = stock / 1.2
        self.ctls = {0x22: mp2868a([0], 0x400 * 4 // 5, 0x300 * 5 // 6, 0x40, b1),
                     0x24: mp2868a([0, 1], 0x380, 0, 0, b2)}
        v = self.vrm()
        self.assertTrue(all(c.mp2868a for c in v.controllers))
        g = v.rdna4_gains()
        self.assertEqual(g["stock_source"], "scripts backup (legacy)")
        self.assertAlmostEqual(g["tbp_scale"], 1.25, places=3)
        self.assertAlmostEqual(g["tdc_scale"], 1.2, places=3)
        self.assertAlmostEqual(g["tdc_alt_scale"], 1.0, places=3)
        self.assertEqual(g["idroop_raw"], 0x1234)
        self.assertEqual(self.ctls[0x22].page, 0)                               # PAGE restored
        self.assertEqual(self.ctls[0x22].writes, [])                            # nothing but PAGE written
        be = A.AmdGpuBackend.detect(self.root)[0]
        be.vrm_opener = self.opener
        ss = {s.id: s for s in be.sensors()}
        self.assertAlmostEqual(ss["amdgpu0.vrm.tbp_scale"].value, 1.25, places=3)
        self.assertAlmostEqual(ss["amdgpu0.vrm.tdc_scale"].value, 1.2, places=3)
        self.assertIn("amdgpu0.vrm.real_power_scaled", ss)

    # ---------------------------------------------------------------- RDNA4 scaling (fpsflow in meat3)
    STOCK = {"pg_vr1": 0x400, "cg1_vr1": 0x300, "pg_vr2": 0x380, "cg1_alt_vr1": 0x40}

    def mp2868a_pair(self, scaled=False, legacy_offset=False, backup=None):
        """Two MP2868A-like controllers with page-2 gains at stock (or at /1.25 and /1.2 when scaled)."""
        def mk(pages, pg, cg1, cg1a):
            c = FakeMps(pages, vout=1.0, iout=100)
            c.regs[0][V.MFR_MODEL] = 0x6868
            c.regs.setdefault(1, {})
            c.real.setdefault(1, (1.0, 100))
            c.regs[0][V.MPS_BACKUP1] = 0x0123                       # low 11 bits are "something else": must survive
            c.regs[1][V.MPS_BACKUP1] = 0x0456
            c.regs[1][V.MPS_BACKUP2] = 0
            c.regs[1][V.MPS_BACKUP3] = 0x0100 if legacy_offset else 0
            c.regs[0][V.MPS_VID] = 0x0001
            c.regs[1][V.MPS_VID] = 0x0000
            c.regs[0][V.MPS_TRIM] = 0x0012
            c.regs[2] = {V.MPS_PG: 0x8000 | pg, V.MPS_CG1: 0x4000 | cg1, V.MPS_CG1_ALT: cg1a << 4 | 0x3,
                         V.MPS_IDROOP_GAIN: 0x1234}
            c.real[2] = (1.0, 100)
            return c
        st = self.STOCK
        c1 = mk([0], st["pg_vr1"] * 4 // 5 if scaled else st["pg_vr1"], st["cg1_vr1"] * 5 // 6 if scaled else st["cg1_vr1"],
                st["cg1_alt_vr1"])
        c2 = mk([0, 1], st["pg_vr2"], 0, 0)
        if backup:
            b1, b2 = backup
            for c, b in ((c1, b1), (c2, b2)):
                p1, p2, p3, p4 = V.split_vr_block(b)
                c.regs[1][V.MPS_BACKUP2] = p1
                c.regs[1][V.MPS_BACKUP1] = c.regs[1][V.MPS_BACKUP1] & 0x7FF | p2 << 11
                c.regs[1][V.MPS_BACKUP3] = c.regs[1][V.MPS_BACKUP3] & 0xFF00 | p3
                c.regs[0][V.MPS_BACKUP1] = c.regs[0][V.MPS_BACKUP1] & 0x7FF | p4 << 11
        self.ctls = {0x22: c1, 0x24: c2}
        return c1, c2

    def test_rdna4p_backup_codec(self):
        f = dict(self.STOCK, gfx_vid=1, soc_vid=0, vddci_vid=1, vram_vid=0, trim_vr1=0x12, droop_vr1=0x34)
        b1, b2 = V.encode_rdna4p_backup(f)
        self.assertLess(max(b1, b2), 1 << 34)
        self.assertEqual(V.decode_rdna4p_backup(b1, b2), f)
        self.assertIsNone(V.decode_rdna4p_backup(b1 ^ 1 << 20, b2))                 # CRC catches a flipped bit
        self.assertIsNone(V.decode_rdna4p_backup(0, 0))                             # empty: not a backup
        self.assertEqual(V.join_vr_block(*V.split_vr_block(b1)), b1)
        # the script's own crc8: poly 0x07, MSB first
        self.assertEqual(V.crc8_bits(0x01, 8), 0x07)
        self.assertEqual(V.crc8_bits(0x00, 60), 0)

    def test_scale_writes_backup_then_gains_and_corrects_sensors(self):
        c1, c2 = self.mp2868a_pair()
        v = self.vrm(allow_writes=True)
        g = v.rdna4_gains()
        self.assertEqual(g["stock_source"], "meat3 first-seen")
        self.assertFalse(g["legacy_power_offset"])
        ok, msg = v.set_rdna4_scale("tbp", 1.25)
        self.assertTrue(ok, msg)
        # backup now on the chip, in the rdna4p format, with the untouched bits preserved
        blk1 = V.join_vr_block(c1.regs[1][V.MPS_BACKUP2], c1.regs[1][V.MPS_BACKUP1] >> 11, c1.regs[1][V.MPS_BACKUP3] & 0xFF,
                               c1.regs[0][V.MPS_BACKUP1] >> 11)
        blk2 = V.join_vr_block(c2.regs[1][V.MPS_BACKUP2], c2.regs[1][V.MPS_BACKUP1] >> 11, c2.regs[1][V.MPS_BACKUP3] & 0xFF,
                               c2.regs[0][V.MPS_BACKUP1] >> 11)
        bk = V.decode_rdna4p_backup(blk1, blk2)
        self.assertEqual({k: bk[k] for k in self.STOCK}, self.STOCK)
        self.assertEqual((bk["gfx_vid"], bk["trim_vr1"], bk["droop_vr1"]), (1, 0x12, 0x34))
        self.assertEqual(c1.regs[0][V.MPS_BACKUP1] & 0x7FF, 0x0123)
        self.assertEqual(c1.regs[1][V.MPS_BACKUP1] & 0x7FF, 0x0456)
        # gains: PG = stock / 1.25 on both controllers, upper bits kept
        self.assertEqual(c1.regs[2][V.MPS_PG], 0x8000 | int(0x400 / 1.25))
        self.assertEqual(c2.regs[2][V.MPS_PG], 0x8000 | int(0x380 / 1.25))
        self.assertEqual(c1.regs[2][V.MPS_CG1], 0x4000 | 0x300)                   # TDC untouched
        self.assertEqual(c1.page, 0)
        g = v.rdna4_gains()
        self.assertEqual(g["stock_source"], "scripts backup (rdna4p)")
        self.assertAlmostEqual(g["tbp_scale"], 1.25, places=2)
        ok, msg = v.set_rdna4_scale("tdc", 1.2)
        self.assertTrue(ok, msg)
        self.assertEqual(c1.regs[2][V.MPS_CG1], 0x4000 | int(0x300 / 1.2))
        self.assertEqual(c1.regs[2][V.MPS_CG1_ALT], int(0x40 / 1.2) << 4 | 0x3)
        g = v.rdna4_gains()
        self.assertAlmostEqual(g["tdc_scale"], 1.2, places=2)
        self.assertAlmostEqual(g["tdc_alt_scale"], 1.2, places=1)
        self.assertTrue(v.gains_touched)
        # caps
        self.assertFalse(v.set_rdna4_scale("tbp", 1.4)[0])                        # default cap 1.3
        self.assertFalse(v.set_rdna4_scale("tbp", 0.9)[0])
        v2 = self.vrm(allow_writes=True, max_tbp_scale=9)
        self.assertFalse(v2.set_rdna4_scale("tbp", 1.6)[0])                       # hard cap 1.5
        # reset and restore-on-close
        ok, msg = v.reset_rdna4_gains()
        self.assertTrue(ok, msg)
        self.assertEqual(c1.regs[2][V.MPS_PG], 0x8000 | 0x400)
        self.assertEqual(c2.regs[2][V.MPS_PG], 0x8000 | 0x380)
        self.assertEqual(c1.regs[2][V.MPS_CG1_ALT], 0x40 << 4 | 0x3)
        self.assertFalse(v.gains_touched)
        v.set_rdna4_scale("tbp", 1.1)
        v.close()
        self.assertEqual(c1.regs[2][V.MPS_PG], 0x8000 | 0x400)                     # stock again on daemon stop

    def test_half_applied_scale_still_restores_on_close(self):
        # the vr2 PG write fails after vr1 was already scaled: the daemon must still put vr1 back on stop
        c1, c2 = self.mp2868a_pair()
        v = self.vrm(allow_writes=True)
        bus = next(b for c in v.controllers if c.addr == 0x24 for b in [v._bus(c.bus_no)])
        real_write_word, failed = bus.write_word, []

        def flaky(addr, cmd, val, pec=False):
            if addr == 0x24 and cmd == V.MPS_PG and not failed:
                failed.append(1)
                raise OSError(121, "Remote I/O error")
            return real_write_word(addr, cmd, val, pec)
        bus.write_word = flaky
        ok, msg = v.set_rdna4_scale("tbp", 1.25)
        self.assertFalse(ok)
        self.assertIn("I2C error", msg)
        self.assertEqual(c1.regs[2][V.MPS_PG], 0x8000 | int(0x400 / 1.25))        # vr1 did get scaled
        self.assertEqual(c2.regs[2][V.MPS_PG], 0x8000 | 0x380)
        self.assertTrue(v.gains_touched)
        v.close()
        self.assertEqual(c1.regs[2][V.MPS_PG], 0x8000 | 0x400)                     # restored

    def test_bus_access_is_serialised_between_threads(self):
        # poll thread (read_all / rdna4_gains) and apply threads share one PAGE register per controller:
        # every gain write must land on page 2 and PAGE must be back at 0 when everything is done
        import threading
        c1, c2 = self.mp2868a_pair()
        v = self.vrm(allow_writes=True)
        self.assertIsInstance(v.lock, type(threading.RLock()))
        stop = threading.Event()

        def poller():
            while not stop.is_set():
                v.read_all(min_interval=0)
                v.rdna4_gains()

        def applier():
            for k in (1.1, 1.2, 1.0, 1.3):
                ok, msg = v.set_rdna4_scale("tbp", k)
                self.assertTrue(ok, msg)
        ts = [threading.Thread(target=poller) for _ in range(2)] + [threading.Thread(target=applier)]
        for t in ts[2:]:
            t.start()
        for t in ts[:2]:
            t.start()
        ts[2].join()
        stop.set()
        for t in ts[:2]:
            t.join()
        for c in (c1, c2):
            self.assertTrue(all(page == 2 for page, cmd, _ in c.writes if cmd == V.MPS_PG), c.writes)
            self.assertEqual(c.page, 0)
        self.assertEqual(c1.regs[2][V.MPS_PG], 0x8000 | int(0x400 / 1.3))

    def test_mps_gfx_decode_matches_first_real_card(self):
        # ASUS 9070 XT at 365 W: firmware GFX 239 A / 258 W, SoC 21.7 A, memory 21.3 A; the MP2868A GFX loop
        # reported 120 A and READ_POUT/READ_IIN were unusable, so: GFX IOUT x2, rail power = VOUT x IOUT,
        # real power from the rails only (READ_IIN ignored)
        c1, c2 = self.mp2868a_pair()
        for c, page, vout, iout in ((c1, 0, 1.08, 120.0), (c2, 0, 0.905, 19.5), (c2, 1, 1.35, 17.1)):
            c.real[page] = (vout, iout)
            c.regs[page][V.READ_VOUT] = 0xE000 | int(round(vout * 256))     # MPS linear 1/256 V coding
        v = self.vrm()
        d = v.read_all()
        g = d[(0x22, 0)]
        self.assertAlmostEqual(g["iout_real"], 240.0, delta=1.0)
        self.assertAlmostEqual(g["vout"], 1.08, places=2)
        self.assertAlmostEqual(g["pout_real"], g["vout"] * 240.0, delta=0.5)
        self.assertFalse(g["iin_trusted"])
        self.assertAlmostEqual(d[(0x24, 0)]["iout_real"], 19.5, delta=0.3)       # SoC loop not doubled
        self.assertAlmostEqual(d[(0x24, 1)]["iout_real"], 17.1, delta=0.3)
        self.assertEqual(V.RAILS[(0x24, 1)][0], "vddio_mem")
        p = V.VrmAccess.power_summary(d)
        self.assertFalse(p["iin_trusted"])
        self.assertAlmostEqual(p["rails_w"], 1.08 * 240 + 0.905 * 19.5 + 1.35 * 17.1, delta=4)   # ~300 W, firmware said 365
        self.assertAlmostEqual(p["real_w"], p["rails_w"] / V.VRM_EFFICIENCY, delta=1)   # not the IIN figure
        self.assertIsNotNone(p["input_w"])                                                 # still reported

    def test_scale_refusals(self):
        self.mp2868a_pair()
        v = self.vrm()                                                             # writes disabled
        ok, why = v.set_rdna4_scale("tbp", 1.2)
        self.assertFalse(ok); self.assertIn("allow_writes", why)
        self.mp2868a_pair(legacy_offset=True)
        v = self.vrm(allow_writes=True)
        ok, why = v.set_rdna4_scale("tbp", 1.2)
        self.assertFalse(ok); self.assertIn("older fpsflow", why)
        self.assertEqual(self.ctls[0x22].regs[2][V.MPS_PG], 0x8000 | 0x400)         # nothing written

    def test_existing_script_backup_is_used_as_stock(self):
        f = dict(self.STOCK, gfx_vid=1, soc_vid=0, vddci_vid=0, vram_vid=0, trim_vr1=0x12, droop_vr1=0x34)
        c1, c2 = self.mp2868a_pair(scaled=True, backup=V.encode_rdna4p_backup(f))
        v = self.vrm(allow_writes=True)
        g = v.rdna4_gains()
        self.assertEqual(g["stock_source"], "scripts backup (rdna4p)")
        self.assertAlmostEqual(g["tbp_scale"], 1.25, places=2)
        self.assertAlmostEqual(g["tdc_scale"], 1.2, places=2)
        before = dict(c1.regs[1])
        ok, msg = v.set_rdna4_scale("tbp", 1.1)                                    # must not rewrite the backup
        self.assertTrue(ok, msg)
        self.assertEqual(c1.regs[1], before)
        self.assertEqual(c1.regs[2][V.MPS_PG], 0x8000 | int(0x400 / 1.1))

    def test_backend_scale_controls_and_sensors(self):
        self.mp2868a_pair()
        import json
        with open(os.path.join(self.tmp, "fac.json"), "w") as fh:
            json.dump({}, fh)
        be = A.AmdGpuBackend.detect(self.root)[0]
        be.vrm_opener = self.opener
        from meat3.core import config as C
        old = C.get
        C.get = lambda: dict(old(), amdgpu_vrm={"enabled": True, "allow_writes": True})
        try:
            ss = {s.id: s for s in be.sensors()}
            cs = {c.id: c for c in be.controls()}
            self.assertIn("amdgpu0.vrm.board_power_corrected", ss)
            tb, td = cs["amdgpu0.vrm.tbp_scale"], cs["amdgpu0.vrm.tdc_scale"]
            self.assertTrue(tb.available and td.available and cs["amdgpu0.vrm.gains_reset"].available)
            self.assertEqual((tb.min, tb.max, tb.danger), (1.0, 1.3, 2))
            ok, msg = be.apply("amdgpu0.vrm.tbp_scale", 1.2)
            self.assertTrue(ok, msg)
            self.assertEqual(self.ctls[0x22].regs[2][V.MPS_PG], 0x8000 | int(0x400 / 1.2))
            self.assertFalse(be.apply("amdgpu0.vrm.tbp_scale", "x")[0])
            ok, msg = be.apply("amdgpu0.vrm.gains_reset", None)
            self.assertTrue(ok, msg)
            self.assertEqual(self.ctls[0x22].regs[2][V.MPS_PG], 0x8000 | 0x400)
            ss = {s.id: s for s in be.sensors()}
            self.assertAlmostEqual(ss["amdgpu0.vrm.tbp_scale"].value, 1.0, places=2)
        finally:
            C.get = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
