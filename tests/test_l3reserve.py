"""Tests for the L3 game reservation (meat3/backends/l3reserve.py + its use in l3apps.L3Apps)
against fake /proc and resctrl trees.

    python3 tests/test_l3reserve.py
"""
from __future__ import annotations

import os
import shutil
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
from meat3.backends import l3apps, l3reserve  # noqa: E402
from test_l3apps import Fake  # noqa: E402

MIB96 = 96 << 20
STEAM_ENV = b"SteamAppId=1091500\0"


class ResctrlFake(Fake):
    def __init__(self, domains=(0,)):
        super().__init__()
        os.makedirs(os.path.join(self.rc, "info", "L3"))
        self.put("info/L3/cbm_mask", "ffff\n")
        self.put("schemata", "    MB:" + ";".join(f"{d}=4096" for d in domains) + "\n"
                 "    L3:" + ";".join(f"{d}=ffff" for d in domains) + "\n")

    def put(self, rel, text):
        with open(os.path.join(self.rc, rel), "w") as f:
            f.write(text)

    def get(self, rel):
        with open(os.path.join(self.rc, rel)) as f:
            return f.read()


def fake_mkdir_ctrl(path):
    """What the kernel provides inside a new control group."""
    for d in ("mon_groups", "mon_data"):
        os.makedirs(os.path.join(path, d), exist_ok=True)
    for f in ("tasks", "schemata"):
        open(os.path.join(path, f), "a").close()


class TestL3Reserve(unittest.TestCase):
    def setUp(self):
        self.f = ResctrlFake()
        self._rm = (l3reserve._rmdir, l3apps._rmdir)
        l3reserve._rmdir = l3apps._rmdir = shutil.rmtree
        real_mkdir = os.mkdir

        def mkdir(path, *args):                         # what the kernel puts in a new resctrl group
            real_mkdir(path, *args)
            if os.path.basename(os.path.dirname(path)) == "mon_groups":
                open(os.path.join(path, "tasks"), "a").close()
            elif os.path.dirname(path) == self.f.rc:
                fake_mkdir_ctrl(path)
        os.mkdir = mkdir
        self.addCleanup(setattr, os, "mkdir", real_mkdir)

    def tearDown(self):
        l3reserve._rmdir, l3apps._rmdir = self._rm
        self.f.close()

    def make(self, **kw):
        kw.setdefault("domain_sizes", {0: MIB96})
        return l3reserve.L3Reserve(self.f.rc, **kw)

    def test_masks_exclusive_and_rounded_to_ways(self):
        r = self.make(background_mb=12)
        self.assertTrue(r.available)
        self.assertEqual((r.ways, r.way_mb, r.bg_ways), (16, 6.0, 2))
        self.assertEqual(r.masks(), (0x0003, 0xfffc))
        r.set_background_mb(20)                       # 3.33 ways -> 3
        self.assertEqual(r.masks(), (0x0007, 0xfff8))
        r.set_background_mb(500)                      # capped at half the cache
        self.assertEqual(r.bg_ways, 8)
        r.set_background_mb(0)
        self.assertEqual(r.bg_ways, 1)

    def test_activate_then_deactivate_restores_root(self):
        r = self.make()
        self.assertTrue(r.activate())
        self.assertEqual(self.f.get("meat3_game/schemata"), "L3:0=fffc\n")
        self.assertEqual(self.f.get("schemata"), "L3:0=3\n")
        r.deactivate()
        self.assertEqual(self.f.get("schemata"), "L3:0=ffff\n")
        self.assertFalse(os.path.exists(r.game_dir))

    def test_multiple_domains(self):
        self.f.close()
        self.f = ResctrlFake(domains=(0, 1))
        r = self.make(domain_sizes={0: MIB96, 1: 32 << 20})
        self.assertEqual(r.way_mb, 6.0)               # sized by the biggest (V-cache) CCD
        r.activate()
        self.assertEqual(self.f.get("schemata"), "L3:0=3;1=3\n")

    def test_recovers_after_killed_daemon(self):
        os.mkdir(os.path.join(self.f.rc, "meat3_game"))
        self.f.put("schemata", "L3:0=3\n")
        r = self.make()
        self.assertEqual(self.f.get("schemata"), "L3:0=ffff\n")
        self.assertFalse(os.path.exists(r.game_dir))
        self.assertFalse(r.active)

    def test_no_cat_support(self):
        shutil.rmtree(os.path.join(self.f.rc, "info"))
        r = self.make()
        self.assertFalse(r.available)
        self.assertFalse(r.activate())
        self.assertIn("cat_l3", r.status())

    def test_disabled_never_activates(self):
        r = self.make(enabled=False)
        self.assertFalse(r.update(True, ["x.exe"]))
        self.assertFalse(r.active)
        self.assertEqual(r.status(), "off")

    # ------------------------------------------------------------------ with L3Apps
    def apps(self, r):
        a = l3apps.L3Apps(self.f.rc, proc_root=self.f.proc, top=4, reserve=r)
        a.hz, a.ncpu = 100, 1
        return a

    def test_game_moved_into_reserved_group_and_released_on_exit(self):
        r = self.make()
        a = self.apps(r)
        self.f.proc_add(100, "/usr/bin/wine64-preloader", 0, STEAM_ENV, argv0="C:\\Cyberpunk2077.exe",
                        tids=[100, 101])
        self.f.proc_add(200, "/usr/bin/firefox", 0)
        a.scan(0.0)
        self.assertFalse(r.active)                    # no CPU history yet
        self.f.ticks(100, 80)                         # 80% over 1 s
        a.scan(1.0)
        self.assertTrue(r.active)
        self.assertEqual(r.games, ["Cyberpunk2077.exe"])
        self.assertEqual(self.f.get("schemata"), "L3:0=3\n")
        tids = lambda rel: sorted(int(x) for x in self.f.get(rel).split())  # noqa: E731
        self.assertEqual(tids("meat3_game/tasks"), [100, 101])
        self.assertEqual(tids("meat3_game/mon_groups/meat3_steam_1091500/tasks"), [100, 101])
        app = a.apps["steam:1091500"]
        self.assertEqual(app.parent, r.game_dir)
        # the game's occupancy is read from its group under meat3_game; totals include meat3_game
        self.f.occ(os.path.join(r.game_dir, "mon_groups", "meat3_steam_1091500"), 0, 70_000_000)
        self.f.occ(r.game_dir, 0, 70_000_000)
        res = a.sample(2.0)
        self.assertEqual(res["apps"][0]["occ"], {0: 70_000_000})
        self.assertEqual(res["total"], {0: 120_000_000})
        self.assertEqual(l3apps.ctrl_groups(self.f.rc), [self.f.rc, r.game_dir])
        # game exits -> reservation released, root back to the full cache
        shutil.rmtree(os.path.join(self.f.proc, "100"))
        a.scan(3.0)
        self.assertFalse(r.active)
        self.assertEqual(self.f.get("schemata"), "L3:0=ffff\n")
        self.assertFalse(os.path.exists(r.game_dir))

    def test_idle_game_launcher_does_not_reserve(self):
        r = self.make()
        a = self.apps(r)
        self.f.proc_add(100, "/usr/bin/wine", 0, argv0="C:\\Launcher.exe")
        a.scan(0.0)
        self.f.ticks(100, 1)                          # 1% < RESERVE_MIN_CPU
        a.scan(1.0)
        self.assertFalse(r.active)
        self.assertEqual(r.status(), "waiting for a game")

    def test_disable_while_active_moves_game_back(self):
        r = self.make()
        a = self.apps(r)
        self.f.proc_add(100, "/usr/bin/game.exe", 0)
        a.scan(0.0)
        self.f.ticks(100, 90)
        a.scan(1.0)
        self.assertTrue(r.active)
        r.set_enabled(False)
        a.scan(2.0)
        self.assertEqual(self.f.get("schemata"), "L3:0=ffff\n")
        self.assertEqual(a.apps["game.exe"].parent, self.f.rc)    # monitoring group recreated under root
        a.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
