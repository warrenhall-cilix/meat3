"""Daemon-side configuration: /etc/meat3/config.json (override path with MEAT3_CONFIG)."""
from __future__ import annotations

import copy
import json
import os
import threading

PATH = os.environ.get("MEAT3_CONFIG", "/etc/meat3/config.json")

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
    # AMD Ryzen SMU overclocking (Curve Optimizer, PBO PPT/TDC/EDC) via ryzen_smu.
    # Validated on Granite Ridge (9800X3D): CO all/per-core write+readback+stress, PPT write+readback.
    "amd_smu_controls": True,
    # Safety guards evaluated by the daemon every poll. sensor: glob on sensor id ("*" wildcard),
    # trips when value > above for `count` consecutive polls; then applies control glob := value.
    "guards": [
        {"name": "12V-2x6 pin overcurrent", "sensor": "nv*.pin[0-9].a", "above": 8.5, "count": 2,
         "control": "nv*.oc.power_limit", "value": 300, "enabled": True},
        {"name": "GPU hotspot overtemp", "sensor": "nv*.t.hotspot", "above": 100, "count": 3,
         "control": "nv*.oc.power_limit", "value": 300, "enabled": True},
        {"name": "GPU memory overtemp", "sensor": "nv*.t.mem", "above": 95, "count": 3,
         "control": "nv*.oc.power_limit", "value": 300, "enabled": True},
        {"name": "AMD GPU hotspot (junction) overtemp", "sensor": "amdgpu*.hwmon.temp2", "above": 105, "count": 3,
         "control": "amdgpu*.oc.power_cap", "value": "min", "enabled": True},
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
