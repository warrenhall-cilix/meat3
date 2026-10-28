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


class FakeDaemon:
    def __init__(self, activity=97.0):
        self.value = 0
        self.applied = []
        self.activity = activity

    def controls(self):
        return {CTL: {"id": CTL, "value": self.value, "available": True, "min": -200, "max": 0},
                "amdgpu0.oc.power_cap": {"id": "amdgpu0.oc.power_cap", "value": 317, "available": True}}

    def sensors(self):
        return {"amdgpu0.gm.average_gfx_activity": {"value": self.activity}}

    def call(self, req):
        assert req["cmd"] == "apply" and req["id"] == CTL
        self.value = req["value"]
        self.applied.append(req["value"])
        return {"ok": True, "msg": "ok"}


class FakeKmsg:
    def __init__(self, d):
        self.d = d

    def bad_lines(self):
        return (["amdgpu 0000:03:00.0: amdgpu: ring gfx_0.0.0 timeout, signaled seq=1"]
                if self.d.value < STABLE_DOWN_TO else [])

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
        a = dict(gpu=None, floor=-200, step=10, step_time=20, margin=15, min_load=85.0, load=None, retune=False)
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
        ok, why = gputune.test_step(d, "amdgpu0", 20, 85.0, FakeKmsg(d), NoLoad(), print,
                                    self.clock.sleep, self.clock)
        self.assertIsNone(ok)
        self.assertIn("40% busy", why)

    def test_gpu_stops_reporting_is_a_failure(self):
        d = FakeDaemon()
        d.activity = None
        ok, why = gputune.test_step(d, "amdgpu0", 20, 85.0, FakeKmsg(d), NoLoad(), print,
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

    def test_pick_gpu(self):
        ctl = {"amdgpu1.oc.od.vddgfx_offset": {"available": True},
               "amdgpu0.oc.od.vddgfx_offset": {"available": False}}
        self.assertEqual(gputune.pick_gpu(ctl, None), "amdgpu1.oc.od.vddgfx_offset")
        self.assertIsNone(gputune.pick_gpu(ctl, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
