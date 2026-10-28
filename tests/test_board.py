"""Tests for meat3.backends.board (generic hwmon), fixture based.

Run: python3 -m pytest tests/test_board.py   or   python3 tests/test_board.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from meat3.backends.board import BoardBackend  # noqa: E402
from meat3.core.model import Sensor  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "board_x870e")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class TestBoard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meat3board")
        self.root = os.path.join(self.tmp, "root")
        shutil.copytree(FIX, self.root, symlinks=True)
        self.sys = os.path.join(self.root, "sys")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sensors(self, b=None):
        b = b or BoardBackend.detect(sys_root=self.sys)[0]
        return {s.id: s for s in b.sensors()}

    def test_detect_and_name(self):
        bs = BoardBackend.detect(sys_root=self.sys)
        self.assertEqual(len(bs), 1)
        self.assertEqual(bs[0].kind, "board")
        self.assertEqual(bs[0].name, "Gigabyte Technology Co., Ltd. X870E AORUS PRO")
        self.assertEqual(BoardBackend.detect(sys_root="/nonexistent"), [])

    def test_exclusions(self):
        s = self.sensors()
        names = {sid.split(".")[1] for sid in s}
        self.assertFalse(any(n.startswith(("amdgpu", "k10temp", "somegpu", "emptychip")) for n in names), names)

    def test_nvme(self):
        s = self.sensors()
        c = s["board.nvme_nvme0.temp1"]
        self.assertEqual(c.label, "Composite")
        self.assertAlmostEqual(c.value, 30.85)
        self.assertEqual(c.unit, "°C")
        self.assertTrue(c.group.endswith("/NVMe nvme0: KINGSTON SKC3000S1024G"))
        self.assertAlmostEqual(s["board.nvme_nvme0.temp3"].value, 63.85)

    def test_spd5118(self):
        s = self.sensors()
        d = s["board.spd5118_1_0051.temp1"]
        self.assertAlmostEqual(d.value, 38.0)
        self.assertIn("0x51", d.group)
        self.assertIn("0x53", s["board.spd5118_1_0053.temp1"].group)

    def test_duplicate_chips_disambiguated(self):
        s = self.sensors()
        g1 = s["board.prom21_xhci_0000_11_00_0.temp1"].group
        g2 = s["board.prom21_xhci_0000_13_00_0.temp1"].group
        self.assertNotEqual(g1, g2)
        self.assertIn("0000:11:00.0", g1)

    def test_superio(self):
        s = self.sensors()
        p = "board.nct6799_nct6775_656."
        self.assertAlmostEqual(s[p + "in0"].value, 1.032)
        self.assertEqual(s[p + "in0"].label, "Vcore")
        self.assertEqual(s[p + "in0"].unit, "V")
        self.assertIn("divider", s[p + "in0"].hint)
        self.assertEqual(s[p + "in1"].label, "in1")
        self.assertEqual(s[p + "fan1"].value, 1180)
        self.assertEqual(s[p + "fan1"].unit, "RPM")
        self.assertEqual(s[p + "fan2"].value, 0)
        self.assertAlmostEqual(s[p + "pwm1"].value, 128 * 100 / 255)
        self.assertEqual(s[p + "pwm2"].value, 100.0)
        self.assertIn("manual", s[p + "pwm2"].hint)
        self.assertIsNone(s[p + "temp13"].value)               # unreadable -> None, row kept
        self.assertEqual(s[p + "temp2"].label, "CPUTIN")
        self.assertAlmostEqual(s[p + "power1"].value, 55.5)     # _input preferred over _average
        self.assertAlmostEqual(s[p + "curr1"].value, 12.5)
        self.assertTrue(s[p + "temp1"].group.endswith("/Super I/O NCT6799"))
        # Super I/O listed first
        first = next(iter(self.sensors().values()))
        self.assertIn("Super I/O", first.group)

    def test_misc_kinds(self):
        s = self.sensors()
        self.assertAlmostEqual(s["board.sht3x_hwmon11.humidity1"].value, 45.5)
        self.assertEqual(s["board.sht3x_hwmon11.humidity1"].unit, "%")
        self.assertAlmostEqual(s["board.ath12k_hwmon_hwmon6.temp1"].value, 46.0)
        self.assertIn("Wi-Fi", s["board.ath12k_hwmon_hwmon6.temp1"].group)

    def test_ids_stable_across_hwmon_renumbering(self):
        before = set(self.sensors())
        base = os.path.join(self.sys, "class/hwmon")
        # renumber every hwmon that has a device link (no-device chips use the hwmon name by design)
        for d in sorted(os.listdir(base)):
            if os.path.exists(os.path.join(base, d, "device")):
                os.rename(os.path.join(base, d), os.path.join(base, "x" + d))
        for d in sorted(os.listdir(base)):
            if d.startswith("x"):
                os.rename(os.path.join(base, d), os.path.join(base, "hwmon" + str(100 + int(d[6:]))))
        after = set(self.sensors())
        dev_ids = {i for i in before if "hwmon" not in i.split(".")[1]}
        self.assertTrue(dev_ids <= after, dev_ids - after)

    def test_rescan_and_contract(self):
        clock = Clock()
        b = BoardBackend(sys_root=self.sys, clock=clock)
        ss = b.sensors()
        ids = [x.id for x in ss]
        self.assertEqual(len(ids), len(set(ids)))
        for x in ss:
            self.assertIsInstance(x, Sensor)
            self.assertTrue(x.value is None or isinstance(x.value, (int, float)))
            x.to_dict()
        # hot-added chip appears after the rescan interval
        h = os.path.join(self.sys, "class/hwmon/hwmon42")
        os.makedirs(h)
        with open(os.path.join(h, "name"), "w") as f:
            f.write("acpitz\n")
        with open(os.path.join(h, "temp1_input"), "w") as f:
            f.write("27800\n")
        self.assertNotIn("board.acpitz_hwmon42.temp1", {x.id for x in b.sensors()})
        clock.t += 31
        s = {x.id: x for x in b.sensors()}
        self.assertAlmostEqual(s["board.acpitz_hwmon42.temp1"].value, 27.8)
        self.assertIn("ACPI", s["board.acpitz_hwmon42.temp1"].group)


if __name__ == "__main__":
    unittest.main(verbosity=1)
