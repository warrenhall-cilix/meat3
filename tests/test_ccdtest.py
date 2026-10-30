"""--allcore: per-CCD all-core clock at a fixed voltage, whole-chip stage, crash and thermal handling."""
import contextlib
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import test_cpubin as tc  # noqa: E402
from meat3 import ccdtest, cpubin  # noqa: E402


class FakeSampler:
    def __init__(self, cores): self.cores = cores
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def summary(self): return {"vddcr_min": 1.13, "tctl_max": 80.0, "ppt_max": 150.0, "clk_mean": None}


class CcdTestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        tc.make_sys(self.tmp)
        self.sys = cpubin.Sysfs(self.tmp)
        self.clk = {k: 3000 for k in tc.CORES}
        self.on = False
        t = self

        class Oc:
            def mode(self, on): t.on = on
            def vid(self, v): t.vid = v
            def freq_all(self, f): t.clk.update({k: f for k in t.clk})
            def freq_core(self, k, f): t.clk[k] = f
        self.oc = Oc()
        self.res = {"label": "C", "ccds": self.sys.ccds}
        self.ran = []

    def make(self, ccd_limit, chip_limit=None, guard_trip_above=None):
        core_ccd = {k: c["ccd"] for c in self.sys.ccds for k in c["cores"]}

        def run_phase(ph, cpus, secs):
            self.ran.append((ph, len(cpus), dict(self.clk)))
            if guard_trip_above and max(self.clk.values()) > guard_trip_above:
                self.on = False                                # the daemon's overtemp guard left manual mode
                return True, "ok"
            if len(cpus) == 8:                                  # whole chip (fake: 4 cores x 2 threads)
                return (True, "ok") if chip_limit(self.clk) else (False, "FATAL ERROR")
            ccd = core_ccd[next(k for k, cl in tc.CORES.items() if cpus[0] in cl)]
            f = max(self.clk[k] for k in self.sys.ccds[ccd]["cores"])
            return (True, "ok") if f <= ccd_limit[ccd] else (False, "Hardware failure detected")
        measure = lambda cores, cpus: {"clk": sum(self.clk[k] for k in cores) / len(cores) - 10}
        return ccdtest.CcdTest(self.res, 1.25, self.sys, self.oc, lambda: self.on, run_phase, lambda: [],
                               measure, FakeSampler, lambda: None, start=5000, step=100, phase_s=1, chip_s=1,
                               log=lambda *a: None)

    def test_per_ccd_then_chip(self):
        t = self.make({0: 5230, 1: 5310}, chip_limit=lambda c: max(c.values()) <= 5260)
        t.run(self.sys.ccds)
        r = self.res["map"]["allcore"]["1.250V"]
        # 5000,5100,5200 pass, 5300 fail, 5250 fail -> 5200; CCD1: 5300 pass, 5400 fail, 5350 fail -> 5300
        self.assertEqual(r["ccds"]["0"]["result_mhz"], 5200)
        self.assertEqual(r["ccds"]["1"]["result_mhz"], 5300)
        self.assertEqual(r["chip"]["result"], {"0": 5200, "1": 5250})          # chip: faster CCD stepped down
        self.assertFalse(self.on)                                               # always leaves manual mode
        self.assertEqual(r["ccds"]["0"]["passes"][-1]["vddcr_min"], 1.13)
        # the CCD under test runs every phase on all its threads, the other CCD sits at 3000
        p = [x for x in self.ran if x[1] == 4 and x[2][0] == 5100]
        self.assertEqual([x[0] for x in p], ccdtest.CCD_PHASES)
        self.assertEqual({x[2][2] for x in p}, {3000})
        self.assertIn("all-core at 1.250V: 5200 MHz", "\n".join(ccdtest.report_lines(self.res, self.sys.ccds[0])))

    def test_guard_trip_is_thermal_not_pass(self):
        t = self.make({0: 9999, 1: 9999}, chip_limit=lambda c: True, guard_trip_above=5150)
        t.run(self.sys.ccds[:1])
        st = self.res["map"]["allcore"]["1.250V"]["ccds"]["0"]
        self.assertEqual(st["result_mhz"], 5100)
        self.assertTrue(st["thermal"])
        self.assertEqual(st["fail"], 5200)

    def test_crash_recovery(self):
        t = self.make({0: 9999, 1: 9999})
        self.res["map"] = {"allcore": {"1.250V": {"ccds": {"1": {"passes": [{"mhz": 5300}]}}, "chip": {},
                                                  "current": {"ccd": 1, "mhz": 5400, "phase": "p95-avx512"}}}}
        t = self.make({0: 9999, 1: 9999})
        self.assertIn("CCD1 at 5400 MHz", t.recover())
        self.assertEqual(self.res["map"]["allcore"]["1.250V"]["ccds"]["1"]["fail"], 5400)

    def test_compare_weighs_allcore(self):
        a = {"label": "A", "ccds": self.sys.ccds, "map": {"allcore": {"1.250V": {"ccds": {"0": {"result_mhz": 5200}, "1": {"result_mhz": 5250}}}}}}
        b = {"label": "B", "ccds": self.sys.ccds, "map": {"allcore": {"1.250V": {"ccds": {"0": {"result_mhz": 5300}, "1": {"result_mhz": 5250}}}}}}
        out = cpubin.compare(a, b)
        self.assertIn("all cores loaded at 1.250V", out)
        self.assertIn("V-Cache CCD: B is better (100 MHz more all-core at the same voltage)", out)
        self.assertIn("frequency CCD: too close to call", out)


if __name__ == "__main__":
    unittest.main()
