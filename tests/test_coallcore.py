"""--co-allcore: per-CCD CO sweep under all-core load, single-core check, crash recovery, limits restored."""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import test_cpubin as tc  # noqa: E402
from meat3 import coallcore, cpubin  # noqa: E402


class CoAllcoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        tc.make_sys(self.tmp)
        self.sys = cpubin.Sysfs(self.tmp)
        self.co = {k: 0 for k in tc.CORES}
        self.limits = []
        self.res = {"label": "P", "ccds": self.sys.ccds}

    def make(self, allcore_floor, single_floor):
        core_ccd = {k: c["ccd"] for c in self.sys.ccds for k in c["cores"]}
        core_of = {c: k for k, cl in tc.CORES.items() for c in cl}
        t = self

        class Smp:
            def __init__(self, cores): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def summary(self):
                # all-core clock rises 20 MHz per CO step of 5
                return {"ppt_max": 230, "tctl_max": 78, "vddcr_min": 1.2,
                        "ccd_clk": {str(n): 5200 - 4 * min(t.co[k] for k in c["cores"]) for n, c in enumerate(t.sys.ccds)}}

        def run_phase(ph, cpus, s):
            ok = all(self.co[k] >= allcore_floor[core_ccd[k]] for k in self.co)
            return (True, "ok") if ok else (False, "Hardware failure detected")

        def suite(cpu):
            k = core_of[cpu]
            return (True, "") if self.co[k] >= single_floor[k] else (False, "verify failed")
        return coallcore.CoAllcore(self.res, self.sys, lambda k, v: self.co.__setitem__(k, v), self.limits.append,
                                   {"ppt_limit": 350}, {"ppt_limit": 200}, run_phase, lambda: [], Smp, suite,
                                   lambda: None, step=5, floor=-40, phase_s=1, log=lambda *a: None)

    def test_sweep_and_single_core_check(self):
        # all-core: CCD0 holds -25, CCD1 -30; single-core: core 3 (CCD1) only to -20
        t = self.make({0: -25, 1: -30}, {0: -40, 1: -40, 2: -40, 3: -20})
        t.run()
        r = self.res["map"]["co_allcore"]
        self.assertEqual(r["ccds"]["0"]["fail"], -30)
        self.assertEqual(r["ccds"]["1"]["fail"], -35)
        self.assertEqual(r["result"], {"0": -25, "1": -20})            # CCD1: -30 -> -25 -> -20 after core 3
        self.assertEqual([f["co"] for f in r["ccds"]["1"]["check_fails"]], [-30, -25])
        self.assertEqual(self.limits, [{"ppt_limit": 350}, {"ppt_limit": 200}])   # raised, then put back
        self.assertEqual(r["baseline"]["load"]["ycruncher"]["ccd_clk"], {"0": 5200, "1": 5200})
        text = "\n".join(coallcore.report_lines(self.res, self.sys.ccds[0]))
        self.assertIn("CCD0 CO -25", text)
        self.assertIn("5200 MHz at CO 0 -> 5300 MHz tuned", text)

    def test_section_created_by_caller(self):
        self.res["map"] = {"co_allcore": {"stock_limits": {"ppt_limit": 200}}}   # what run_co_allcore does first
        t = self.make({0: -10, 1: -10}, {k: -40 for k in range(4)})
        t.run()
        self.assertEqual(self.res["map"]["co_allcore"]["result"], {"0": -10, "1": -10})

    def test_crash_recovery(self):
        t = self.make({0: -99, 1: -99}, {k: -99 for k in range(4)})
        self.res["map"] = {"co_allcore": {"ccds": {"0": {"passes": [{"co": -20}], "co": -20}}, "steps": [], "check": {},
                                          "current": {"ccd": 0, "co": -25, "phase": "p95-avx2"}}}
        t = self.make({0: -99, 1: -99}, {k: -99 for k in range(4)})
        self.assertIn("CCD0 CO -25", t.recover())
        self.assertEqual(self.res["map"]["co_allcore"]["ccds"]["0"]["fail"], -25)
        self.res["map"]["co_allcore"]["current"] = {"ccd": 1, "co": -30, "check_core": 3, "phase": "single-core check"}
        self.res["map"]["co_allcore"]["ccds"]["1"] = {"co": -30}
        t.recover()
        self.assertEqual(self.res["map"]["co_allcore"]["ccds"]["1"]["co"], -25)


class MarkerTest(unittest.TestCase):
    def test_clear_markers(self):
        res = {"map": {"current_av": {"core": "1"}, "co_allcore": {"current": {"ccd": 0, "co": -5}},
                       "allcore": {"1.250V": {"current": {"ccd": 1}}}, "validate": {"1.250V manual": {"cores": {}}}}}
        self.assertEqual(sorted(cpubin.clear_markers(res)), ["allcore 1.250V", "co_allcore", "current_av"])
        self.assertNotIn("current", res["map"]["co_allcore"])
        self.assertEqual(cpubin.clear_markers(res), [])


if __name__ == "__main__":
    unittest.main()
