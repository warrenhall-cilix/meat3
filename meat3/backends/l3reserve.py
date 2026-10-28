"""L3 cache reservation for games via resctrl cache allocation (AMD L3 CAT, cpuinfo flag cat_l3).

While a game runs, the L3 ways are split in two exclusive parts:
  * the game's threads go into the control group /sys/fs/resctrl/meat3_game, which may allocate
    only in the upper ways (e.g. 14 of 16 = 84 MB on a 9800X3D);
  * everything else stays in the default (root) group, which is limited to the lower ways
    (default 12 MB).
Lookups still hit anywhere; the mask only controls where a thread may *fill* lines, so the
background can no longer evict the game's working set. Masks are exclusive on purpose: measured
on a 9800X3D with 4 cache-streaming background threads, a 64 MB random-access working set took
128 ns/access unreserved, 63 ns with the background limited but the game mask overlapping, and
10.6 ns (= running alone) with exclusive masks.

Invariant used for crash recovery: the root schemata is only restricted while meat3_game exists.
Activation creates the group before restricting root; deactivation restores root before removing
the group. A meat3_game left behind by a killed daemon therefore means "restore root to full".
"""
from __future__ import annotations

import os
import re
import threading
from typing import Optional

GROUP = "meat3_game"
_rmdir = os.rmdir            # tests swap this (resctrl removes non-empty group dirs, a real fs does not)


def _read(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _write(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)


class L3Reserve:
    def __init__(self, resctrl_base: str, enabled: bool = True, background_mb: float = 12,
                 domain_sizes: Optional[dict[int, int]] = None):
        self.base = resctrl_base
        self.game_dir = os.path.join(resctrl_base, GROUP)
        self.enabled = bool(enabled)
        self.error: Optional[str] = None           # permanent: no CAT support
        self.last_error: Optional[str] = None      # last activation failure
        self.active = False
        self.lock = threading.RLock()             # apply() runs on the socket thread, update() on the poll thread
        self.games: list[str] = []
        self.full = 0
        self.ways = 0
        self.domains: list[int] = []
        info = os.path.join(resctrl_base, "info", "L3")
        mask = _read(os.path.join(info, "cbm_mask"))
        root = _read(os.path.join(resctrl_base, "schemata")) or ""
        m = re.search(r"^\s*L3:(.*)$", root, re.M)
        if not mask or not m:
            self.error = "CPU/kernel without L3 cache allocation (cat_l3)"
        else:
            self.full = int(mask.strip(), 16)
            self.ways = bin(self.full).count("1")
            self.domains = [int(kv.split("=")[0]) for kv in m.group(1).split(";") if "=" in kv]
            if self.ways < 4 or self.full != (1 << self.ways) - 1:
                self.error = f"unsupported L3 way mask {mask.strip()}"
        sizes = domain_sizes or {}
        biggest = max(sizes.values()) if sizes else 0
        self.way_bytes = biggest // self.ways if self.ways and biggest else 0
        self.bg_ways = 2
        self.set_background_mb(background_mb)
        self._recover()

    # ------------------------------------------------------------------ configuration
    @property
    def available(self) -> bool:
        return self.error is None

    @property
    def way_mb(self) -> float:
        return self.way_bytes / 1048576 if self.way_bytes else 0.0

    @property
    def max_bg_ways(self) -> int:
        return max(1, self.ways // 2)

    def set_background_mb(self, mb: float) -> None:
        w = round(float(mb) / self.way_mb) if self.way_mb else 2
        with self.lock:
            self.bg_ways = max(1, min(self.max_bg_ways, w))
            if self.active:
                try:
                    self._write_masks()
                except OSError as e:
                    self.last_error = f"cannot change L3 split: {e.strerror or e}"

    def set_enabled(self, on: bool) -> None:
        with self.lock:
            self.enabled = bool(on)
            if not on:
                self.deactivate()

    def masks(self) -> tuple[int, int]:
        """(background mask, game mask): background in the low ways, game in the rest, no overlap."""
        bg = (1 << self.bg_ways) - 1
        return bg, self.full & ~bg

    def _line(self, mask: int) -> str:
        return "L3:" + ";".join(f"{d}={mask:x}" for d in self.domains) + "\n"

    # ------------------------------------------------------------------ resctrl state
    def _recover(self) -> None:
        if os.path.isdir(self.game_dir):
            try:
                _write(os.path.join(self.base, "schemata"), self._line(self.full))
            except OSError:
                pass
            try:
                _rmdir(self.game_dir)       # kernel moves its tasks back to the root group
            except OSError:
                pass

    def _write_masks(self) -> None:
        bg, game = self.masks()
        _write(os.path.join(self.game_dir, "schemata"), self._line(game))
        _write(os.path.join(self.base, "schemata"), self._line(bg))

    def activate(self) -> bool:
        with self.lock:
            return self._activate()

    def _activate(self) -> bool:
        if self.active:
            return True
        if not self.available:
            return False
        try:
            os.makedirs(self.game_dir, exist_ok=True)
            self._write_masks()
        except OSError as e:
            self._recover()
            self.last_error = f"cannot reserve L3: {e.strerror or e}"
            return False
        self.active, self.last_error = True, None
        return True

    def deactivate(self) -> None:
        with self.lock:
            if not self.active:
                return
            self.active = False
            self.games = []
            self._recover()

    def update(self, want: bool, games: list[str]) -> bool:
        """Called every scan; returns True when the active state changed."""
        with self.lock:
            was = self.active
            if want and self.enabled:
                self._activate()
                self.games = games if self.active else []
            else:
                self.deactivate()
            return was != self.active

    def status(self) -> str:
        if not self.available:
            return self.error or "unavailable"
        if not self.enabled:
            return "off"
        if not self.active:
            return self.last_error or "waiting for a game"
        return "active for " + (", ".join(self.games) or "game")

    def close(self) -> None:
        self.deactivate()
