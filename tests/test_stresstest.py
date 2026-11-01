"""meat3.stresstest: presets, command building, result classification and the run state machine
(fake commands stand in for the stress tools; no hardware, no real load)."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meat3 import stresstest as st  # noqa: E402

REAL_IDS = ["cpu0.ccd0.clk_eff", "cpu0.ccd0.clk_active", "cpu0.k10temp.temp1", "cpu0.pmt.ppt", "cpu0.pmt.ppt_limit",
            "cpu0.pmt.tdc", "cpu0.pmt.tdc_limit", "cpu0.pmt.vddcr_cpu", "cpu0.pmt.vddcr_cpu_cur", "nv0.temp.gpu"]
MOCK_IDS = ["cpu.clk_eff_avg", "cpu.temp.tctl", "cpu.pwr.package", "cpu.pwr.tdc", "cpu.volt.vcore", "cpu.volt.vid"]


def finish(run: st.StressRun, timeout: float = 10.0) -> st.StressRun:
    end = time.monotonic() + timeout
    while not run.poll():
        assert time.monotonic() < end, "run did not finish"
        time.sleep(0.05)
    return run


class Sensors(unittest.TestCase):
    def test_real_ids(self):
        got = st.pick_sensors(REAL_IDS)
        self.assertEqual(got, {"clock": "cpu0.ccd0.clk_eff", "Tctl": "cpu0.k10temp.temp1", "power": "cpu0.pmt.ppt",
                               "current": "cpu0.pmt.tdc", "vcore": "cpu0.pmt.vddcr_cpu"})

    def test_mock_ids(self):
        got = st.pick_sensors(MOCK_IDS)
        self.assertEqual(got, {"clock": "cpu.clk_eff_avg", "Tctl": "cpu.temp.tctl", "power": "cpu.pwr.package",
                               "current": "cpu.pwr.tdc", "vcore": "cpu.volt.vcore"})

    def test_gpu_only(self):
        self.assertEqual(st.pick_sensors(["nv0.clk_eff_avg", "amdgpu0.temp.tctl"]), {})


class Commands(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="meat3-st-")

    def test_stress_ng(self):
        c = st.build_command("quick", 90, self.d, cpus=list(range(16)))
        self.assertEqual(c[:5], ["stress-ng", "--cpu", "16", "--cpu-method", "matrixprod"])
        self.assertIn("--verify", c)
        self.assertIn("90s", c)
        c = st.build_command("mixed", 60, self.d, cpus=[0, 1])
        self.assertEqual(c[2:5], ["2", "--cpu-method", "all"])
        self.assertFalse(st.runs_until_stopped("quick"))

    def test_ycruncher_config(self):
        c = st.build_command("avx512", 300, self.d, cpus=[0, 1, 2, 3])
        self.assertEqual(c[1:3], ["skip-warnings", "config"])
        cfg = Path(c[3]).read_text()
        self.assertIn("LogicalCores : [0 1 2 3]", cfg)
        self.assertIn("SecondsPerTest : 37", cfg)          # 300 // 8 tests
        self.assertIn('StopOnError : "true"', cfg)
        self.assertTrue(st.runs_until_stopped("avx512"))

    def test_mprime_config(self):
        c = st.build_command("p95-512", 600, self.d, cpus=list(range(8)))
        self.assertEqual(c[:2], ["mprime", "-t"])
        cfg = Path(self.d, "prime.txt").read_text()
        self.assertIn("NumWorkers=8", cfg)
        self.assertIn("CpuSupportsAVX512F=1", cfg)
        st.build_command("p95", 600, self.d, cpus=list(range(8)))
        self.assertIn("CpuSupportsAVX512F=0", Path(self.d, "prime.txt").read_text())

    def test_unknown_preset(self):
        with self.assertRaises(ValueError):
            st.StressRun("nope", 10)


class Classify(unittest.TestCase):
    def test_stress_ng(self):
        out = "stress-ng: info:  [1] dispatching hogs\nstress-ng: info:  [1] failed: 0\nstress-ng: info:  [1] successful run completed\n"
        self.assertEqual(st.classify("quick", out, 0, False), (True, "0 failures"))
        ok, why = st.classify("quick", "stress-ng: fail:  [12] cpu: matrixprod verify failed\nstress-ng: info: failed: 1\n", 2, False)
        self.assertFalse(ok)
        self.assertIn("exit 2", why)
        self.assertIn("verify failed", why)
        ok, why = st.classify("quick", "stress-ng: fail:  [12] cpu: matrixprod verify failed\n", 0, False)
        self.assertFalse(ok)
        self.assertEqual(st.classify("quick", "", 0, True), (False, "stopped"))

    def test_ycruncher(self):
        out = "Running BKT: Passed  Test Speed: 1\nRunning BBP: Passed  Test Speed: 2\n"
        self.assertEqual(st.classify("avx512", out, None, True), (True, "2 tests passed"))
        ok, why = st.classify("avx512", out, 1, False)
        self.assertFalse(ok)
        self.assertIn("stopped by itself", why)
        ok, why = st.classify("avx512", out + "Running SFTv4: FAILED\n", 1, False)
        self.assertFalse(ok)
        self.assertIn("SFTv4", why)

    def test_mprime(self):
        d = tempfile.mkdtemp(prefix="meat3-st-")
        Path(d, "results.txt").write_text("[Worker #1] Self-test 4K passed!\n[Worker #2] Self-test 4K passed!\n")
        self.assertEqual(st.classify("p95", "", None, True, d), (True, "2 FFT sizes passed"))
        with open(os.path.join(d, "results.txt"), "a") as f:
            f.write("[Worker #1] FATAL ERROR: Rounding was 0.5, expected less than 0.4\n")
        ok, why = st.classify("p95", "", None, True, d)
        self.assertFalse(ok)
        self.assertIn("FATAL ERROR", why)
        ok, why = st.classify("p95", "", 1, False, tempfile.mkdtemp(prefix="meat3-st-"))
        self.assertFalse(ok)
        self.assertIn("exited early", why)


class Runs(unittest.TestCase):
    def test_pass(self):
        run = st.StressRun("quick", 30, command=["sh", "-c", "echo 'stress-ng: info: [1] failed: 0'; sleep 0.2"],
                           kernel_check=lambda since: [])
        run.start()
        self.assertTrue(run.running)
        run.sample({"clock": 5489.0, "Tctl": 60.0})
        run.sample({"clock": 5492.0, "Tctl": 66.2})
        finish(run)
        self.assertTrue(run.ok, run.why)
        self.assertEqual(run.why, "0 failures")
        self.assertEqual(run.peaks, {"clock": 5492.0, "Tctl": 66.2})
        self.assertEqual(run.kernel, [])
        self.assertFalse(os.path.isdir(run.workdir))
        s = run.summary()
        self.assertTrue(s.startswith("stress quick PASS after"), s)
        self.assertIn("clock 5492", s)
        self.assertIn("Tctl 66.2", s)
        self.assertNotIn("not readable", s)

    def test_fail_exit(self):
        run = st.StressRun("quick", 30, command=["sh", "-c", "echo 'stress-ng: fail: [3] cpu: verify failed'; exit 2"],
                           kernel_check=lambda since: None)
        run.start()
        finish(run)
        self.assertFalse(run.ok)
        self.assertIn("exit 2", run.why)
        self.assertIn("verify failed", run.why)
        self.assertIn("kernel log not readable", run.summary())

    def test_kernel_overrides_pass(self):
        run = st.StressRun("quick", 30, command=["sh", "-c", "echo 'failed: 0'"],
                           kernel_check=lambda since: ["mce: [Hardware Error]: CPU 3: Machine Check", "x"])
        run.start()
        finish(run)
        self.assertFalse(run.ok)
        self.assertTrue(run.why.startswith("kernel: mce:"), run.why)
        self.assertIn("1 more", run.why)
        self.assertIn("FAIL", run.summary())

    def test_timed_stop_is_pass_for_looping_tool(self):
        run = st.StressRun("avx512", 1, command=["sh", "-c", "echo 'Running BKT: Passed  Test Speed: 1'; while true; do sleep 0.1; done"],
                           kernel_check=lambda since: [])
        run.start()
        t0 = time.monotonic()
        finish(run)
        self.assertGreaterEqual(time.monotonic() - t0, 0.9)
        self.assertTrue(run.stopped_by_us)
        self.assertFalse(run.user_stop)
        self.assertTrue(run.ok, run.why)
        self.assertEqual(run.why, "1 tests passed")

    def test_user_stop(self):
        run = st.StressRun("avx512", 60, command=["sh", "-c", "echo 'Running BKT: Passed'; while true; do sleep 0.1; done"],
                           kernel_check=lambda since: [])
        run.start()
        time.sleep(0.2)
        run.stop()
        finish(run)
        self.assertTrue(run.user_stop)
        self.assertFalse(run.ok)
        self.assertEqual(run.why, "stopped")
        self.assertIn("stress avx512 stopped after", run.summary())

    def test_missing_tool(self):
        old = st.tool_path
        st.tool_path = lambda tool: None
        try:
            run = st.StressRun("avx512", 10)
            run.start()
        finally:
            st.tool_path = old
        self.assertTrue(run.poll())
        self.assertFalse(run.ok)
        self.assertIn("missing tool: y-cruncher", run.why)

    def test_bad_command(self):
        run = st.StressRun("quick", 10, command=["/nonexistent/stress-tool"], kernel_check=lambda since: [])
        run.start()
        self.assertTrue(run.poll())
        self.assertFalse(run.ok)
        self.assertIn("cannot start", run.why)


class Kernel(unittest.TestCase):
    def test_regex(self):
        for ln in ("mce: [Hardware Error]: Machine check events logged", "watchdog: BUG: soft lockup - CPU#3 stuck",
                   "stress-ng[123]: segfault at 0 ip ...", "traps: foo[1] general protection fault"):
            self.assertTrue(st.KMSG_BAD.search(ln), ln)
        for ln in ("MCE: In-kernel MCE decoding enabled.", "usb 1-1: new high-speed USB device"):
            self.assertFalse(st.KMSG_BAD.search(ln) and "decoding" not in ln, ln)

    def test_cli_list(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = st.main(["--list"])
        self.assertEqual(rc, 0)
        for k in st.PRESETS:
            self.assertIn(k, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
