"""Tests for meat3-skutable (CustomSkuTable view/edit) on a real ASUS RX 9070 XT pp_table.

    python3 tests/test_skutable.py
"""
from __future__ import annotations

import argparse
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from meat3 import skutable as S  # noqa: E402

with open(os.path.join(HERE, "fixtures/amdgpu/pp_table_asus_9070xt.bin"), "rb") as _f:
    PP = _f.read()


def args(**kw):
    a = dict(ppt=None, tdc_gfx=None, tdc_soc=None, temp=None, tbp_m=None, tbp_b=None, zero_rpm=None,
             fan_max_rpm=None, force=False)
    a.update(kw)
    return argparse.Namespace(**a)


class TestSkuTable(unittest.TestCase):
    def setUp(self):
        self.t = S.from_pptable(PP)

    def test_real_values(self):
        self.assertEqual(len(self.t), 360)
        self.assertEqual(S.get(self.t, "SocketPowerLimitAc")[0], 317)
        self.assertEqual(S.get(self.t, "VrTdcLimit"), [330, 84])
        self.assertEqual(S.get(self.t, "PlatformTdcLimit"), [330, 84])
        self.assertEqual(S.get(self.t, "TemperatureLimit")[1], 110)
        self.assertEqual(S.get(self.t, "FwCtfLimit")[1], 118)
        self.assertEqual(S.get(self.t, "FanMaximumRpm"), 6000)
        self.assertIn("317 W", S.describe(self.t))

    def test_edits_touch_only_their_fields(self):
        new, notes = S.edit(self.t, args(tdc_gfx=360, ppt=340, temp=["Hotspot=100"], zero_rpm="off"))
        self.assertEqual(S.get(new, "VrTdcLimit"), [360, 84])
        self.assertEqual(S.get(new, "PlatformTdcLimit"), [360, 84])
        self.assertEqual(S.get(new, "SocketPowerLimitAc")[0], 340)
        self.assertEqual(S.get(new, "SocketPowerLimitDc")[0], 340)
        self.assertEqual(S.get(new, "TemperatureLimit")[1], 100)
        self.assertEqual(S.get(new, "FanZeroRpmEnable"), 0)
        changed = [i for i in range(360) if new[i] != self.t[i]]
        self.assertTrue(all(i in (0, 1, 8, 9, 22, 23, 132, 272, 273, 276, 277) for i in changed), changed)
        self.assertEqual(len(notes), 4)

    def test_refusals(self):
        with self.assertRaises(ValueError):
            S.edit(self.t, args(temp=["Hotspot=115"]))                 # above stock
        with self.assertRaises(ValueError):
            S.edit(self.t, args(temp=["Hotspot=118"], force=True))    # at shutdown limit
        with self.assertRaises(ValueError):
            S.edit(self.t, args(tbp_m=900))                            # experiment needs --force
        with self.assertRaises(ValueError):
            S.edit(self.t, args(temp=["Bogus=90"]))
        new, _ = S.edit(self.t, args(tbp_m=900, tbp_b=-5, force=True))
        self.assertEqual((S.get(new, "TotalBoardPowerM"), S.get(new, "TotalBoardPowerB")), (900, -5))

    def test_rejects_non_rdna4_table(self):
        with self.assertRaises(ValueError):
            S.from_pptable(b"\x00" * 6000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
