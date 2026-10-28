import os
import shutil
import tempfile
import unittest
from unittest import mock

from meat3.core import updater as U


class FakeRun:
    def __init__(self, log="", fetch_rc=0, active="inactive", sysrun_rc=0):
        self.calls = []
        self.log, self.fetch_rc, self.active, self.sysrun_rc = log, fetch_rc, active, sysrun_rc

    def __call__(self, cmd, timeout=60):
        self.calls.append(cmd)
        if "fetch" in cmd:
            return self.fetch_rc, "fatal: unable to access github" if self.fetch_rc else ""
        if "log" in cmd:
            return 0, self.log
        if cmd[:2] == ["systemctl", "is-active"]:
            return (0 if self.active == "active" else 3), self.active
        if cmd[0] == "systemd-run":
            return self.sysrun_rc, "" if not self.sysrun_rc else "Failed to start transient service unit"
        return 0, ""


class TestUpdater(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meat3-upd-")
        self.prefix = os.path.join(self.tmp, "opt")
        self.src = os.path.join(self.tmp, "meat3")
        os.makedirs(self.prefix)
        os.makedirs(os.path.join(self.src, ".git"))
        with open(os.path.join(self.prefix, "VERSION"), "w") as f:
            f.write("e9a4e03\n")
        with open(os.path.join(self.prefix, "SOURCE"), "w") as f:
            f.write(self.src + "\n")
        with open(os.path.join(self.prefix, "update.sh"), "w") as f:
            f.write("#!/bin/bash\n")
        self.log = os.path.join(self.tmp, "update.log")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def up(self, run):
        return U.Updater(prefix=self.prefix, run=run, log_path=self.log)

    def test_check_lists_new_commits(self):
        run = FakeRun(log="1c2d61d unwrap GFX current\n7fca1d8 VRM unreachable\n")
        u = self.up(run)
        u.check(wait=True)
        st = u.status()
        self.assertEqual((st["installed"], st["behind"], st["error"], st["checking"]), ("e9a4e03", 2, "", False))
        self.assertEqual(st["commits"][0], "1c2d61d unwrap GFX current")
        fetch = next(c for c in run.calls if "fetch" in c)
        self.assertIn(self.src, fetch)
        if os.geteuid() == 0:                       # git runs as the checkout's owner, never as root
            self.assertEqual(fetch[0], "runuser")

    def test_up_to_date_and_fetch_error(self):
        u = self.up(FakeRun(log=""))
        u.check(wait=True)
        self.assertEqual(u.status()["behind"], 0)
        u = self.up(FakeRun(fetch_rc=1))
        u.check(wait=True)
        st = u.status()
        self.assertIsNone(st["behind"])
        self.assertIn("git fetch failed", st["error"])

    def test_not_a_git_install(self):
        os.remove(os.path.join(self.prefix, "SOURCE"))
        u = self.up(FakeRun())
        u.check(wait=True)
        self.assertIn("without a git checkout", u.status()["error"])
        self.assertFalse(u.start()[0])

    def test_start_runs_in_own_unit(self):
        run = FakeRun()
        ok, msg = self.up(run).start()
        self.assertTrue(ok, msg)
        sr = next(c for c in run.calls if c[0] == "systemd-run")
        self.assertIn(f"--unit={U.UNIT}", sr)
        self.assertEqual(sr[-3:], ["/bin/bash", os.path.join(self.prefix, "update.sh"), self.src])
        self.assertTrue(any(a.startswith("--setenv=SUDO_USER=") for a in sr))

    def test_start_refused_while_running_or_on_failure(self):
        self.assertEqual(self.up(FakeRun(active="active")).start(), (False, "an update is already running"))
        ok, msg = self.up(FakeRun(sysrun_rc=1)).start()
        self.assertFalse(ok)
        self.assertIn("systemd-run failed", msg)

    def test_only_owner_may_start(self):
        u = self.up(FakeRun())
        me = os.stat(self.src).st_uid
        self.assertTrue(u.may_start(me))
        self.assertTrue(u.may_start(0))
        with mock.patch.object(U.pwd, "getpwuid", side_effect=lambda uid: mock.Mock(pw_name="owner" if uid == me else "other")):
            self.assertFalse(u.may_start(me + 1))

    def test_log_tail(self):
        with open(self.log, "w") as f:
            f.write("\n".join(f"line {i}" for i in range(100)))
        self.assertEqual(self.up(FakeRun()).log_tail(3), ["line 97", "line 98", "line 99"])


if __name__ == "__main__":
    unittest.main()
