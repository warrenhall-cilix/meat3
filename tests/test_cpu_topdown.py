import os
import shutil
import tempfile
import unittest

from meat3.backends import cpu_topdown as td


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class FakeCorePerf:
    """Raw core-PMU fake. Counters advance per read by steps[(config, target)], where target is the
    cpu for per-CPU sets and ('tid', n) for thread sets. mux: counted half the time.
    starve_groups: grouped events never run (a pinned counter elsewhere)."""

    def __init__(self, steps, mux=False, fail_cpu=None, starve_groups=False):
        self.steps, self.mux, self.fail_cpu, self.starve = steps, mux, fail_cpu, starve_groups
        self.fd = {}
        self.st = {}
        self.closed = []
        self.opens = []
        self.gate, self.on, self.ioctls = {}, {}, []

    def open(self, typ, config, cpu, read_format=0, pid=-1, group_fd=-1, disabled=False):
        assert typ == td.PERF_TYPE_RAW and read_format == 3
        assert (pid == -1) != (cpu == -1)
        if cpu == self.fail_cpu:
            raise OSError(13, "Permission denied")
        fd = 100 + len(self.opens)
        target = cpu if pid == -1 else ("tid", pid)
        self.opens.append((config, target, group_fd))
        self.fd[fd] = (config, target)
        self.st[fd] = [0, 0, 0]
        self.gate[fd] = fd if group_fd == -1 else group_fd
        self.on[fd] = not disabled
        return fd

    def ioctl(self, fd, request, arg=0):
        assert request in (td.PERF_EVENT_IOC_ENABLE, td.PERF_EVENT_IOC_DISABLE)
        self.ioctls.append((fd, request, arg))
        self.on[fd] = request == td.PERF_EVENT_IOC_ENABLE

    def read_scaled(self, fd):
        if fd in self.closed:
            return None
        # each read = one more second of counting (if enabled), then report the totals
        config, target = self.fd[fd]
        st = self.st[fd]
        step = self.steps.get((config, target), 0)
        if self.on[self.gate[fd]]:                  # disabled: nothing advances
            st[1] += 1_000_000_000
            if self.starve and self._is_grouped(fd):
                pass
            elif self.mux:
                st[0] += step // 2
                st[2] += 500_000_000
            else:
                st[0] += step
                st[2] += 1_000_000_000
        return tuple(st)

    def _is_grouped(self, fd):
        """fd belongs to a set opened as a group (its leader, or a member pointing at one)."""
        idx = fd - 100
        if self.opens[idx][2] != -1:
            return True
        nxt = self.opens[idx + 1] if idx + 1 < len(self.opens) else None
        return nxt is not None and nxt[2] == fd

    def close(self, fd):
        self.closed.append(fd)


C = td.events_for(0x1A, 0x44)


def steps_for(target, cyc, instr, ops, fe, be, smt):
    return {(C["cycles"], target): cyc, (C["instr"], target): instr, (C["ops"], target): ops,
            (C["fe"], target): fe, (C["be"], target): be, (C["smt"], target): smt}


class TestEncoding(unittest.TestCase):
    def test_raw_config(self):
        self.assertEqual(td.raw_config(0x76), 0x76)
        self.assertEqual(td.raw_config(0x1A0, 0x1E), 0x1EA0 | (1 << 32))
        self.assertEqual(td.raw_config(0x1A0, 0x60), 0x60A0 | (1 << 32))

    def test_width_by_generation(self):
        self.assertEqual(td.dispatch_width(0x1A, 0x44), 8)      # Zen 5 (9800X3D)
        self.assertEqual(td.dispatch_width(0x19, 0x61), 6)      # Zen 4 (7950X)
        self.assertIsNone(td.dispatch_width(0x19, 0x21))        # Zen 3: IPC only
        self.assertEqual(set(td.events_for(0x19, 0x21)), {"cycles", "instr"})
        self.assertEqual(len(td.events_for(0x1A, 0x44)), 6)     # fits the 6 core counters
        self.assertEqual(next(iter(td.events_for(0x1A, 0x44))), "cycles")   # group leader

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
        self.steps = {}
        self.steps.update(steps_for(0, 4e9, 8e9, 12e9, 3.2e9, 12.8e9, 1.6e9))
        self.steps.update(steps_for(8, 1e9, 2e9, 3e9, 0.8e9, 3.2e9, 0.4e9))
        self.steps.update(steps_for(1, 1e5, 1e5, 1e5, 0, 0, 0))

    def run_two(self, perf):
        t = td.TopDown(perf, [[0, 8], [1]], 0x1A, 0x44, self.clock)
        first = t.sample()
        self.clock.t += 1.0
        return t, first, t.sample()

    def test_rates_and_idle(self):
        perf = FakeCorePerf(self.steps)
        t, first, s = self.run_two(perf)
        self.assertTrue(t.grouped)
        # each set: leader opened with group_fd -1, the other 5 point at it
        leaders = [o for o in perf.opens if o[2] == -1]
        self.assertEqual(len(leaders), 3)
        self.assertIsNone(first[0]["ipc"])
        self.assertAlmostEqual(s[0]["ipc"], 2.0)
        self.assertAlmostEqual(s[0]["retiring"], 37.5)
        self.assertAlmostEqual(s[0]["backend"], 40.0)
        self.assertAlmostEqual(s[0]["hz"], 5e9)
        self.assertIsNone(s[1]["backend"])                     # idle core: blank, not noise
        self.assertFalse(t.multiplexed)
        t.close()
        self.assertEqual(len(perf.closed), 18)

    def test_multiplexed_values_scaled(self):
        t, _, s = self.run_two(FakeCorePerf(self.steps, mux=True))
        self.assertTrue(t.multiplexed)
        self.assertAlmostEqual(s[0]["backend"], 40.0, places=3)
        self.assertAlmostEqual(s[0]["ipc"], 2.0, places=3)
        self.assertAlmostEqual(s[0]["hz"], 5e9, delta=1e3)     # extrapolated back to full time

    def test_open_failure_on_one_cpu(self):
        t, _, s = self.run_two(FakeCorePerf(self.steps, fail_cpu=8))
        self.assertIn("Permission denied", t.error)
        self.assertIsNone(s[0]["ipc"])                         # incomplete core -> no value
        self.assertIsNone(s[1]["backend"])

    def test_starved_group_falls_back_to_ungrouped(self):
        perf = FakeCorePerf(self.steps, starve_groups=True)
        t = td.TopDown(perf, [[0, 8], [1]], 0x1A, 0x44, self.clock)
        for _ in range(4):
            t.sample()
            self.clock.t += 1.0
        self.assertFalse(t.grouped)
        s = t.sample()
        self.clock.t += 1.0
        s = t.sample()
        self.assertAlmostEqual(s[0]["backend"], 40.0)


def write_thread(proc, pid, tid, ticks, start=1000, switches=0):
    d = os.path.join(proc, str(pid), "task", str(tid))
    os.makedirs(d, exist_ok=True)
    rest = ["S", "1"] + ["0"] * 9 + [str(ticks), "0"] + ["0"] * 6 + [str(start)]
    with open(os.path.join(d, "stat"), "w") as f:
        f.write(f"{tid} (worker x) " + " ".join(rest) + "\n")
    with open(os.path.join(d, "schedstat"), "w") as f:
        f.write(f"123 456 {switches}\n")


class TestAppPipeline(unittest.TestCase):
    def setUp(self):
        self.proc = tempfile.mkdtemp(prefix="meat3proc")
        self.clock = Clock()
        self.hz = os.sysconf("SC_CLK_TCK")
        self.ticks = {}

    def tearDown(self):
        shutil.rmtree(self.proc, ignore_errors=True)

    def tick(self, pid, tid, pct):
        self.ticks[tid] = self.ticks.get(tid, 0) + int(round(self.hz * pct / 100))
        write_thread(self.proc, pid, tid, self.ticks[tid])

    def test_hot_threads_only_and_coverage(self):
        steps = {}
        steps.update(steps_for(("tid", 501), 4e9, 6e9, 8e9, 3.2e9, 16e9, 0))    # main thread: 50% back-end
        steps.update(steps_for(("tid", 502), 1e9, 3e9, 4e9, 0.8e9, 0.8e9, 0))
        perf = FakeCorePerf(steps)
        ap = td.AppPipeline(perf, 0x1A, 0x44, self.clock, self.proc, max_threads=2, min_thread_pct=1.0)
        apps = [{"key": "steam:1", "pids": [500]}]
        for i in range(4):
            self.tick(500, 501, 90)
            self.tick(500, 502, 40)
            self.tick(500, 503, 0)            # idle thread: never counted
            self.tick(500, 504, 0.5 if i < 2 else 0)
            r = ap.update(apps)["steam:1"]
            self.clock.t += 1.0
        targets = {o[1] for o in perf.opens}
        self.assertEqual(targets, {("tid", 501), ("tid", 502)})
        self.assertEqual(r["counted"], 2)
        self.assertEqual(r["threads"], 4)
        self.assertAlmostEqual(r["coverage"], 100.0)
        self.assertAlmostEqual(r["ipc"], 9e9 / 5e9)
        self.assertAlmostEqual(r["backend"], 100 * 16.8e9 / 40e9)
        ap.close()
        self.assertEqual(len(perf.closed), len(perf.opens))

    def test_exited_threads_closed_and_cap(self):
        steps = {}
        for tid in (601, 602, 603):
            steps.update(steps_for(("tid", tid), 1e9, 1e9, 1e9, 0, 0, 0))
        perf = FakeCorePerf(steps)
        ap = td.AppPipeline(perf, 0x1A, 0x44, self.clock, self.proc, max_threads=2)
        apps = [{"key": "x", "pids": [600]}]
        for _ in range(4):
            self.tick(600, 601, 80)
            self.tick(600, 602, 60)
            self.tick(600, 603, 10)
            r = ap.update(apps)["x"]
            self.clock.t += 1.0
        self.assertEqual(r["counted"], 2)                     # capped at the 2 hottest
        self.assertLess(r["coverage"], 100.0)
        shutil.rmtree(os.path.join(self.proc, "600", "task", "601"))
        r = ap.update(apps)["x"]
        self.assertEqual(len(perf.closed), 4)                  # 601's group of 4 closed
        self.assertEqual(r["threads"], 2)
        self.assertEqual(ap.update([]), {})                    # app gone: everything closed
        self.assertEqual(len(perf.closed), 8)


class TestCost(unittest.TestCase):
    def test_app_events_are_the_small_set(self):
        self.assertEqual(set(td.app_events_for(0x1A, 0x44)), {"cycles", "instr", "be", "fe"})
        r = td.breakdown({"cycles": 100, "instr": 150, "be": 400, "fe": 80}, 8)
        self.assertAlmostEqual(r["backend"], 50.0)
        self.assertAlmostEqual(r["frontend"], 10.0)
        self.assertIsNone(r["retiring"])
        self.assertIsNone(r["badspec"])                        # not derivable without all buckets

    def test_busy_switchers_skipped_and_cost_estimated(self):
        proc = tempfile.mkdtemp(prefix="meat3proc")
        try:
            clock = Clock()
            hz = os.sysconf("SC_CLK_TCK")
            steps = {}
            for tid in (801, 802):
                steps.update(steps_for(("tid", tid), 1e9, 1e9, 1e9, 0, 0, 0))
            perf = FakeCorePerf(steps)
            ap = td.AppPipeline(perf, 0x1A, 0x44, clock, proc, max_switches=20000)
            for i in range(4):
                write_thread(proc, 800, 801, hz * (i + 1), switches=5000 * (i + 1))      # 5k/s: counted
                write_thread(proc, 800, 802, hz * (i + 1), switches=90000 * (i + 1))     # 90k/s: skipped
                r = ap.update([{"key": "g", "pids": [800]}])["g"]
                clock.t += 1.0
            self.assertEqual({o[1] for o in perf.opens}, {("tid", 801)})
            self.assertEqual(r["skipped_busy"], 1)
            self.assertAlmostEqual(r["coverage"], 50.0)
            # 5000 switches/s x 1.1 us x half the time = 0.275% of a core
            self.assertAlmostEqual(ap.cost_pct, 100 * 5000 * td.SWITCH_COST_S * 0.5)
            ap.set_enabled(False)                              # per-core turn: still says why
            write_thread(proc, 800, 802, hz * 6, switches=90000 * 6)
            self.assertEqual(ap.update([{"key": "g", "pids": [800]}])["g"]["skipped_busy"], 1)
        finally:
            shutil.rmtree(proc, ignore_errors=True)


class TestTakingTurns(unittest.TestCase):
    """Per-core and per-thread sets alternate; each view returns its last result while off."""

    def test_disable_freezes_and_caches(self):
        clock = Clock()
        steps = steps_for(0, 4e9, 8e9, 12e9, 3.2e9, 12.8e9, 1.6e9)
        perf = FakeCorePerf(steps)
        t = td.TopDown(perf, [[0]], 0x1A, 0x44, clock)
        t.sample()
        clock.t += 1.0
        on = t.sample()
        self.assertAlmostEqual(on[0]["backend"], 40.0)
        t.set_enabled(False)
        leader = next(fd for fd, (c, tg) in perf.fd.items() if c == C["cycles"])
        self.assertEqual(perf.ioctls[-1], (leader, td.PERF_EVENT_IOC_DISABLE, td.PERF_IOC_FLAG_GROUP))
        self.assertEqual(len(perf.ioctls), 1)                  # one ioctl per group
        clock.t += 1.0
        self.assertIs(t.sample(), on)                          # cached while off
        perf.steps = steps_for(0, 4e9, 8e9, 12e9, 3.2e9, 4e9, 1.6e9)  # workload changes
        t.set_enabled(True)
        clock.t += 1.0
        s = t.sample()
        self.assertAlmostEqual(s[0]["backend"], 12.5)          # only the enabled window counted

    def test_threads_opened_while_off_start_disabled(self):
        proc = tempfile.mkdtemp(prefix="meat3proc")
        try:
            clock = Clock()
            hz = os.sysconf("SC_CLK_TCK")
            perf = FakeCorePerf(steps_for(("tid", 701), 1e9, 2e9, 3e9, 0, 1e9, 0))
            ap = td.AppPipeline(perf, 0x1A, 0x44, clock, proc)
            ap.set_enabled(False)
            ticks = 0
            for _ in range(4):
                ticks += hz
                write_thread(proc, 700, 701, ticks)
                r = ap.update([{"key": "g", "pids": [700]}])["g"]
                clock.t += 1.0
            self.assertTrue(ap.counting)
            self.assertIsNone(r["ipc"])                        # never had the counters yet
            leader = next(fd for fd, o in zip(sorted(perf.fd), perf.opens) if o[2] == -1)
            self.assertFalse(perf.on[leader])
            ap.set_enabled(True)
            ticks += hz
            write_thread(proc, 700, 701, ticks)
            r = ap.update([{"key": "g", "pids": [700]}])["g"]
            self.assertAlmostEqual(r["ipc"], 2.0)
            ap.set_enabled(False)
            ticks += hz
            write_thread(proc, 700, 701, ticks)
            self.assertAlmostEqual(ap.update([{"key": "g", "pids": [700]}])["g"]["ipc"], 2.0)   # cached
        finally:
            shutil.rmtree(proc, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
