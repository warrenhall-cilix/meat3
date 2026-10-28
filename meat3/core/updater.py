"""Self-update, driven by the daemon: check GitHub for new commits, then pull + reinstall.

The installer records the git checkout it installed from in /opt/meat3/SOURCE. The daemon (root):

* check: `git fetch` in that checkout, run as the checkout's owner so .git never gets root-owned
  files, then counts and lists the commits not installed yet. Runs in a thread; status() is instant.
* start: launches /opt/meat3/update.sh (the root-owned copy the installer made) in its own transient
  systemd unit, because the installer restarts meat3d and would otherwise kill its own parent.
  update.sh pulls as the owner and runs the checkout's packaging/install.sh.

Only the checkout's owner (or root) may start an update: it runs that user's code as root, which they
could already do with sudo; other members of the meat3 group can't.
"""
from __future__ import annotations

import os
import pwd
import subprocess
import threading
import time
from typing import Callable, Optional

PREFIX = "/opt/meat3"
LOG = "/run/meat3/update.log"
UNIT = "meat3-update"


def _run(cmd: list[str], timeout: float = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


class Updater:
    def __init__(self, prefix: str = PREFIX, run: Callable = _run, log_path: str = LOG):
        self.prefix, self.run, self.log_path = prefix, run, log_path
        self.lock = threading.Lock()
        self.checking = False
        self.state: dict = {"checked": 0.0, "behind": None, "commits": [], "error": ""}

    # ------------------------------------------------------------------ source checkout
    def _read(self, name: str) -> str:
        try:
            with open(os.path.join(self.prefix, name)) as f:
                return f.read().strip()
        except OSError:
            return ""

    def source(self) -> tuple[Optional[str], Optional[str], str]:
        """(checkout path, owner name, error)."""
        src = self._read("SOURCE")
        if not src:
            return None, None, "installed without a git checkout (re-run packaging/install.sh from a git clone)"
        if not os.path.isdir(os.path.join(src, ".git")):
            return None, None, f"{src} is not a git checkout"
        try:
            owner = pwd.getpwuid(os.stat(src).st_uid).pw_name
        except (OSError, KeyError) as e:
            return None, None, str(e)
        return src, owner, ""

    def _git(self, src: str, owner: str, *args: str, timeout: float = 60) -> tuple[int, str]:
        cmd = ["git", "-C", src, *args]
        if os.geteuid() == 0 and owner != "root":
            cmd = ["runuser", "-u", owner, "--"] + cmd
        return self.run(cmd, timeout=timeout)

    def may_start(self, uid: int) -> bool:
        if uid == 0:
            return True
        src, owner, _ = self.source()
        try:
            return owner is not None and pwd.getpwuid(uid).pw_name == owner
        except KeyError:
            return False

    # ------------------------------------------------------------------ check
    def check(self, wait: bool = False) -> None:
        with self.lock:
            if self.checking:
                return
            self.checking = True
        t = threading.Thread(target=self._check, daemon=True)
        t.start()
        if wait:
            t.join()

    def _check(self) -> None:
        st = {"checked": time.time(), "behind": None, "commits": [], "error": ""}
        try:
            src, owner, err = self.source()
            if err:
                st["error"] = err
                return
            rc, out = self._git(src, owner, "fetch", "--quiet", timeout=60)
            if rc:
                st["error"] = "git fetch failed: " + out.splitlines()[-1] if out else "git fetch failed"
                return
            rc, out = self._git(src, owner, "log", "--format=%h %s", "HEAD..@{u}")
            if rc:
                st["error"] = "no upstream branch: " + out
                return
            commits = [ln for ln in out.splitlines() if ln.strip()]
            st["behind"], st["commits"] = len(commits), commits[:30]
        finally:
            with self.lock:
                self.state, self.checking = st, False

    # ------------------------------------------------------------------ update
    def running(self) -> bool:
        rc, out = self.run(["systemctl", "is-active", UNIT], timeout=5)
        return out.strip() in ("active", "activating")

    def start(self) -> tuple[bool, str]:
        src, owner, err = self.source()
        if err:
            return False, err
        script = os.path.join(self.prefix, "update.sh")
        if not os.path.isfile(script):
            return False, f"{script} missing (re-run packaging/install.sh once by hand)"
        if self.running():
            return False, "an update is already running"
        self.run(["systemctl", "reset-failed", UNIT], timeout=5)
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            open(self.log_path, "w").close()
        except OSError:
            pass
        rc, out = self.run(["systemd-run", f"--unit={UNIT}", "--collect", "--service-type=exec",
                            f"--setenv=SUDO_USER={owner}", f"--property=StandardOutput=append:{self.log_path}",
                            f"--property=StandardError=append:{self.log_path}",
                            "/bin/bash", script, src], timeout=15)
        if rc:
            return False, "systemd-run failed: " + out
        return True, "update started; meat3d restarts when it finishes"

    def log_tail(self, n: int = 40) -> list[str]:
        try:
            with open(self.log_path, errors="replace") as f:
                return f.read().splitlines()[-n:]
        except OSError:
            return []

    def status(self) -> dict:
        with self.lock:
            st = dict(self.state, checking=self.checking)
        st["installed"] = self._read("VERSION") or "unknown"
        st["running"] = self.running()
        st["log"] = self.log_tail()
        return st
