import unittest

from meat3.backends import cpu_topdown as td


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class FakeCorePerf:
    """Raw core-PMU fake: per-(config, cpu) step per sample; optional multiplexing (running = half)."""

    def __init__(self, steps, mux=False, fail_cpu=None):
        self.steps, self.mux, self.fail_cpu = steps, mux, fail_cpu
        self.fd = {}
        self.vals = {}
        self.closed = []

    def open(self, typ, config, cpu, read_format=0):
        assert typ == td.PERF_TYPE_RAW and read_format == 3
        if cpu == self.fail_cpu:
            raise OSError(13, "Permission denied")
        fd = 100 + len(self.fd)
        self.fd[fd] = (config, cpu)
        self.vals[fd] = 0
        return fd

    def read_scaled(self, fd):
        v = self.vals[fd]
        self.vals[fd] += self.steps.get(self.fd[fd], 0)
        if self.mux:      # counted only half the time -> raw is half, scaled back x2
            return v // 2, 1000, 500
        return v, 1000, 1000

    def close(self, fd):
        self.closed.append(fd)


C = td.events_for(0x1A, 0x44)


def core_steps(cpu, cyc, instr, ops, fe, be, smt):
    return {(C["cycles"], cpu): cyc, (C["instr"], cpu): instr, (C["ops"], cpu): ops,
            (C["fe"], cpu): fe, (C["be"], cpu): be, (C["smt"], cpu): smt}


class TestEncoding(unittest.TestCase):
    def test_raw_config(self):
        self.assertEqual(td.raw_config(0x76), 0x76)
        self.assertEqual(td.raw_config(0x1A0, 0x1E), 0x1E_A0 | (1 << 32))
        self.assertEqual(td.raw_config(0x1A0, 0x60), 0x60A0 | (1 << 32))

    def test_width_by_generation(self):
        self.assertEqual(td.dispatch_width(0x1A, 0x44), 8)      # Zen 5 (9800X3D)
        self.assertEqual(td.dispatch_width(0x19, 0x61), 6)      # Zen 4 (7950X)
        self.assertIsNone(td.dispatch_width(0x19, 0x21))        # Zen 3: IPC only
        self.assertEqual(set(td.events_for(0x19, 0x21)), {"cycles", "instr"})
        self.assertEqual(len(td.events_for(0x1A, 0x44)), 6)     # fits the 6 core counters

    def test_breakdown(self):
        r = td.breakdown({"cycles": 1000, "instr": 2500, "ops": 3000, "fe": 800, "be": 3200, "smt": 400}, 8)
        self.assertAlmostEqual(r["ipc"], 2.5)
        self.assertAlmostEqual(r["retiring"], 37.5)
        self.assertAlmostEqual(r["frontend"], 10.0)
        self.assertAlmostEqual(r["backend"], 40.0)
        self.assertAlmostEqual(r["smt"], 5.0)
        self.assertAlmostEqual(r["badspec"], 7.5)
        self.assertIsNone(td.breakdown({"cycles": 0}, 8)["ipc"])
        z3 = td.breakdown({"cycles": 100, "instr": 150}, None)
        self.assertAlmostEqual(z3["ipc"], 1.5)
        self.assertIsNone(z3["backend"])


class TestTopDown(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        # core 0 = cpus 0+8 (SMT pair), core 1 = cpu 1 idle
        steps = {}
        steps.update(core_steps(0, 4e9, 8e9, 12e9, 3.2e9, 12.8e9, 1.6e9))
        steps.update(core_steps(8, 1e9, 2e9, 3e9, 0.8e9, 3.2e9, 0.4e9))
        steps.update(core_steps(1, 1e5, 1e5, 1e5, 0, 0, 0))
        self.steps = steps

    def run_two(self, perf):
        t = td.TopDown(perf, [[0, 8], [1]], 0x1A, 0x44, self.clock)
        first = t.sample()
        self.clock.t += 1.0
        return t, first, t.sample()

    def test_rates_and_idle(self):
        t, first, s = self.run_two(FakeCorePerf(self.steps))
        self.assertIsNone(first[0]["ipc"])
        self.assertAlmostEqual(s[0]["ipc"], 2.0)
        self.assertAlmostEqual(s[0]["retiring"], 37.5)
        self.assertAlmostEqual(s[0]["backend"], 40.0)
        self.assertAlmostEqual(s[0]["hz"], 5e9)
        self.assertIsNone(s[1]["backend"])                     # idle core: blank, not noise
        self.assertFalse(t.multiplexed)
        t.close()
        self.assertEqual(len(t.perf.closed), 18)

    def test_multiplexed_values_scaled(self):
        t, _, s = self.run_two(FakeCorePerf(self.steps, mux=True))
        self.assertTrue(t.multiplexed)
        self.assertAlmostEqual(s[0]["backend"], 40.0, places=3)
        self.assertAlmostEqual(s[0]["ipc"], 2.0, places=3)

    def test_open_failure_on_one_cpu(self):
        t, _, s = self.run_two(FakeCorePerf(self.steps, fail_cpu=8))
        self.assertIn("Permission denied", t.error)
        self.assertIsNone(s[0]["ipc"])                         # incomplete core -> no value
        self.assertIsNone(s[1]["backend"])


if __name__ == "__main__":
    unittest.main()
