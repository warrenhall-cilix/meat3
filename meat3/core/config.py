"""Daemon-side configuration: /etc/meat3/config.json (override path with MEAT3_CONFIG)."""
from __future__ import annotations

import copy
import json
import os
import threading

PATH = os.environ.get("MEAT3_CONFIG", "/etc/meat3/config.json")
# apply_on_start crash guard: written before applying, removed after BOOT_APPLY_GRACE_S of uptime
# or on a clean daemon stop. Still present at the next start = that boot died -> skip applying.
BOOT_APPLY_MARKER = "/var/lib/meat3/boot_apply.pending"
BOOT_APPLY_GRACE_S = 300

DEFAULTS = {
    # Board power/current correction for shunt-modded cards, keyed by PCI address ("0000:01:00.0")
    # or "*" for all NVIDIA GPUs. Real = reported x factor. 1.0 = stock card.
    "nvidia_shunt_factor": {},
    # 12V-2x6 per-pin current thresholds (A) used for colour cues and warnings
    "pin_warn_a": 8.0,
    "pin_crit_a": 9.0,
    # OSD
    "osd": {
        "enabled": False,
        "hotkey": ["KEY_RIGHTCTRL", "KEY_F12"],
        "items": [],
    },
    "poll_interval_s": 1.0,
    # L3 cache occupancy per application/game (AMD/Intel resctrl monitoring groups; root daemon).
    # top: how many of the busiest apps get their own group; game_patterns: process names that
    # count as games besides Steam titles (detected via SteamAppId).
    "l3_per_app": {"enabled": True, "top": 8, "game_patterns": ["*.exe"]},
    # L3 reservation (resctrl cache allocation, cat_l3): while a game runs, background apps may only
    # fill `background_mb` of L3 and the game gets the rest exclusively. Needs l3_per_app (game
    # detection). Rounded to whole L3 ways (6 MB each on a 9800X3D), at most half the cache.
    "l3_reserve": {"enabled": True, "background_mb": 12},
    # Per-core IPC and pipeline stall breakdown from the core PMU (counting mode, negligible cost).
    # per_app ("off" | "game" | "all"): IPC/back-end/front-end per program, counted on each program's
    # busy threads (>= min_thread_pct of a core, at most max_threads, skipping threads that switch
    # more than max_switches/s). Costs ~1 us per context switch of a counted thread.
    "cpu_topdown": {"enabled": True, "per_app": "game", "max_threads": 16, "min_thread_pct": 1.0,
                    "max_switches": 20000},
    # RDNA4 VRM controllers over PMBus (GPU's own "AMDGPU SMU" I2C buses, addresses 0x22/0x24/0x26).
    # Telemetry is read-only. Voltage offset writes are off unless allow_writes is true, and are
    # clamped to +-max_offset_mv. max_current_scale caps the current-reporting scale (raises the
    # firmware current limit). All VRM settings are volatile: a power cycle restores the factory values.
    "amdgpu_vrm": {"enabled": True, "allow_writes": False, "max_offset_mv": 50, "max_current_scale": 1.5},
    # AMD Ryzen SMU overclocking (Curve Optimizer, PBO PPT/TDC/EDC) via ryzen_smu.
    # Validated on Granite Ridge (9800X3D): CO all/per-core write+readback+stress, PPT write+readback.
    "amd_smu_controls": True,
    # Manual overclocking through the SMU (Ryzen Master's manual mode). vid_encoding stays null until
    # `meat3-bin --oc-probe` has verified it on this chip ("svi3"), with the SMU firmware it was verified
    # on; the voltage control is locked until then. max_vid caps the voltage control (hard cap 1.45 V).
    "manual_oc": {"vid_encoding": None, "verified_smu": None, "max_vid": 1.40},
    # Controls applied when meat3d starts, e.g. {"cpu0.smu.co_core0": -20} (meat3-cotune --save-boot).
    "apply_on_start": {},
    # Safety guards evaluated by the daemon every poll. sensor: glob on sensor id ("*" wildcard),
    # trips when value > above for `count` consecutive polls; then applies control glob := value.
    "guards": [
        {"name": "12V-2x6 pin overcurrent", "sensor": "nv*.pin[0-9].a", "above": 8.5, "count": 2,
         "control": "nv*.oc.power_limit", "value": 300, "enabled": True},
        {"name": "GPU hotspot overtemp", "sensor": "nv*.t.hotspot", "above": 100, "count": 3,
         "control": "nv*.oc.power_limit", "value": 300, "enabled": True},
        {"name": "GPU memory overtemp", "sensor": "nv*.t.mem", "above": 95, "count": 3,
         "control": "nv*.oc.power_limit", "value": 300, "enabled": True},
        # manual overclocking switches off the CPU's own boost management: leave it before the CPU gets hot
        {"name": "Manual CPU overclock overtemp", "sensor": "cpu*.k10temp.temp1", "above": 90, "count": 2,
         "control": "cpu*.smu.oc_mode", "value": 0, "enabled": True},
        {"name": "AMD GPU hotspot (junction) overtemp", "sensor": "amdgpu*.hwmon.temp2", "above": 105, "count": 3,
         "control": "amdgpu*.oc.power_cap", "value": "min", "enabled": True},
        # for RDNA4 cards with the board-power shunts bypassed: the power cap no longer bites, so cut
        # the GPU clock instead. Set "above" to the real power you allow, then enable it.
        {"name": "AMD real power (VRM) over limit", "sensor": "amdgpu*.vrm.real_power", "above": 450, "count": 3,
         "control": "amdgpu*.oc.od.sclk_offset", "value": "min", "enabled": False},
    ],
}

_lock = threading.Lock()
_cfg = None


def load() -> dict:
    global _cfg
    with _lock:
        cfg = copy.deepcopy(DEFAULTS)
        try:
            with open(PATH) as f:
                user = json.load(f)
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            print(f"meat3: config {PATH} unreadable ({e}); using defaults")
        for g in cfg.get("guards") or []:
            # 0.1 shipped "nv*.pin?.a", which also matched the connector total "nv0.pins.a"
            if isinstance(g, dict) and g.get("sensor") == "nv*.pin?.a":
                g["sensor"] = "nv*.pin[0-9].a"
            # "guard_overrides": {"<guard name>": {"above": 420, "enabled": true}} tweaks one guard
            # without copying the whole list
            if isinstance(g, dict) and isinstance(cfg.get("guard_overrides"), dict):
                g.update(cfg["guard_overrides"].get(g.get("name"), {}) or {})
        _cfg = cfg
        return cfg


def get() -> dict:
    return _cfg if _cfg is not None else load()


def save(cfg: dict) -> None:
    global _cfg
    with _lock:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, PATH)
        _cfg = cfg


def shunt_factor(pci: str) -> float:
    m = get().get("nvidia_shunt_factor", {})
    try:
        return float(m.get(pci, m.get("*", 1.0)))
    except (TypeError, ValueError):
        return 1.0
