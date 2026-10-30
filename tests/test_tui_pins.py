"""TUI pinned sensors: graph rendering, pins file, pin/unpin/reorder keys."""
import collections
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from meat3 import tui  # noqa: E402


class GraphRowsTest(unittest.TestCase):
    def test_shape_and_right_alignment(self):
        rows = tui.graph_rows([0, 50, 100], 6, 2, 0, 100)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(len(r) == 6 for r in rows))
        self.assertEqual(rows[0][:3], "   ")                    # no data yet on the left
        self.assertEqual(rows[1][:3], "   ")
        self.assertEqual(rows[0][-1], "█")                      # 100 % fills both rows
        self.assertEqual(rows[1][-1], "█")
        self.assertEqual(rows[0][-2], " ")                      # 50 % fills only the bottom row
        self.assertEqual(rows[1][-2], "█")
        self.assertEqual(rows[1][-3], "▁")                      # 0 still shows a sliver

    def test_autoscale_flat_and_gaps(self):
        rows = tui.graph_rows([40, 40, 40], 3, 1)
        self.assertEqual(rows, ["▄▄▄"])                         # flat line drawn at half height
        rows = tui.graph_rows([10, None, 20], 3, 1)
        self.assertEqual(rows[0][1], " ")
        self.assertEqual(rows[0][2], "█")
        self.assertEqual(tui.graph_rows([], 4, 2), ["    ", "    "])

    def test_only_last_width_values(self):
        rows = tui.graph_rows(list(range(100)), 10, 1)
        self.assertEqual(rows[0][-1], "█")
        self.assertEqual(rows[0][0], "▁")                       # scaled to 90..99, not 0..99


class PinsFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = os.path.join(self.tmp, "meat3", "tui.json")

    def test_roundtrip_and_bad_files(self):
        self.assertEqual(tui.load_pins(self.path), [])
        tui.save_pins(self.path, [{"id": "nv0.t.hotspot", "graph": True}, {"id": "cpu0.pmt.ppt", "graph": False}])
        self.assertEqual(tui.load_pins(self.path), [{"id": "nv0.t.hotspot", "graph": True},
                                                    {"id": "cpu0.pmt.ppt", "graph": False}])
        with open(self.path, "w") as f:
            f.write('{"pins": [{"id": 5}, "junk", {"id": "ok"}]}')
        self.assertEqual(tui.load_pins(self.path), [{"id": "ok", "graph": False}])
        with open(self.path, "w") as f:
            f.write("not json")
        self.assertEqual(tui.load_pins(self.path), [])


class PinKeysTest(unittest.TestCase):
    """Drives the key handlers without curses (no screen needed for these paths)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        t = tui.Tui.__new__(tui.Tui)
        t.pins_file = os.path.join(self.tmp, "tui.json")
        t.pins, t.hist, t.filter = [], {}, ""
        t.view, t.sel, t.scroll = 1, {0: 0, 1: 0, 2: 0, 3: 0}, {0: 0, 1: 0, 2: 0, 3: 0}
        t.msg, t.msg_t = "", 0.0
        t.sensors = {sid: {"id": sid, "group": "GPU 0: Test/Temps", "label": sid, "unit": "°C", "value": 50}
                     for sid in ("a.t1", "a.t2", "a.t3")}
        t.order = list(t.sensors)
        self.t = t

    def test_pin_toggle_graph_unpin(self):
        t = self.t
        t.key("a")                                              # pin the first sensor as a value
        self.assertEqual(t.pins, [{"id": "a.t1", "graph": False}])
        t.key("g")                                              # same sensor: switch to graph
        self.assertEqual(t.pins, [{"id": "a.t1", "graph": True}])
        t.sel[1] = 2
        t.key("g")                                              # new sensor straight in as a graph
        self.assertEqual(t.pins[1], {"id": "a.t3", "graph": True})
        self.assertEqual(tui.load_pins(t.pins_file), t.pins)    # saved on every change
        t.sel[1] = 0
        t.key("a")                                              # a on a pinned sensor unpins it
        self.assertEqual([p["id"] for p in t.pins], ["a.t3"])

    def test_overview_edit_keys(self):
        t = self.t
        t.pins = [{"id": s, "graph": False} for s in ("a.t1", "a.t2", "a.t3")]
        t.view = 0
        t.key("]")                                              # move the first pin down
        self.assertEqual([p["id"] for p in t.pins], ["a.t2", "a.t1", "a.t3"])
        self.assertEqual(t.sel[0], 1)
        t.key("g")
        self.assertTrue(t.pins[1]["graph"])
        t.key("x")
        self.assertEqual([p["id"] for p in t.pins], ["a.t2", "a.t3"])
        t.key("[")
        self.assertEqual([p["id"] for p in t.pins], ["a.t3", "a.t2"])
        t.key("a")                                              # from the Overview: go and pick one
        self.assertEqual(t.view, 1)


if __name__ == "__main__":
    unittest.main()
