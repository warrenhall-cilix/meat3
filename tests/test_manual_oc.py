"""Manual overclocking: SMU encodings, gating, controls, the --oc-probe flow and the manual at-voltage test."""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from meat3.backends import amd_smu  # noqa: E402
from meat3 import ocprobe  # noqa: E402


class FakeSmu:
    """Records commands; behaves like a chip whose VID encoding is SVI3 (or SVI2 with svi2=True)."""

    def __init__(self, svi2=False, fw="98.84.0", per_core=True, enable_vid=None):
        self.sent, self.svi2, self.fw, self.per_core = [], svi2, fw, per_core
        self.oc = False
        self.vid_v = 0.90
        self.clk = {k: 3500.0 for k in range(16)}
        self.enable_vid = enable_vid
        self.state = os.path.join(tempfile.mkdtemp(), "oc_mode")
        self.oc_state_file = self.state
        self.oc_cache = {}

    # RyzenSmu API used by the code under test
    def fw_version(self):
        return self.fw

    def oc_active(self):
        return os.path.exists(self.state)

    def set_oc_active(self, on):
        amd_smu.RyzenSmu.set_oc_active(self, on)

    def codename(self):
        return "GraniteRidge"

    def send(self, name, args):
        self.sent.append((name, list(args)))
        a0 = args[0] if args else 0
        if name == "oc_enable":
            self.oc = True
            if self.enable_vid:
                self.vid_v = self.enable_vid
        elif name == "oc_disable":
            self.oc = False
            self.vid_v = 0.90
            self.clk = {k: 3500.0 for k in range(16)}
        elif name == "oc_freq_all":
            self.clk = {k: float(a0 & 0xFFFFF) for k in range(16)}
        elif name == "oc_freq_core":
            slot = ((a0 >> 28) & 0xF) * 8 + ((a0 >> 20) & 0xF)
            if self.per_core:
                self.clk[slot] = float(a0 & 0xFFFFF)
        elif name == "oc_vid":
            self.vid_v = (1.55 - a0 * 0.00625) if self.svi2 else (0.245 + a0 * 0.005)
        return amd_smu.SMU_OK, [0x5 if name == "oc_caps" else 0] * 6

    def sensors(self):
        s = {"cpu0.pmt.svi3_vdd_vid": self.vid_v, "cpu0.pmt.vid_peak": 1.33, "cpu0.pmt.vddcr_cpu": self.vid_v - 0.01,
             "cpu0.k10temp.temp1": 45.0, "cpu0.util.total": 2.0}
        for k, c in self.clk.items():
            s[f"cpu0.pmt.core{k}.clk"] = c
        return s


class EncodingTest(unittest.TestCase):
    def test_svi3_and_masks(self):
        self.assertEqual(amd_smu.vid_svi3(1.25), 201)
        self.assertEqual(amd_smu.vid_svi3_volts(201), 1.25)
        self.assertEqual(amd_smu.vid_svi3(1.10), 171)
        self.assertEqual(amd_smu.vid_svi3(0.1), 0)
        self.assertEqual(amd_smu.vid_svi3(9.9), 0xFF)
        self.assertEqual(amd_smu.oc_core_mask(9), (1 << 28) | (1 << 20))
        # the safety argument: the probe's SVI3 value read as SVI2 is a *low* voltage
        self.assertLess(1.55 - amd_smu.vid_svi3(ocprobe.PROBE_V) * 0.00625, 0.6)


class ControlsTest(unittest.TestCase):
    def setUp(self):
        self.smu = FakeSmu()
        self.cfg = {"manual_oc": {"vid_encoding": None, "max_vid": 1.40}}
        p = mock.patch("meat3.core.config.get", lambda: self.cfg)
        p.start()
        self.addCleanup(p.stop)
        g = mock.patch.object(amd_smu, "_gate_reason", lambda smu: "")
        g.start()
        self.addCleanup(g.stop)

    def apply(self, key, v):
        return amd_smu.smu_apply(self.smu, None, "cpu0", f"cpu0.smu.{key}", v, 16)

    def ctl(self):
        return {c.id: c for c in amd_smu.oc_controls(self.smu, None, "cpu0", "CPU", 16, "")}

    def test_needs_manual_mode_and_verified_encoding(self):
        self.assertFalse(self.apply("oc_freq_all", 5000)[0])                 # not in manual mode
        self.assertTrue(self.apply("oc_mode", True)[0])
        self.assertTrue(self.smu.oc_active())
        ok, msg = self.apply("oc_vid", 1.25)
        self.assertFalse(ok)
        self.assertIn("--oc-probe", msg)
        self.assertFalse(self.ctl()["cpu0.smu.oc_vid"].available)
        self.cfg["manual_oc"].update(vid_encoding="svi3", verified_smu="98.84.0")
        ok, msg = self.apply("oc_vid", 1.25)
        self.assertTrue(ok, msg)
        self.assertEqual(self.smu.sent[-1], ("oc_vid", [201]))
        self.assertEqual(self.ctl()["cpu0.smu.oc_vid"].value, 1.25)
        self.assertFalse(self.apply("oc_vid", 1.41)[0])                       # above max_vid
        self.cfg["manual_oc"]["max_vid"] = 2.0
        self.assertFalse(self.apply("oc_vid", 1.46)[0])                       # hard cap 1.45
        self.smu.fw = "98.90.0"                                               # firmware changed
        self.assertIn("again", self.apply("oc_vid", 1.2)[1])

    def test_frequencies_and_leaving(self):
        self.apply("oc_mode", True)
        self.assertTrue(self.apply("oc_freq_all", 3000)[0])
        self.assertTrue(self.apply("oc_freq_core9", 5400)[0])
        self.assertEqual(self.smu.sent[-1], ("oc_freq_core", [(1 << 28) | (1 << 20) | 5400]))
        self.assertEqual(self.ctl()["cpu0.smu.oc_freq_core9"].value, 5400)
        self.assertFalse(self.apply("oc_freq_core9", 7000)[0])
        self.assertFalse(self.apply("oc_freq_core99", 5000)[0])
        self.assertTrue(self.apply("oc_mode", False)[0])
        self.assertFalse(self.smu.oc_active())
        n = len(self.smu.sent)
        ok, msg = self.apply("oc_mode", 0)                                    # guard firing while off: no SMU call
        self.assertTrue(ok)
        self.assertEqual(len(self.smu.sent), n)

    def test_guard_targets_manual_mode(self):
        from meat3.core import config
        g = next(g for g in config.DEFAULTS["guards"] if g["name"] == "Manual CPU overclock overtemp")
        self.assertEqual((g["control"], g["value"], g["enabled"]), ("cpu*.smu.oc_mode", 0, True))


class ProbeTest(unittest.TestCase):
    def probe(self, smu):
        steps = []
        p = ocprobe.Probe(smu, smu.sensors, list(range(16)), sleep=lambda s: None, log=lambda *a: None,
                          marker=steps.append)
        return p.run(), steps

    def test_svi3_chip_passes(self):
        smu = FakeSmu()
        res, steps = self.probe(smu)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["per_core_ok"])
        self.assertEqual(steps, ["enable", "freq", "vid", "disable"])
        self.assertEqual(smu.sent[-1][0], "oc_disable")
        self.assertFalse(smu.oc_active())
        self.assertEqual(("oc_vid", [171]), next(c for c in smu.sent if c[0] == "oc_vid"))

    def test_svi2_chip_fails_low_and_leaves_manual_mode(self):
        smu = FakeSmu(svi2=True)
        res, _ = self.probe(smu)
        self.assertFalse(res["ok"])
        self.assertLess(res["after_vid"]["vid"], 0.6)                         # wrong encoding = low, not high
        self.assertEqual(smu.sent[-1][0], "oc_disable")

    def test_enable_overvolting_aborts(self):
        smu = FakeSmu(enable_vid=1.50)
        with self.assertRaises(RuntimeError):
            self.probe(smu)
        self.assertEqual(smu.sent[-1][0], "oc_disable")
        self.assertNotIn("oc_vid", [c[0] for c in smu.sent])

    def test_busy_cpu_refused(self):
        smu = FakeSmu()
        smu_sensors = smu.sensors
        smu.sensors = lambda: dict(smu_sensors(), **{"cpu0.util.total": 60.0})
        res, _ = self.probe(smu)
        self.assertFalse(res["ok"])
        self.assertIn("busy", res["why"])
        self.assertEqual(smu.sent, [])


class ManualRunTest(unittest.TestCase):
    def test_per_core_clock_at_fixed_voltage(self):
        sys.path.insert(0, HERE)
        import test_cpubin as tc
        from meat3 import cpubin, cpumap
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        tc.make_sys(tmp)
        fake = tc.FakeChip(tmp)
        clk = {k: 3000 for k in tc.CORES}
        base = fake.sensors

        def sensors():
            s = base()
            for k in tc.CORES:
                busy = any(c in fake.loaded for c in tc.CORES[k])
                s[f"cpu0.core{k}.clk_eff"]["value"] = clk[k] if busy else 600.0
                s[f"cpu0.pmt.core{k}.volt"]["value"] = 1.25
            return s
        fake.sensors = sensors
        calls = []

        class Oc:
            def mode(self, on): calls.append(("mode", on))
            def vid(self, v): calls.append(("vid", v))
            def freq_all(self, f):
                calls.append(("all", f))
                clk.update({k: f for k in clk})
            def freq_core(self, k, f): clk[k] = f
        b = cpubin.Binner(cpubin.Sysfs(tmp), fake, load=fake.load, sleep=lambda s: None, settle_s=0, sample_s=2,
                          log=lambda *a: None)
        limits = {0: 5300, 1: 5450, 2: 5600, 3: 5550}
        core_of = {c: k for k, cpus in tc.CORES.items() for c in cpus}
        suite = lambda cpu: (True, "") if clk[core_of[cpu]] <= limits[core_of[cpu]] else (False, "verify failed")
        res = {"label": "M", "ccds": b.sys.ccds}
        mp = cpumap.Mapper(b, res, None, suite, lambda: None, log=lambda *a: None)
        mp.run_manual(list(tc.CORES), 1.25, Oc(), 5000, 100)
        av = res["map"]["at_voltage"]["1.250V manual"]
        best = {k: cpumap.at_voltage_result(av[str(k)])["target"] for k in tc.CORES}
        self.assertEqual(best, {0: 5300, 1: 5450, 2: 5600, 3: 5550})
        self.assertEqual(calls[0], ("mode", True))
        self.assertEqual(calls[-1], ("mode", False))                          # always leaves manual mode
        self.assertIn(("vid", 1.25), calls)
        self.assertEqual(set(clk.values()), {3000})                           # every core back to the low clock
        self.assertIn("1.250V manual", cpubin.report(res))


if __name__ == "__main__":
    unittest.main()
