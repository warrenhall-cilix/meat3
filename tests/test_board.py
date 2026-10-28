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

    def test_drives_left_to_storage_backend(self):
        s = self.sensors()
        self.assertFalse([i for i in s if ".nvme_" in i or ".drivetemp" in i])

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
        self.assertEqual(s[p + "in1"].label, "VIN1 (raw pin)")
        self.assertIn("divider is unknown", s[p + "in1"].hint)
        self.assertEqual(s[p + "fan1"].value, 1180)
        self.assertEqual(s[p + "fan1"].unit, "RPM")
        self.assertEqual(s[p + "fan2"].value, 0)
        self.assertAlmostEqual(s[p + "pwm1"].value, 128 * 100 / 255)
        self.assertEqual(s[p + "pwm2"].value, 100.0)
        self.assertIn("manual", s[p + "pwm2"].hint)
        self.assertIsNone(s[p + "temp13"].value)               # unreadable -> None, row kept
        self.assertEqual(s[p + "temp2"].label, "CPU socket")      # Nuvoton source name -> friendly
        self.assertIn("driver label 'CPUTIN'", s[p + "temp2"].hint)
        self.assertAlmostEqual(s[p + "power1"].value, 55.5)     # _input preferred over _average
        self.assertAlmostEqual(s[p + "curr1"].value, 12.5)
        self.assertTrue(s[p + "temp1"].group.endswith("/Super I/O NCT6799"))
        # Super I/O listed first
        first = next(iter(self.sensors().values()))
        self.assertIn("Super I/O", first.group)

    def _ite(self, name, files):
        hw = os.path.join(self.sys, "class/hwmon", f"hwmon7{len(name)}")
        os.makedirs(hw)
        for f, v in {"name": name, **files}.items():
            with open(os.path.join(hw, f), "w") as fh:
                fh.write(f"{v}\n")

    def test_gigabyte_x870e_map(self):
        # real BACONATOR3000 readings (X870E AORUS PRO, IT8696E + IT87952E, frankcrawford it87)
        self._ite("it8696_a008090a", {
            "in0_input": 1260, "in1_input": 2004, "in2_input": 2016, "in3_input": 2028, "in4_input": 1236,
            "in5_input": 1140, "in6_input": 1440, "in7_input": 3312, "in7_label": "3VSB", "in8_input": 3096,
            "in8_label": "Vbat", "in9_input": 3072, "in9_label": "+3.3V", "temp1_input": 29000,
            "temp2_input": 59000, "temp5_input": 42000, "temp6_input": -55000, "fan1_input": 1569,
            "fan6_input": 0, "pwm1": 130, "pwm6": 0})
        self._ite("it87952_a008090a", {"in0_input": 1815, "in2_input": 1837, "in4_input": 1804,
                                       "in5_input": 1056, "in7_input": 3410, "in7_label": "3VSB",
                                       "temp1_input": 34000, "temp3_input": 46000, "fan1_input": 1011})
        s = self.sensors()
        pre = lambda chip: next(k for k in s if k.startswith(f"board.{chip}_")).rsplit(".", 1)[0] + "."
        a, b = pre("it8696_a008090a"), pre("it87952_a008090a")
        self.assertEqual(s[a + "in0"].label, "Vcore")
        self.assertAlmostEqual(s[a + "in1"].value, 3.3046, places=3)
        self.assertEqual(s[a + "in2"].label, "+12V")
        self.assertAlmostEqual(s[a + "in2"].value, 12.096)
        self.assertAlmostEqual(s[a + "in3"].value, 5.07)
        self.assertIn("x 6 (board divider)", s[a + "in2"].hint)
        self.assertEqual(s[a + "in7"].label, "+3V Standby")
        self.assertAlmostEqual(s[a + "in7"].value, 3.312)
        self.assertEqual((s[a + "in8"].label, s[a + "in9"].label), ("CMOS Battery", "AVCC3"))
        self.assertEqual(s[a + "temp2"].label, "PCH (chipset)")
        self.assertEqual(s[a + "temp5"].label, "VRM MOS")
        self.assertIsNone(s[a + "temp6"].value)                        # open header
        self.assertIn("no probe", s[a + "temp6"].hint)
        self.assertEqual(s[a + "fan1"].label, "CPU Fan")
        self.assertEqual(s[a + "pwm1"].label, "CPU Fan duty")
        self.assertNotIn(a + "fan6", s)
        self.assertNotIn(a + "pwm6", s)
        self.assertEqual(s[b + "in2"].label, "PM VCC18")
        self.assertNotIn(b + "in0", s)
        self.assertNotIn(b + "in7", s)                                  # duplicate 3VSB hidden
        self.assertEqual(s[b + "fan1"].label, "System Fan 5 / Pump")
        self.assertEqual(s[b + "temp3"].label, "System 2")

    def test_wrong_divider_falls_back_to_raw(self):
        # same chip on an unknown Gigabyte board wired differently: +12V divider would give 4.2 V
        with open(os.path.join(self.sys, "class/dmi/id/board_name"), "w") as f:
            f.write("SOME FUTURE BOARD\n")
        self._ite("it8696", {"in1_input": 2004, "in2_input": 700, "in0_input": 1100})
        s = self.sensors()
        p = next(k for k in s if k.startswith("board.it8696_")).rsplit(".", 1)[0] + "."
        self.assertEqual(s[p + "in2"].label, "in2 (raw pin)")
        self.assertAlmostEqual(s[p + "in2"].value, 0.7)
        self.assertIn("didn't fit", s[p + "in2"].hint)
        self.assertAlmostEqual(s[p + "in1"].value, 3.3046, places=3)    # this one fits -> kept
        self.assertIn("vendor default", s[p + "in1"].hint)

    def test_map_lookup(self):
        from meat3.backends import board_maps as bm
        e = bm.lookup("nct6799", "ASUSTeK COMPUTER INC.", "ROG STRIX X870E-E GAMING WIFI")
        self.assertEqual(e["name"], "ASUS X870E (NCT6701D)")
        self.assertTrue(bm.describe("nct6799", "in9", None, e)["hidden"])
        self.assertEqual(bm.describe("nct6799", "in4", None, e)["mult"], 12.0)
        g = bm.lookup("nct6798", "ASUSTeK COMPUTER INC.", "TUF GAMING B650-PLUS")
        self.assertTrue(g["guess"])
        self.assertTrue(bm.describe("nct6798", "temp2", "CPUTIN", g)["hidden"])   # meaningless on ASUS
        self.assertIsNone(bm.lookup("nct6799", "Some Vendor", "X"))
        self.assertEqual(bm.describe("nct6799", "temp1", "SYSTIN", None)["label"], "Motherboard")
        self.assertTrue(bm.describe("nct6799", "temp9", "Virtual_TEMP", None)["hidden"])
        # it87 in3 already doubled by the driver when in3_label exists
        e = bm.lookup("it8696", "Gigabyte Technology Co., Ltd.", "X870E AORUS PRO")
        d = bm.describe("it8696", "in3", "+3.3V", e)              # AVCC on VIN3: driver label, x1
        self.assertEqual((d["label"], d["mult"]), ("+3.3V", 1.0))
        # IT87952E second chip on a board without its own entry: no Gigabyte main-chip rails
        self.assertIsNone(bm.lookup("it87952_a008090a", "Gigabyte Technology Co., Ltd.", "X670E AORUS MASTER"))
        self.assertTrue(bm.lookup("it8689", "Gigabyte Technology Co., Ltd.", "X570 AORUS ELITE")["guess"])
        self.assertTrue(bm.OPEN_PROBE(-55) and bm.OPEN_PROBE(127))
        self.assertFalse(bm.OPEN_PROBE(125))                       # a real hot VRM still shows
        a = bm.lookup("nct6799", "ASUSTeK COMPUTER INC.", "ROG STRIX X870E-E GAMING WIFI")
        self.assertTrue(bm.describe("nct6799", "temp2", "CPUTIN", a)["hidden"])
        self.assertFalse(bm.plausible("+12V", 4.0))
        self.assertTrue(bm.plausible("Vcore", 0.4))

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
