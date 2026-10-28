"""Tests for meat3-cotune's search, crash recovery and result handling (no hardware, no stress).

    python3 tests/test_cotune.py
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from meat3 import cotune  # noqa: E402

STABLE = {0: -20, 1: -35, 2: 0, 3: 5}       # most negative stable CO per core (3: even 0 fails)


class FakeDaemon:
    def __init__(self, *_a):
        self.co = {k: 0 for k in STABLE}
        self.log = []

    def controls(self):
        return {f"cpu0.smu.co_core{k}": {"id": f"cpu0.smu.co_core{k}", "value": v, "available": True}
                for k, v in self.co.items()}

    def sensors(self):
        return {f"cpu0.core{k}.cppc_rank": {"value": 100 - k} for k in STABLE}

    def set_co(self, k, v):
        self.co[k] = v
        self.log.append((k, v))


class TestCotune(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meat3-cotune-")
        cotune.STATE = os.path.join(self.tmp, "cotune.json")
        self.d = FakeDaemon()
        cotune.Daemon = lambda *_a: self.d
        cotune.core_threads = lambda: [[k, k + 8] for k in STABLE]
        cotune.Kmsg = type("K", (), {"__init__": lambda s: None, "bad_lines": lambda s: [],
                                     "close": lambda s: None})
        cotune.test_step = lambda cpu, t, km, log: (self.d.co[cpu] >= STABLE[cpu], "verify error")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def args(self, **kw):
        a = dict(cores=None, floor=-30, step=5, step_time=1, margin=3, retune=False)
        a.update(kw)
        return argparse.Namespace(**a)

    def test_search_results(self):
        self.assertEqual(cotune.tune(self.args()), 0)
        st = cotune.load_state()
        c = st["cores"]
        self.assertEqual((c["0"].get("pass"), c["0"].get("fail"), c["0"].get("result")), (-20, -25, -17))
        self.assertEqual((c["1"].get("pass"), c["1"].get("fail"), c["1"].get("result")), (-30, None, -27))   # hit floor
        self.assertEqual((c["2"].get("pass"), c["2"].get("fail"), c["2"].get("result")), (0, -5, 0))
        self.assertEqual((c["3"].get("pass"), c["3"].get("fail"), c["3"].get("result")), (None, 0, None))
        self.assertNotIn("current", st)
        self.assertEqual(self.d.co, {0: 0, 1: 0, 2: 0, 3: 0})          # restored to start values
        self.assertEqual(cotune.results(st), {0: -17, 1: -27, 2: 0})

    def test_crash_during_step_marks_value_unstable_and_resumes(self):
        # simulate: core 0 passed -10, machine died while testing -15
        cotune.save_state({"initial": {"0": 0, "1": 0, "2": 0, "3": 0},
                           "cores": {"0": {"pass": -10}},
                           "current": {"core": 0, "value": -15, "since": 0}})
        cotune.tune(self.args(cores="0"))
        c = cotune.load_state()["cores"]["0"]
        self.assertEqual((c["pass"], c["fail"], c["result"]), (-10, -15, -7))
        self.assertIn("hung", c["fail_reason"])
        self.assertNotIn((0, -15), self.d.log)                        # never retried the crash value

    def test_already_done_skipped_unless_retune(self):
        cotune.tune(self.args(cores="0"))
        n = len(self.d.log)
        cotune.tune(self.args(cores="0"))
        self.assertEqual(len(self.d.log), n + len(STABLE))           # only the final restore calls
        cotune.tune(self.args(cores="0", retune=True))
        self.assertGreater(len(self.d.log), n + 2 * len(STABLE))

    def test_state_file_written_atomically(self):
        cotune.save_state({"a": 1})
        with open(cotune.STATE) as f:
            self.assertEqual(json.load(f), {"a": 1})
        self.assertFalse(os.path.exists(cotune.STATE + ".tmp"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
