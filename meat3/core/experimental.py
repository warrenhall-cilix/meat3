"""Experimental features behind the GUI's disclaimer: SMU firmware pin status/run, VRM write switches,
scale caps and guard thresholds. Daemon side only (no Qt); the GUI talks to it through meat3d.

Settings are written to the user's config file with config.update_user (never the merged defaults),
and the daemon tells backends to re-read them (Backend.reload_config) so no restart is needed.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import Any, Optional

from . import config

HARD_MAX_SCALE = 1.5          # mirrors backends/amdgpu_vrm.HARD_MAX_SCALE; the control range never exceeds it

# key path -> (kind, lo, hi); kind: bool | float | choice
SETTINGS: dict[str, tuple] = {
    "experimental.accepted": ("bool",),
    "amdgpu_vrm.allow_writes": ("bool",),
    "amdgpu_vrm.restore_on_stop": ("bool",),
    "amdgpu_vrm.experimental_mps_writes": ("bool",),
    "amdgpu_vrm.max_tbp_scale": ("float", 1.0, HARD_MAX_SCALE),
    "amdgpu_vrm.max_tdc_scale": ("float", 1.0, HARD_MAX_SCALE),
    "amdgpu_vrm.max_offset_mv": ("float", 0.0, 100.0),
    "amdgpu_vrm.mps_offset_lsb_mv": ("choice", (None, 5, 6.25)),
}
GUARD_FIELDS = {"above": ("float", -1e9, 1e9), "enabled": ("bool",)}


def _coerce(spec: tuple, value: Any):
    kind = spec[0]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if value in (0, 1):
            return bool(value)
        raise ValueError("expected true/false")
    if kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("expected a number")
        v = float(value)
        if not spec[1] <= v <= spec[2]:
            raise ValueError(f"{v:g} outside {spec[1]:g}..{spec[2]:g}")
        return v
    if kind == "choice":
        if value in spec[1]:
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) in spec[1]:
            return float(value) if float(value) != int(value) else int(value)
        raise ValueError(f"expected one of {spec[1]}")
    raise ValueError("bad spec")


def validate(settings: dict, guards: Optional[dict] = None) -> dict:
    """{dotted key: value} + {guard name: {above, enabled}} -> patch for config.update_user. Raises ValueError."""
    patch: dict = {}
    for key, value in (settings or {}).items():
        spec = SETTINGS.get(key)
        if spec is None:
            raise ValueError(f"{key}: not an experimental setting")
        v = _coerce(spec, value)
        top, sub = key.split(".", 1)
        patch.setdefault(top, {})[sub] = v
    names = {g.get("name") for g in config.get().get("guards") or []}
    for name, fields in (guards or {}).items():
        if name not in names:
            raise ValueError(f"guard {name!r} does not exist")
        if not isinstance(fields, dict):
            raise ValueError(f"guard {name!r}: expected an object")
        out = {}
        for f, value in fields.items():
            spec = GUARD_FIELDS.get(f)
            if spec is None:
                raise ValueError(f"guard {name!r}: field {f!r} cannot be set here")
            out[f] = _coerce(spec, value)
        if out:
            patch.setdefault("guard_overrides", {})[name] = out
    return patch


def apply(settings: dict, guards: Optional[dict] = None, reset_guards: bool = False) -> dict:
    """Validate, write the user's config file and reload. reset_guards drops a user `guards` list and all
    overrides so the built-in list applies again."""
    patch = validate(settings, guards)
    if reset_guards:
        patch["guards"] = None
        patch["guard_overrides"] = None
    return config.update_user(patch)


def effective() -> dict:
    cfg = config.get()
    vrm = cfg.get("amdgpu_vrm") or {}
    return {
        "accepted": bool((cfg.get("experimental") or {}).get("accepted", False)),
        "hard_max_scale": HARD_MAX_SCALE,
        "amdgpu_vrm": {k: vrm.get(k) for k in ("allow_writes", "restore_on_stop", "experimental_mps_writes",
                                               "max_tbp_scale", "max_tdc_scale", "max_offset_mv",
                                               "mps_offset_lsb_mv")},
        "guards": [{"name": g.get("name"), "sensor": g.get("sensor"), "above": g.get("above"),
                    "count": g.get("count", 1), "control": g.get("control"), "value": g.get("value"),
                    "enabled": bool(g.get("enabled", True))} for g in cfg.get("guards") or [] if isinstance(g, dict)],
        "user_guards_list": isinstance(config.user_settings().get("guards"), list),
    }


# ----------------------------------------------------------------------------- SMU firmware pin
def smufw_state(root: str = "/", cache: Optional[str] = None) -> dict:
    """Structured version of `meat3-smufw` (status): one entry per RDNA4 device."""
    from .. import smufw
    cache = cache or smufw.CACHE
    devs = []
    for pci, ver, mp1 in smufw.running_pmfw(root):
        name = smufw.fw_name(mp1)
        if not name:
            continue
        d: dict = {"pci": pci, "pmfw": ver, "pmfw_hex": f"0x{ver:06x}", "vrm_bus_open": ver <= smufw.PMFW_GOOD,
                   "good_pmfw_hex": f"0x{smufw.PMFW_GOOD:06x}", "file": name, "installed": None, "pinned": None,
                   "kernel_loadable": True, "backup": False, "reboot_needed": False, "note": ""}
        inst = smufw.find_installed(name, root)
        if inst:
            d["installed"] = inst
            try:
                if inst.endswith(".zst"):
                    with open(inst, "rb") as f:
                        d["kernel_loadable"] = smufw.zstd_kernel_loadable(f.read(6))
                d["pinned"] = smufw.sha256(smufw.decompress(inst)) == smufw.KNOWN_GOOD.get(name)
            except Exception as e:  # unreadable / zstd missing
                d["note"] = str(e)
            d["backup"] = os.path.exists(smufw.backup_path(inst, cache))
            if d["pinned"] and ver > smufw.PMFW_GOOD:
                d["reboot_needed"] = True
                d["note"] = "known-good file installed; reboot to run it"
            elif d["pinned"] is False and ver <= smufw.PMFW_GOOD:
                d["note"] = "running the older firmware but the installed file is the distribution's: reboot to go back"
            if not d["kernel_loadable"]:
                d["note"] = "installed .zst has no frame content size: the kernel refuses it at boot; re-pin or restore"
        else:
            d["note"] = f"{name} not installed under {', '.join(smufw.FW_DIRS)}"
        devs.append(d)
    return {"devices": devs, "hook": os.path.exists(os.path.join(root, smufw.HOOK.lstrip("/")))}


class SmufwRunner:
    """Runs `meat3-smufw --pin|--restore` in a thread (as whoever runs the daemon: root) and keeps its output."""

    def __init__(self, python: str = sys.executable, cwd: Optional[str] = None, timeout: float = 600.0):
        self.python, self.timeout = python, timeout
        self.cwd = cwd or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self.action = ""
        self.log: list[str] = []
        self.rc: Optional[int] = None
        self.started = 0.0
        self.finished = 0.0

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, action: str) -> tuple[bool, str]:
        if action not in ("pin", "restore"):
            return False, f"unknown action {action!r}"
        with self.lock:
            if self.running():
                return False, f"{self.action} is still running"
            self.action, self.log, self.rc = action, [], None
            self.started, self.finished = time.time(), 0.0
            self.thread = threading.Thread(target=self._run, args=(action,), daemon=True)
            self.thread.start()
        return True, f"{action} started"

    def _run(self, action: str) -> None:
        cmd = [self.python, "-m", "meat3.smufw", "--" + action]
        try:
            p = subprocess.Popen(cmd, cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 env=dict(os.environ, PYTHONUNBUFFERED="1"))
            assert p.stdout is not None
            for line in p.stdout:
                with self.lock:
                    self.log.append(line.rstrip("\n"))
            rc = p.wait(timeout=self.timeout)
        except (OSError, subprocess.SubprocessError) as e:
            with self.lock:
                self.log.append(f"!! {e}")
            rc = 1
        with self.lock:
            self.rc, self.finished = rc, time.time()

    def status(self) -> dict:
        with self.lock:
            return {"action": self.action, "running": self.running(), "rc": self.rc, "log": list(self.log[-80:]),
                    "started": self.started, "finished": self.finished}
