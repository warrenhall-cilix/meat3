"""Tests for meat3-gputune's search, load gating, crash recovery and results (no GPU, no load).

    python3 tests/test_gputune.py
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from meat3 import gputune  # noqa: E402

CTL = "amdgpu0.oc.od.vddgfx_offset"
STABLE_DOWN_TO = -60


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


CLK = "amdgpu0.oc.od.sclk_offset"
CLOCK_STABLE_UP_TO = 150


class FakeDaemon:
    def __init__(self, activity=97.0, clock_ctl=True):
        self.value = 0
        self.clock = 0
        self.applied = []
        self.clock_applied = []
        self.activity = activity
        self.clock_ctl = clock_ctl

    def controls(self):
        c = {CTL: {"id": CTL, "value": self.value, "available": True, "min": -200, "max": 0},
             "amdgpu0.oc.power_cap": {"id": "amdgpu0.oc.power_cap", "value": 317, "available": True}}
        if self.clock_ctl:
            c[CLK] = {"id": CLK, "value": self.clock, "available": True, "min": -500, "max": 1000}
        return c

    def sensors(self):
        # a card that clocks ~1 MHz higher per mV of undervolt and draws a little less power
        return {"amdgpu0.gm.average_gfx_activity": {"value": self.activity},
                "amdgpu0.gm.average_gfxclk_frequency": {"value": 2900 - self.value + self.clock},
                "amdgpu0.gm.average_socket_power": {"value": 300 + self.value * 0.2},
                "amdgpu0.gm.temperature_hotspot": {"value": 80},
                "amdgpu0.smu14.throttle.duty.power": {"value": 90 + self.value * 0.5}}

    def call(self, req):
        assert req["cmd"] == "apply"
        if req["id"] == CLK:
            self.clock = req["value"]
            self.clock_applied.append(req["value"])
            return {"ok": True, "msg": "ok"}
        assert req["id"] == CTL
        self.value = req["value"]
        self.applied.append(req["value"])
        return {"ok": True, "msg": "ok"}


class FakeKmsg:
    def __init__(self, d):
        self.d = d

    def bad_lines(self):
        return (["amdgpu 0000:03:00.0: amdgpu: ring gfx_0.0.0 timeout, signaled seq=1"]
                if self.d.value < STABLE_DOWN_TO or self.d.clock > CLOCK_STABLE_UP_TO else [])

    def close(self):
        pass


class NoLoad:
    def ensure(self):
        return None

    def stop(self):
        pass


class TestGputune(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meat3-gputune-")
        gputune.STATE = os.path.join(self.tmp, "gputune.json")
        self.clock = Clock()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def args(self, **kw):
        a = dict(gpu=None, floor=-200, step=10, step_time=20, margin=15, min_load=85.0, load=None, retune=False,
                 clock=False, clock_step=25, clock_max=400, clock_margin=25)
        a.update(kw)
        return argparse.Namespace(**a)

    def run_tune(self, d, **kw):
        return gputune.tune(self.args(**kw), d=d, km=FakeKmsg(d), load=NoLoad(),
                            sleep=self.clock.sleep, clock=self.clock)

    def test_finds_stable_offset_and_restores(self):
        d = FakeDaemon()
        self.assertEqual(self.run_tune(d), 0)
        st = gputune.load_state()
        self.assertEqual((st["pass"], st["fail"], st["result"]), (-60, -70, -45))
        self.assertEqual(d.value, 0)                         # back to the starting offset
        self.assertEqual(d.applied[:8], [0, -10, -20, -30, -40, -50, -60, -70])
        self.assertNotIn("current", st)

    def test_floor_respected(self):
        d = FakeDaemon()
        self.run_tune(d, floor=-30)
        st = gputune.load_state()
        self.assertEqual((st["pass"], st.get("fail"), st["result"]), (-30, None, -15))
        self.assertNotIn(-40, d.applied)

    def test_light_load_steps_not_counted(self):
        d = FakeDaemon(activity=40.0)
        ok, why, _ = gputune.test_step(d, "amdgpu0", 20, 85.0, FakeKmsg(d), NoLoad(), print,
                                       self.clock.sleep, self.clock)
        self.assertIsNone(ok)
        self.assertIn("40% busy", why)

    def test_gpu_stops_reporting_is_a_failure(self):
        d = FakeDaemon()
        d.activity = None
        ok, why, _ = gputune.test_step(d, "amdgpu0", 20, 85.0, FakeKmsg(d), NoLoad(), print,
                                       self.clock.sleep, self.clock)
        self.assertFalse(ok)
        self.assertIn("stopped reporting", why)

    def test_crash_marks_value_unstable_and_resumes(self):
        gputune.save_state({"control": CTL, "initial": 0, "pass": -40, "current": {"value": -50, "since": 0}})
        d = FakeDaemon()
        self.run_tune(d)
        st = gputune.load_state()
        self.assertEqual((st["pass"], st["fail"], st["result"]), (-40, -50, -25))
        self.assertIn("hung", st["fail_reason"])
        self.assertNotIn(-50, d.applied)                      # never retried the crash value

    def test_step_telemetry_recorded(self):
        d = FakeDaemon()
        self.run_tune(d)
        st = gputune.load_state()
        s0, s60 = st["steps"]["0"], st["steps"]["-60"]
        self.assertTrue(s0["ok"] and s60["ok"] and not st["steps"]["-70"]["ok"])
        self.assertEqual((s0["mhz"], s60["mhz"]), (2900, 2960))
        self.assertEqual((s0["w"], s60["w"], s60["hot"], s60["ppt_duty"], s60["load"]), (300, 288, 80, 60, 97))
        self.assertIn("2960", "".join(gputune.fmt_stats(s60)))

    def test_clock_phase_after_undervolt(self):
        d = FakeDaemon()
        self.assertEqual(self.run_tune(d, clock=True), 0)
        st = gputune.load_state()
        self.assertEqual((st["result"], st["clock_control"]), (-45, CLK))
        self.assertEqual((st["clock_pass"], st["clock_fail"], st["clock_result"]), (150, 175, 125))
        self.assertEqual(d.clock_applied, [25, 50, 75, 100, 125, 150, 175, 0])   # restored at the end
        self.assertEqual(d.applied[-2:], [-45, 0])                               # undervolt applied for phase 2, then restored
        self.assertTrue(st["clock_steps"]["150"]["ok"] and not st["clock_steps"]["175"]["ok"])
        # a second --clock run keeps the undervolt result and resumes the clock sweep above the known fail
        d2 = FakeDaemon()
        self.assertEqual(self.run_tune(d2, clock=True), 0)
        self.assertEqual(d2.applied, [-45, 0])
        self.assertEqual(d2.clock_applied, [0])          # nothing left to test between 150 and 175
        # apply / save-boot set both controls
        d3 = FakeDaemon()
        self.assertEqual(gputune.apply_result(gputune.load_state(), d=d3), 0)
        self.assertEqual((d3.value, d3.clock), (-45, 125))
        self.assertEqual([(c, v) for c, v, _ in gputune._results(gputune.load_state())], [(CTL, -45), (CLK, 125)])

    def test_clock_crash_marks_value_unstable(self):
        gputune.save_state({"control": CTL, "initial": 0, "pass": -60, "fail": -70, "result": -45,
                            "clock_control": CLK, "clock_initial": 0, "clock_pass": 100,
                            "current": {"value": 125, "since": 0, "kind": "clock"}})
        d = FakeDaemon()
        self.run_tune(d, clock=True)
        st = gputune.load_state()
        self.assertEqual((st["clock_fail"], st["clock_result"]), (125, 75))
        self.assertIn("hung", st["clock_fail_reason"])
        self.assertEqual(d.clock_applied, [0])            # 125 never retried; nothing between 100 and 125

    def test_clock_needs_control(self):
        d = FakeDaemon(clock_ctl=False)
        self.assertEqual(self.run_tune(d, clock=True), 2)

    def test_pick_gpu(self):
        ctl = {"amdgpu1.oc.od.vddgfx_offset": {"available": True},
               "amdgpu0.oc.od.vddgfx_offset": {"available": False}}
        self.assertEqual(gputune.pick_gpu(ctl, None), "amdgpu1.oc.od.vddgfx_offset")
        self.assertIsNone(gputune.pick_gpu(ctl, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
