"""meat3-bin: CCD detection, measurement plumbing, V/F sweep, frequency-limit restore, compare verdicts."""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from meat3 import cpubin  # noqa: E402

# 9950X3D-like: 2 CCDs x 2 cores x 2 threads (cpu k and k+4 are siblings, like Linux numbers them)
CORES = {0: (0, 4), 1: (1, 5), 2: (2, 6), 3: (3, 7)}
CCD_OF_CORE = {0: 0, 1: 0, 2: 1, 3: 1}
L3 = {0: "98304K", 1: "32768K"}                 # CCD0 = V-Cache
MAX_KHZ = 5700000


def w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(text))


def make_sys(root):
    base = os.path.join(root, "devices/system/cpu")
    w(os.path.join(base, "online"), "0-7")
    for k, cpus in CORES.items():
        for c in cpus:
            d = os.path.join(base, f"cpu{c}")
            w(os.path.join(d, "topology/core_id"), k)
            w(os.path.join(d, "topology/die_id"), 0)
            w(os.path.join(d, "topology/physical_package_id"), 0)
            w(os.path.join(d, "cache/index3/id"), CCD_OF_CORE[k])
            w(os.path.join(d, "cache/index3/size"), L3[CCD_OF_CORE[k]])
            w(os.path.join(d, "cpufreq/cpuinfo_max_freq"), MAX_KHZ)
            w(os.path.join(d, "cpufreq/cpuinfo_min_freq"), 600000)
            w(os.path.join(d, "cpufreq/scaling_max_freq"), MAX_KHZ)
    w(os.path.join(root, "class/dmi/id/bios_version"), "F13c")
    w(os.path.join(root, "class/dmi/id/board_name"), "X870E TEST")


class FakeChip:
    """Clock follows the frequency limit up to the core's fused boost; voltage follows the clock.
    `offset_mv` makes one chip's silicon better or worse."""

    def __init__(self, root, offset_mv=0.0, fixed_mhz=None):
        self.root, self.offset, self.fixed = root, offset_mv / 1000, fixed_mhz
        self.loaded: list[int] = []
        self.fmax = {0: 5250, 1: 5200, 2: 5700, 3: 5650}        # V-Cache CCD boosts lower

    def load(self, cpus):
        self.loaded = list(cpus)
        chip = self

        class L:
            def stop(self_):
                chip.loaded = []
        return L()

    def cap_mhz(self, c):
        with open(os.path.join(self.root, f"devices/system/cpu/cpu{c}/cpufreq/scaling_max_freq")) as f:
            return int(f.read()) / 1000 * 0.9                    # amd-pstate scale isn't real MHz

    def sensors(self):
        s = {}
        for k, cpus in CORES.items():
            busy = any(c in self.loaded for c in cpus)
            clk = min(self.cap_mhz(cpus[0]), self.fmax[k]) if busy else 600.0
            if busy and len(self.loaded) > 2:
                clk -= 150                                       # all-core load: a bit lower
            if busy and self.fixed:
                clk = self.fixed                                 # BIOS multiplier: limits are ignored
            volt = 0.6 + clk * 0.0001 + self.offset + (0.02 if CCD_OF_CORE[k] == 0 else 0)
            s[f"cpu0.core{k}.clk_eff"] = {"value": clk}
            s[f"cpu0.pmt.core{k}.volt"] = {"value": volt}
            s[f"cpu0.core{k}.power"] = {"value": 10.0 if busy else 0.5}
            s[f"cpu0.pmt.core{k}.temp"] = {"value": 70.0 if busy else 40.0}
            s[f"cpu0.pmt.core{k}.fmax"] = {"value": self.fmax[k]}
            s[f"cpu0.core{k}.cppc_rank"] = {"value": 200 - k}
        s["cpu0.k10temp.temp1"] = {"value": 72.0}
        s["cpu0.clk.base"] = {"value": self.fixed or 4300.0}       # TSC = P0 clock
        return s

    def controls(self):
        return {f"cpu0.smu.co_core{k}": {"value": 0} for k in CORES}


class BinTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def chip(self, name, offset_mv, fixed_mhz=None):
        root = os.path.join(self.tmp, name)
        make_sys(root)
        fake = FakeChip(root, offset_mv, fixed_mhz)
        sysfs = cpubin.Sysfs(root)
        b = cpubin.Binner(sysfs, fake, load=fake.load, sleep=lambda s: None, settle_s=0, sample_s=3,
                          log=lambda *a: None)
        res = {"label": name, "when": "now", "cpu": {"model": "test"}, "system": sysfs.ident(), "ccds": sysfs.ccds}
        res["fused"] = b.fused()
        res["boost"] = {"stock": b.boost()}
        res["vf"] = b.vf(points=5)
        return res, root

    def test_ccds_and_roles(self):
        make_sys(self.tmp)
        s = cpubin.Sysfs(self.tmp)
        self.assertEqual([(c["ccd"], c["cores"], c["l3_mb"], c["role"]) for c in s.ccds],
                         [(0, [0, 1], 96.0, "V-Cache"), (1, [2, 3], 32.0, "frequency")])

    def test_measurements_and_limits_restored(self):
        res, root = self.chip("A", 0)
        self.assertEqual(res["fused"]["cores"]["0"]["fmax_mhz"], 5250)
        self.assertEqual(res["boost"]["stock"]["single"]["2"]["clk"], 5130)       # min(5700*0.9, 5700)
        self.assertEqual(res["boost"]["stock"]["single"]["0"]["clk"], 5130)       # capped at 5130 < 5250
        allc = res["boost"]["stock"]["ccd"]["1"]
        self.assertEqual(allc["power"], 20.0)                                     # sum over the CCD's cores
        vf = res["vf"]["0"]
        self.assertEqual(vf["core"], 0)                                           # best CPPC rank on CCD0
        clks = [p["clk"] for p in vf["single"]]
        self.assertEqual(clks, sorted(clks, reverse=True))                       # limit lowered step by step
        self.assertLess(clks[-1], clks[0] * 0.7)
        for c in range(8):                                                        # limits back to stock
            with open(os.path.join(root, f"devices/system/cpu/cpu{c}/cpufreq/scaling_max_freq")) as f:
                self.assertEqual(int(f.read()), MAX_KHZ)
        text = cpubin.report(res)
        self.assertIn("CCD0 (V-Cache)", text)
        self.assertIn("CCD1 (frequency)", text)

    def test_compare_picks_lower_voltage_chip_per_ccd(self):
        a, _ = self.chip("A", 0)
        b, _ = self.chip("B", -30)                    # B needs 30 mV less everywhere
        out = cpubin.compare(a, b)
        self.assertIn("V-Cache CCD: B is better", out)
        self.assertIn("frequency CCD: B is better", out)
        self.assertIn("B -30 mV vs A", out)
        same = cpubin.compare(a, a)
        self.assertIn("too close to call", same)

    def test_co_counts_in_the_verdict(self):
        a, _ = self.chip("A", 0)
        b, _ = self.chip("B", 0)
        a["co"] = {"0": {"result": -25}, "1": {"result": -20}, "2": {"result": -5}, "3": {"result": -5}}
        b["co"] = {"0": {"result": -10}, "1": {"result": -10}, "2": {"result": -20}, "3": {"result": -25}}
        out = cpubin.compare(a, b)
        self.assertIn("V-Cache CCD: A is better (Curve Optimizer)", out)
        self.assertIn("frequency CCD: B is better (Curve Optimizer)", out)

    def test_fixed_bios_multiplier_is_detected(self):
        # what BACONATOR does: every core pinned at 5389 MHz, TSC at the same rate, limits ignored
        res, root = self.chip("F", 0, fixed_mhz=5389)
        self.assertTrue(res["fused"]["fixed_ratio"])
        self.assertIn("fixed", res["vf"]["0"]["note"])
        self.assertEqual(len(res["vf"]["0"]["single"]), 1)                       # stopped after 2 points
        text = cpubin.report(res)
        self.assertIn("fixed BIOS multiplier", text)
        self.assertIn("not measured", text)
        ok, _ = self.chip("A", 0)
        self.assertFalse(ok["fused"]["fixed_ratio"])
        self.assertNotIn("note", ok["vf"]["0"])
        self.assertIn("F was measured on a fixed BIOS multiplier", cpubin.compare(ok, res))
        with open(os.path.join(root, "devices/system/cpu/cpu0/cpufreq/scaling_max_freq")) as f:
            self.assertEqual(int(f.read()), MAX_KHZ)

    def test_volt_at_and_labels(self):
        curve = [{"clk": 3000, "volt": 0.9}, {"clk": 5000, "volt": 1.3}]
        self.assertAlmostEqual(cpubin.volt_at(curve, 4000), 1.1)
        self.assertIsNone(cpubin.volt_at(curve, 5500))
        self.assertRaises(ValueError, cpubin.path_for, "../x")
        self.assertTrue(cpubin.path_for("chip-A_1", "/s").endswith("/s/chip-A_1.json"))

    def test_store_roundtrip(self):
        res, _ = self.chip("A", 0)
        cpubin.save(res, self.tmp)
        self.assertEqual(cpubin.load("A", self.tmp)["vf"], res["vf"])


if __name__ == "__main__":
    unittest.main()


class MapTest(unittest.TestCase):
    """Static V/F map on the fake chip: CO lowers voltage ~4 mV/step; each core is stable down to its own limit."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def run_map(self, name, limits, offset_mv=0.0, clocks="4500,max", swap_co=False):
        from meat3 import cpumap
        root = os.path.join(self.tmp, name)
        make_sys(root)
        fake = FakeChip(root, offset_mv)
        fake.co = {k: 0 for k in CORES}
        base_sensors = fake.sensors

        def sensors():
            s = base_sensors()
            for k in CORES:
                s[f"cpu0.pmt.core{k}.volt"]["value"] += 0.004 * fake.co[k]
            return s
        fake.sensors = sensors
        sysfs = cpubin.Sysfs(root)
        b = cpubin.Binner(sysfs, fake, load=fake.load, sleep=lambda s: None, settle_s=0, sample_s=2,
                          log=lambda *a: None)
        res = {"label": name, "ccds": sysfs.ccds}
        core_of = {c: k for k, cpus in CORES.items() for c in cpus}

        def apply_co(k, v):
            fake.co[(k + 2) % 4 if swap_co else k] = v     # swap_co: a chip where CO numbering is off

        def suite(cpu):
            k = core_of[cpu]
            return (True, "") if fake.co[k] >= limits[k] else (False, "verify failed")
        saves = []
        mp = cpumap.Mapper(b, res, apply_co, suite, lambda: saves.append(1), step=5, floor=-40, log=lambda *a: None)
        ok, why = mp.check_mapping([0, 2])
        if not ok:
            return res, why, root
        mp.run(list(CORES), cpumap.parse_clocks(clocks))
        return res, "", root

    def test_map_finds_each_cores_limit(self):
        res, err, root = self.run_map("A", {0: -20, 1: -10, 2: -30, 3: -15})
        self.assertEqual(err, "")
        from meat3 import cpumap
        r = cpumap.point_result(res["map"]["points"]["0@4500"])
        self.assertEqual(r["co"], -20)
        self.assertAlmostEqual(r["margin_mv"], 80, delta=1)            # 20 steps-of-CO x 4 mV
        self.assertEqual(res["map"]["points"]["1@4500"]["fail"], -15)
        self.assertAlmostEqual(r["clk"], 4500, delta=25)                 # calibrated to the real clock
        m = cpumap.ccd_map(res, res["ccds"][1])
        self.assertEqual(list(m), [4500, "max"])
        self.assertAlmostEqual(m[4500]["worst_margin"], 60, delta=1)     # core 3: -15
        self.assertIn("static V/F map", cpubin.report(res))
        for c in range(8):                                               # limits restored
            with open(os.path.join(root, f"devices/system/cpu/cpu{c}/cpufreq/scaling_max_freq")) as f:
                self.assertEqual(int(f.read()), MAX_KHZ)

    def test_other_cores_held_low_while_mapping(self):
        from meat3 import cpumap
        root = os.path.join(self.tmp, "H")
        make_sys(root)
        fake = FakeChip(root)
        sysfs = cpubin.Sysfs(root)
        b = cpubin.Binner(sysfs, fake, load=fake.load, sleep=lambda s: None, settle_s=0, sample_s=2,
                          log=lambda *a: None)
        mp = cpumap.Mapper(b, {"label": "H"}, lambda k, v: None, lambda cpu: (True, ""), lambda: None)
        mp.calibrate(2, 4500)
        caps = {}
        for c in range(8):
            with open(os.path.join(root, f"devices/system/cpu/cpu{c}/cpufreq/scaling_max_freq")) as f:
                caps[c] = int(f.read())
        self.assertEqual({caps[c] for c in (0, 1, 3, 4, 5, 7)}, {int(MAX_KHZ * cpumap.OTHERS_FRAC)})
        self.assertGreater(caps[2], caps[0])                             # core 2 (cpus 2, 6) at its target
        self.assertEqual(caps[2], caps[6])

    def test_floor_marked_and_crash_recovery(self):
        from meat3 import cpumap
        res, _, _ = self.run_map("A", {0: -99, 1: -99, 2: -99, 3: -99}, clocks="4500")
        r = cpumap.point_result(res["map"]["points"]["0@4500"])
        self.assertTrue(r["floor_hit"])
        self.assertIn("still stable at the CO floor", cpubin.report(res))
        # a hang mid-step: the next run marks that step unstable
        res["map"]["current"] = {"key": "2@4500", "co": -25, "since": 0}
        res["map"]["points"]["2@4500"] = {"passes": [{"co": 0, "volt": 1.0, "clk": 4500}]}
        mp = cpumap.Mapper(None, res, None, None, lambda: None)
        self.assertIn("marked unstable", mp.recover())
        self.assertEqual(res["map"]["points"]["2@4500"]["fail"], -25)
        self.assertNotIn("current", res["map"])

    def test_mapping_check_catches_wrong_numbering(self):
        _, err, _ = self.run_map("A", {k: -30 for k in CORES}, swap_co=True)
        self.assertIn("numbering may not match", err)

    def test_compare_uses_stable_voltage(self):
        a, _, _ = self.run_map("A", {0: -20, 1: -20, 2: -20, 3: -20})
        b, _, _ = self.run_map("B", {0: -20, 1: -20, 2: -20, 3: -20}, offset_mv=-25)
        a["label"], b["label"] = "A", "B"
        out = cpubin.compare(a, b)
        self.assertIn("stable V 4500, mean core", out)
        self.assertIn("V-Cache CCD: B is better (stable voltage 25 mV lower)", out)


class AtVoltageTest(unittest.TestCase):
    """Highest stable clock per core at a voltage cap, on the fake chip."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def run_av(self, name, maxclk, volt, offset_mv=0.0, mv_per_co=4.0):
        from meat3 import cpumap
        root = os.path.join(self.tmp, name)
        make_sys(root)
        fake = FakeChip(root, offset_mv)
        fake.co = {k: 0 for k in CORES}
        base = fake.sensors

        def sensors():
            s = base()
            for k in CORES:
                s[f"cpu0.pmt.core{k}.volt"]["value"] += mv_per_co / 1000 * fake.co[k]
            return s
        fake.sensors = sensors
        sysfs = cpubin.Sysfs(root)
        b = cpubin.Binner(sysfs, fake, load=fake.load, sleep=lambda s: None, settle_s=0, sample_s=2,
                          log=lambda *a: None)
        res = {"label": name, "ccds": sysfs.ccds}
        core_of = {c: k for k, cpus in CORES.items() for c in cpus}

        def apply_co(k, v):
            fake.co[k] = v

        def suite(cpu):
            k = core_of[cpu]
            clk = min(fake.cap_mhz(cpu), fake.fmax[k])                   # the clock it runs under load
            return (True, "") if clk <= maxclk[k] + 30 else (False, "verify failed")
        mp = cpumap.Mapper(b, res, apply_co, suite, lambda: None, log=lambda *a: None)
        mp.run_at_voltage(list(CORES), volt, 4500, 100)
        return res, root, fake

    def test_clock_limits_and_ceiling(self):
        from meat3 import cpumap
        # fused ceilings in the fake: 5250/5200 (V-Cache), 5700/5650; amd-pstate scale 0.9 caps at 5130 MHz
        res, root, fake = self.run_av("A", {0: 4800, 1: 4940, 2: 5200, 3: 9999}, 1.20)
        av = res["map"]["at_voltage"]["1.200V"]
        self.assertEqual(cpumap.at_voltage_result(av["0"])["target"], 4800)
        self.assertEqual(cpumap.at_voltage_result(av["1"])["target"], 4950)      # 4900 pass, 5000 fail, 4950 pass
        self.assertEqual(av["1"]["fail"], 5000)
        r3 = cpumap.at_voltage_result(av["3"])
        self.assertEqual(r3["limit"], "ceiling")                                # never failed: hit the clock cap
        for k in CORES:
            for p in av[str(k)]["passes"]:
                self.assertLessEqual(p["volt"], 1.202)                          # always within the voltage budget
        self.assertEqual(fake.co, {k: 0 for k in CORES})                         # CO back to 0
        text = cpubin.report(res)
        self.assertIn("highest stable clock at <= 1.200V", text)
        self.assertIn("hit the clock ceiling", text)

    def test_voltage_budget_needs_co(self):
        from meat3 import cpumap
        # at 1.05 V the fake needs negative CO above ~4400 MHz; past CO -50 it stops with "CO limit"
        res, _, _ = self.run_av("A", {k: 9999 for k in CORES}, 1.05, mv_per_co=1.0)
        av = res["map"]["at_voltage"]["1.050V"]
        r = cpumap.at_voltage_result(av["2"])
        self.assertLess(r["co"], 0)
        self.assertEqual(r["limit"], "CO limit")

    def test_compare_on_clock_at_voltage(self):
        a, _, _ = self.run_av("A", {0: 4600, 1: 4600, 2: 4700, 3: 4700}, 1.20)
        b, _, _ = self.run_av("B", {0: 4900, 1: 4900, 2: 4700, 3: 4700}, 1.20)
        out = cpubin.compare(a, b)
        self.assertIn("clock at 1.200V, mean core", out)
        self.assertIn("V-Cache CCD: B is better (300 MHz more at the same voltage)", out)
        self.assertIn("frequency CCD: too close to call", out)

    def test_crash_recovery(self):
        from meat3 import cpumap
        res = {"map": {"current_av": {"tag": "1.250V", "core": "3", "target": 5600, "co": -20},
                       "at_voltage": {"1.250V": {"3": {"passes": [{"target": 5500, "co": -15}],
                                                        "current": {"target": 5600}}}}}}
        mp = cpumap.Mapper(None, res, None, None, lambda: None)
        self.assertIn("marked unstable", mp.recover_at_voltage())
        self.assertEqual(res["map"]["at_voltage"]["1.250V"]["3"]["fail"], 5600)


class FixedVoltageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def make(self, rail_v):
        from meat3 import cpumap
        make_sys(self.tmp)
        fake = FakeChip(self.tmp)
        base = fake.sensors

        def sensors():                                   # BIOS fixed Vcore: every core reads the rail
            s = base()
            for k in CORES:
                s[f"cpu0.pmt.core{k}.volt"]["value"] = rail_v
            return s
        fake.sensors = sensors
        b = cpubin.Binner(cpubin.Sysfs(self.tmp), fake, load=fake.load, sleep=lambda s: None, settle_s=0,
                          sample_s=2, log=lambda *a: None)
        co_calls = []
        core_of = {c: k for k, cpus in CORES.items() for c in cpus}
        suite = lambda cpu: (True, "") if min(fake.cap_mhz(cpu), fake.fmax[core_of[cpu]]) <= 4900 + 30 else (False, "x")
        res = {"label": "F", "ccds": b.sys.ccds}
        return cpumap.Mapper(b, res, lambda k, v: co_calls.append((k, v)), suite, lambda: None,
                             log=lambda *a: None), res, co_calls

    def test_fixed_voltage_never_touches_co(self):
        from meat3 import cpumap
        mp, res, co_calls = self.make(1.248)
        mp.run_at_voltage([0, 2], 1.25, 4500, 100, fixed=True)
        av = res["map"]["at_voltage"]["1.250V fixed"]
        self.assertEqual(cpumap.at_voltage_result(av["0"])["target"], 4900)
        self.assertTrue(all(v == 0 for _, v in co_calls))                  # only "back to 0"
        self.assertEqual(av["2"]["passes"][0]["volt"], 1.248)

    def test_fixed_voltage_wrong_reading_stops(self):
        mp, _, _ = self.make(1.33)                                           # BIOS setting didn't take
        with self.assertRaises(RuntimeError) as e:
            mp.run_at_voltage([0], 1.25, 4500, 100, fixed=True)
        self.assertIn("check the Vcore setting", str(e.exception))
