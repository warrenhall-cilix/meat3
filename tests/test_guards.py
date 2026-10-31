"""Daemon guard trips: the pre-trip values are recorded, alerts stay until acknowledged, restore re-applies."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from meat3.core import config  # noqa: E402
from meat3.core.model import Backend, Control, Sensor  # noqa: E402
from meat3.daemon import meat3d  # noqa: E402


class FakeNv(Backend):
    kind, name = "gpu", "fake 5080"

    @classmethod
    def detect(cls):
        return []

    def __init__(self):
        self.limit = 600
        self.pin5 = 2.0
        self.applied = []

    def sensors(self):
        return [Sensor("nv0.pin5.a", "GPU/pins", "Pin 5 current", self.pin5, "A", 2)]

    def controls(self):
        return [Control("nv0.oc.power_limit", "GPU", "Power limit", "int", value=self.limit, min=250, max=800, step=5)]

    def apply(self, cid, value):
        self.applied.append((cid, value))
        self.limit = int(value)
        return True, f"Power limit = {value} W"


class TestGuards(unittest.TestCase):
    def setUp(self):
        self.be = FakeNv()
        self.st = meat3d.State([self.be])
        self.st.refresh_controls()
        self.old_get = config.get
        config.get = lambda: dict(self.old_get(), guards=[
            {"name": "pin overcurrent", "sensor": "nv*.pin[0-9].a", "above": 8.5, "count": 2,
             "control": "nv*.oc.power_limit", "value": 300, "enabled": True}])

    def tearDown(self):
        config.get = self.old_get

    def test_trip_records_before_and_restore_reapplies(self):
        self.st.poll(); self.st.poll()
        self.assertEqual(self.st.alerts(), [])
        self.be.pin5 = 9.0
        self.st.poll()
        self.assertEqual(self.st.alerts(), [])                       # count=2: one hot poll is not a trip
        self.st.poll()
        al = self.st.alerts()
        self.assertEqual(len(al), 1)
        e = al[0]
        self.assertEqual((e["guard"], e["before"], e["acked"]), ("pin overcurrent", {"nv0.oc.power_limit": 600}, False))
        self.assertEqual(self.be.limit, 300)
        self.assertIn("Pin 5 current = 9.0 A > 8.5", e["trigger"])
        self.st.poll()                                               # still hot: fires once per excursion
        self.assertEqual(len(self.st.alerts()), 1)
        ok, msg = self.st.guard_restore(e["id"])
        self.assertTrue(ok, msg)
        self.assertEqual(self.be.limit, 600)
        self.assertEqual(self.st.alerts(), [])                       # restoring acknowledges
        self.assertTrue(e["restored"])
        self.assertFalse(self.st.guard_restore(999)[0])

    def test_ack_and_snapshot(self):
        self.be.pin5 = 9.0
        self.st.poll(); self.st.poll()
        self.be.pin5 = 2.0
        self.st.poll(); self.st.poll(); self.be.pin5 = 9.0; self.st.poll(); self.st.poll()
        self.assertEqual(len(self.st.alerts()), 2)                   # second excursion, second entry
        ids = [e["id"] for e in self.st.alerts()]
        self.assertTrue(self.st.guard_ack(ids[0]))
        self.assertEqual([e["id"] for e in self.st.alerts()], ids[1:])
        self.assertTrue(self.st.guard_ack("all"))
        self.assertEqual(self.st.alerts(), [])
        self.assertFalse(self.st.guard_ack(12345))
        # a trip that changed nothing (value already 300) has nothing to restore
        self.be.pin5 = 2.0; self.st.poll(); self.st.poll(); self.be.pin5 = 9.0; self.st.poll(); self.st.poll()
        e = self.st.alerts()[0]
        self.assertEqual(e["before"], {})
        ok, msg = self.st.guard_restore(e["id"])
        self.assertFalse(ok); self.assertIn("nothing", msg)
        self.assertEqual(self.st.alerts(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
