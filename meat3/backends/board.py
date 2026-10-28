"""Board / generic hwmon backend.

Exposes every /sys/class/hwmon device except the ones owned by other backends:
  * GPUs (amdgpu, nvidia, nouveau, radeon, i915, xe, and anything whose parent device is a
    PCI display-class function) -> GPU backends
  * CPU sensors (k10temp, zenpower, coretemp) -> cpu_amd / cpu_generic

Drives (nvme, drivetemp) belong to the storage backend.

That covers Super I/O chips (nct6775/nct6687/it87/...: fans, PWM duty, voltages, temps), DDR5 SPD hub sensors (spd5118), network/Wi-Fi (r8169, ath1xk, iwlwifi), USB
controllers, ACPI thermal zones, PSU/EC drivers, etc.

Read-only. Sensor ids are derived from the hwmon `name` plus the parent device's sysfs
basename (e.g. PCI address / i2c address), not from the unstable hwmonN index.
"""
from __future__ import annotations

import os
import re
import time
from meat3.core.model import Backend, Sensor
from meat3.backends._cpu_common import (_listdir, hwmon_channels, read_hwmon_value, read_int, read_str, slug)
from meat3.backends import board_maps

EXCLUDE_NAMES = {
    "amdgpu", "nvidia", "nouveau", "radeon", "i915", "xe",     # GPU backends
    "k10temp", "zenpower", "coretemp",                         # CPU backends
    "nvme", "drivetemp",                                       # storage backend
}

RESCAN_S = 30.0


def _pretty_chip(name: str, hw: str) -> str:
    """Human readable group name for one hwmon device."""
    dev = os.path.join(hw, "device")
    if name == "nvme":
        model = read_str(os.path.join(dev, "model"))
        nd = os.path.basename(os.path.realpath(dev)) if os.path.exists(dev) else ""
        return f"NVMe {nd}: {model}" if model else f"NVMe {nd}".strip()
    if name == "spd5118":
        addr = os.path.basename(os.path.realpath(dev)) if os.path.exists(dev) else ""
        m = re.match(r"(\d+)-00(5[0-7])$", addr)
        if m:
            return f"DIMM SPD hub (slot addr 0x{m.group(2)}, bus {m.group(1)})"
        return f"DIMM SPD hub {addr}".strip()
    if name.startswith("ath") or name.startswith("iwlwifi") or name.startswith("mt7"):
        return f"Wi-Fi ({name})"
    if name.startswith("r8169") or name.startswith("igc") or name.startswith("atlantic"):
        return f"Ethernet ({name})"
    if name.startswith("nct") or name.startswith("it8") or name.startswith("f71") or name.startswith("w83"):
        return f"Super I/O {name.split('_', 1)[0].upper()}"     # it8696_a008090a: drop Gigabyte's SIV id
    if name.endswith("_xhci"):
        return f"USB controller ({name})"
    if name == "acpitz":
        return "ACPI thermal zone"
    return name


class BoardBackend(Backend):
    kind = "board"

    def __init__(self, sys_root: str = "/sys", clock=time.monotonic):
        self.sys_root = sys_root
        self.clock = clock
        vendor = read_str(os.path.join(sys_root, "class/dmi/id/board_vendor")) or ""
        prod = read_str(os.path.join(sys_root, "class/dmi/id/board_name")) or ""
        self.vendor, self.board = vendor, prod
        self.name = (f"{vendor} {prod}".strip() or "Motherboard")
        self.chips: list[dict] = []
        self.last_scan = -1e9
        self._scan()

    @classmethod
    def detect(cls, sys_root: str = "/sys") -> list["BoardBackend"]:
        try:
            b = cls(sys_root)
            return [b] if b.chips else []
        except Exception:
            return []

    # -- discovery
    @staticmethod
    def _excluded(name: str, hw: str) -> bool:
        if name in EXCLUDE_NAMES:
            return True
        cls = read_str(os.path.join(hw, "device/class"))
        if cls and cls.startswith("0x03"):          # PCI display controller
            return True
        return False

    def _scan(self) -> None:
        base = os.path.join(self.sys_root, "class/hwmon")
        chips = []
        used_ids: dict[str, int] = {}
        for d in _listdir(base):
            hw = os.path.join(base, d)
            name = read_str(os.path.join(hw, "name"))
            if not name or self._excluded(name, hw):
                continue
            dev = os.path.join(hw, "device")
            devid = os.path.basename(os.path.realpath(dev)) if os.path.exists(dev) else d
            cid = slug(f"{name}_{devid}")
            n = used_ids.get(cid, 0)
            used_ids[cid] = n + 1
            if n:
                cid = f"{cid}_{n}"
            pretty = _pretty_chip(name, hw)
            chans = hwmon_channels(hw)
            pwms = sorted((f for f in _listdir(hw) if re.fullmatch(r"pwm\d+", f)), key=lambda f: int(f[3:]))
            if not chans and not pwms:
                continue
            chips.append({"id": cid, "name": name, "path": hw, "pretty": pretty, "chans": chans,
                          "pwms": pwms, "hwmon": d, "devid": devid, "fit": {},
                          "map": board_maps.lookup(name, self.vendor, self.board)})
        # stable ordering: Super I/O first, then the rest by pretty name
        chips.sort(key=lambda c: (0 if c["pretty"].startswith("Super I/O") else 1, c["pretty"], c["id"]))
        # disambiguate identical group names (two DIMMs on the same addr can't happen, but
        # e.g. two prom21_xhci controllers do)
        seen: dict[str, int] = {}
        for c in chips:
            k = seen.get(c["pretty"], 0)
            seen[c["pretty"]] = k + 1
        dup = {p for p, k in seen.items() if k > 1}
        cnt: dict[str, int] = {}
        for c in chips:
            if c["pretty"] in dup:
                i = cnt.get(c["pretty"], 0) + 1
                cnt[c["pretty"]] = i
                c["pretty"] = f"{c['pretty']} [{c['devid']}]" if c["devid"] != c["hwmon"] else f"{c['pretty']} #{i}"
        self.chips = chips
        self.last_scan = self.clock()

    # -- polling
    def sensors(self) -> list[Sensor]:
        now = self.clock()
        if now - self.last_scan > RESCAN_S:
            self._scan()
        out: list[Sensor] = []
        for c in self.chips:
            group = f"{self.name}/{c['pretty']}"
            hint_base = f"hwmon {c['name']} ({c['hwmon']})"
            entry = c["map"]
            if entry:
                hint_base += f"; names from the {entry['name']} map" + (" (vendor default; +12V/+5V/+3.3V/battery rails are range-checked)" if entry.get("guess") else "")
            for ch in c["chans"]:
                drv = ch["label"] if ch["label"] != ch["key"] else None
                d = board_maps.describe(c["name"], ch["key"], drv, entry)
                if d["hidden"]:
                    continue
                raw = read_hwmon_value(ch)
                v, label, hint = raw, d["label"], hint_base
                if ch["kind"] == "in" and raw is not None:
                    if d["mapped"] and d["mult"] != 1.0:
                        fit = c["fit"].get(ch["key"])
                        if fit is None:                   # decided once per rescan so the row doesn't flip
                            fit = c["fit"][ch["key"]] = board_maps.plausible(label, raw * d["mult"])
                        if fit:
                            v = raw * d["mult"]
                            hint += f"; pin {raw:.3f} V x {d['mult']:g} (board divider)"
                        else:
                            label = f"{ch['key']} (raw pin)"
                            hint += f"; the {d['label']} divider (x{d['mult']:g}) didn't fit this board, showing the pin voltage"
                    elif not d["mapped"] and c["pretty"].startswith("Super I/O"):
                        hint += "; raw Super I/O pin voltage, this board's divider is unknown"
                if ch["kind"] == "temp" and board_maps.OPEN_PROBE(raw) and c["pretty"].startswith("Super I/O"):
                    v = None
                    hint += f"; reads {raw:.0f} °C: no probe connected to this header"
                if drv and drv != label:
                    hint += f"; driver label '{drv}'"
                out.append(Sensor(f"board.{c['id']}.{ch['key']}", group, label, v, ch["unit"], ch["dec"], hint))
            for p in c["pwms"]:
                raw = read_int(os.path.join(c["path"], p))
                mode = read_int(os.path.join(c["path"], f"{p}_enable"))
                drv = read_str(os.path.join(c["path"], f"{p}_label"))
                d = board_maps.describe(c["name"], p, drv, entry)
                if d["hidden"]:
                    continue
                label = d["label"] if (d["mapped"] or drv) else f"{p.upper()} duty"
                hint = f"{hint_base}; {p} raw 0-255"
                if mode is not None:
                    hint += {0: ", full speed (no control)", 1: ", manual", 2: ", automatic"}.get(
                        mode, f", mode {mode}")
                out.append(Sensor(f"board.{c['id']}.{p}", group, label,
                                  None if raw is None else raw * 100.0 / 255.0, "%", 0, hint))
        return out


# daemon entry point convention: module-level BACKENDS list
BACKENDS = [BoardBackend]
