import unittest

from meat3.backends.vf_measured import VfMeasured


class TestVfMeasured(unittest.TestCase):
    def test_bins_keep_best_and_last(self):
        v = VfMeasured()
        for mv, mhz in ((901, 2500), (899, 2520), (902, 2510), (1000, 3000), (1001, 2990)):
            self.assertTrue(v.add(mv, mhz, 80))
        pts = v.points()
        self.assertEqual([p["mv"] for p in pts], [900, 1000])
        self.assertEqual((pts[0]["mhz"], pts[0]["last"], pts[0]["n"]), (2520, 2510, 3))
        self.assertEqual((pts[1]["mhz"], pts[1]["last"]), (3000, 2990))
        self.assertEqual(v.value()["mode"], "measured")
        self.assertEqual(v.value()["samples"], 5)

    def test_idle_and_garbage_rejected(self):
        v = VfMeasured()
        self.assertFalse(v.add(None, 2500, 80))
        self.assertFalse(v.add(700, 2500, 5))          # idle
        self.assertFalse(v.add(700, 300, 80))          # deep-sleep clock
        self.assertFalse(v.add("x", 2500, 80))
        self.assertEqual(v.points(), [])
        self.assertEqual(v.rejected, 2)

    def test_single_sample_bins_hidden_until_confirmed(self):
        v = VfMeasured()
        v.add(950, 2800, 90)
        self.assertEqual(v.points(), [])
        self.assertEqual(len(v.points(min_samples=1)), 1)
        v.add(950, 2805, 90)
        self.assertEqual(v.points()[0]["mhz"], 2805)

    def test_reset_and_load(self):
        v = VfMeasured()
        v.load([{"mv": 800, "mhz": 2200, "n": 3}, {"mv": 1000, "mhz": 3000, "last": 2980, "n": 2}])
        self.assertEqual([p["mhz"] for p in v.points()], [2200, 3000])
        self.assertEqual(v.points()[1]["last"], 2980)
        v.reset()
        self.assertEqual((v.points(), v.samples), ([], 0))
