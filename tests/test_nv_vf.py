"""Client VF-point block parsing (fixtures captured on an RTX 5080 / GB203, module v4, +350 MHz offset)."""
from __future__ import annotations

import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from meat3.backends import nv_rm  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "nv_vf")


def _read(name: str) -> bytes:
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


class VfPointsTest(unittest.TestCase):
    def setUp(self):
        self.status = _read("status_0x2080902b.bin")
        self.ctrl = bytearray(_read("control_0x2080902c.bin"))

    def test_parse_gpc_points(self):
        pts = nv_rm.parse_vf_points(self.status, self.ctrl)
        self.assertEqual(len(pts), 127)                       # GPC points only (0x404/0x606 domains skipped)
        self.assertEqual([p["i"] for p in pts], list(range(127)))
        self.assertEqual(pts[0]["mv"], 450.0)
        self.assertEqual(pts[-1]["mv"], 1240.0)
        p120 = pts[120]
        self.assertEqual((p120["mv"], p120["mhz"], p120["base"], p120["delta"]), (1200.0, 3592.0, 3247.0, 350.0))
        self.assertTrue(all(p["delta"] == 350.0 for p in pts))  # uniform NVML offset
        mv = [p["mv"] for p in pts]
        self.assertEqual(mv, sorted(mv))

    def test_set_deltas_validation(self):
        class Fake(nv_rm.RMClient):
            def __init__(self, status, ctrl):
                self.status, self.ctrl, self.written = status, ctrl, None

            def vf_blocks(self):
                return b"", self.status, bytearray(self.ctrl)

            def control(self, cmd, params):
                self.assertTrue(cmd == nv_rm.CLK_VF_CTRL_SET)
                self.written = bytes(params)
                return 0, bytearray(params)

            assertTrue = staticmethod(lambda c: None if c else (_ for _ in ()).throw(AssertionError()))

        rm = Fake(self.status, self.ctrl)
        with self.assertRaises(nv_rm.RMError):
            rm.set_vf_deltas({120: 2000})                   # beyond +-1000
        with self.assertRaises(nv_rm.RMError):
            rm.set_vf_deltas({130: 0})                      # not a GPC point
        with self.assertRaises(nv_rm.RMError):
            rm.set_vf_deltas({120: -300})                   # 3247-300 < point 119's 3577: non-monotonic
        # the "3400 only at >= 1.2 V" shape: points below the knee capped at 3395, knee and above 3400
        pts = {p["i"]: p for p in nv_rm.parse_vf_points(self.status, self.ctrl)}
        deltas = {i: (3400 if pts[i]["mv"] >= 1200 else min(pts[i]["base"] + 350, 3395)) - pts[i]["base"]
                  for i in range(100, 127)}
        deltas = {i: d for i, d in deltas.items() if d != 350}
        n = rm.set_vf_deltas(deltas)
        self.assertEqual(n, len(deltas))
        self.assertEqual(struct.unpack_from("<i", rm.written, 0x24 + 0x10 * 120 + 8)[0], 153000)   # 3247 + 153
        self.assertEqual(struct.unpack_from("<i", rm.written, 0x24 + 0x10 * 90 + 8)[0], 350000)    # untouched


if __name__ == "__main__":
    unittest.main()
