"""Tests for per-app L3 occupancy (meat3/backends/l3apps.py) against fake /proc and resctrl trees.

    python3 tests/test_l3apps.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from meat3.backends import l3apps  # noqa: E402


class Fake:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="meat3-l3apps-")
        self.proc = os.path.join(self.tmp, "proc")
        self.rc = os.path.join(self.tmp, "resctrl")
        os.makedirs(os.path.join(self.rc, "mon_groups"))
        self.occ(self.rc, 0, 50_000_000)

    def occ(self, group_dir, dom, val):
        d = os.path.join(group_dir, "mon_data", f"mon_L3_{dom:02d}")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "llc_occupancy"), "w") as f:
            f.write(f"{val}\n")

    def proc_add(self, pid, exe, ticks, env=b"", argv0=None, tids=None, ppid=1):
        p = os.path.join(self.proc, str(pid))
        os.makedirs(os.path.join(p, "task"), exist_ok=True)
        for t in tids or [pid]:
            os.makedirs(os.path.join(p, "task", str(t)), exist_ok=True)
        if not os.path.lexists(os.path.join(p, "exe")):
            os.symlink(exe, os.path.join(p, "exe"))
        with open(os.path.join(p, "environ"), "wb") as f:
            f.write(env)
        with open(os.path.join(p, "cmdline"), "wb") as f:
            f.write((argv0 or exe).encode() + b"\0")
        self.ticks(pid, ticks, ppid)

    def ticks(self, pid, ticks, ppid=1):
        # pid (comm) state ppid pgrp session tty tpgid flags minflt cminflt majflt cmajflt utime stime ...
        fields = ["S", str(ppid)] + ["0"] * 9 + [str(ticks), "0"] + ["0"] * 6 + ["12345"]
        with open(os.path.join(self.proc, str(pid), "stat"), "w") as f:
            f.write(f"{pid} (x) " + " ".join(fields) + "\n")

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestL3Apps(unittest.TestCase):
    def setUp(self):
        self.f = Fake()
        self.a = l3apps.L3Apps(self.f.rc, proc_root=self.f.proc, top=2)
        self.a.hz, self.a.ncpu = 100, 1

    def tearDown(self):
        self.f.close()

    def groups(self):
        return sorted(os.listdir(os.path.join(self.f.rc, "mon_groups")))

    def test_steam_game_tracked_and_named_after_exe(self):
        env = b"HOME=/h\0SteamAppId=1091500\0"
        self.f.proc_add(100, "/usr/bin/wine64-preloader", 0, env, argv0="Z:\\games\\Cyberpunk2077.exe")
        self.f.proc_add(101, "/usr/bin/wineserver", 0, env)
        self.a.scan(0.0)
        self.assertEqual(self.groups(), ["meat3_steam_1091500"])       # games tracked immediately
        app = self.a.apps["steam:1091500"]
        self.assertTrue(app.game)
        self.assertEqual(app.name, "Cyberpunk2077.exe")
        self.assertEqual(sorted(app.pids), [100, 101])

    def test_children_inherit_game(self):
        self.f.proc_add(600, "/usr/bin/launcher", 0, b"SteamAppId=42\0")
        self.f.proc_add(601, "/usr/bin/worker", 0, b"", ppid=600)       # env wiped by setproctitle
        self.a.scan(0.0)
        self.assertEqual(sorted(self.a.apps["steam:42"].pids), [600, 601])

    def test_top_apps_by_cpu_and_threads_assigned(self):
        self.f.proc_add(200, "/usr/bin/firefox", 0, tids=[200, 201, 202])
        self.f.proc_add(300, "/usr/bin/idle-thing", 0)
        self.a.scan(0.0)
        self.assertEqual(self.groups(), [])                             # no CPU history yet
        self.f.ticks(200, 50)                                           # 50% of one CPU over 1 s
        self.a.scan(1.0)
        self.assertEqual(self.groups(), ["meat3_firefox"])
        tasks = os.path.join(self.f.rc, "mon_groups", "meat3_firefox", "tasks")
        open(tasks, "w").close()                                        # the kernel provides this file
        self.f.ticks(200, 100)
        self.a.scan(2.0)
        with open(tasks) as fh:
            self.assertEqual(sorted(int(x) for x in fh.read().split()), [200, 201, 202])
        g = os.path.join(self.f.rc, "mon_groups", "meat3_firefox")
        self.f.occ(g, 0, 12_000_000)
        res = self.a.sample(3.0)
        self.assertEqual(res["apps"][0]["name"], "firefox")
        self.assertEqual(res["apps"][0]["occ"], {0: 12_000_000})
        self.assertEqual(res["total"], {0: 50_000_000})

    def test_kernel_threads_and_exited_apps_dropped(self):
        self.f.proc_add(2, "/x", 0)                    # kthreadd
        self.f.proc_add(400, "/usr/bin/game.exe", 0, ppid=2)
        self.f.proc_add(500, "/usr/bin/hl2.exe", 0)
        self.a.scan(0.0)
        self.assertEqual(self.groups(), ["meat3_hl2_exe"])              # *.exe = game; ppid 2 skipped
        shutil.rmtree(os.path.join(self.f.proc, "500"))
        self.a.scan(1.0)
        self.assertEqual(self.groups(), [])
        self.assertNotIn("hl2.exe", self.a.apps)

    def test_stale_groups_from_previous_run_removed(self):
        os.mkdir(os.path.join(self.f.rc, "mon_groups", "meat3_old"))
        os.mkdir(os.path.join(self.f.rc, "mon_groups", "users_own"))
        l3apps.L3Apps(self.f.rc, proc_root=self.f.proc)
        self.assertEqual(self.groups(), ["users_own"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
