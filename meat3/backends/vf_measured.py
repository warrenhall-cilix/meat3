"""Measured voltage/frequency curve.

RDNA 2+ exposes no editable V/F points, only offsets, so the only curve we can show is the one the card
actually runs: every poll, the live GFX voltage and clock are binned by voltage (5 mV) and the highest and
latest clock seen in each bin are kept. Under load this traces the firmware's V/F curve (plus the effect of
any offset / undervolt); the idle region is left out so the plot is not a cloud of low-clock points.

The result is served as a read-only 'curve' control with mode "measured":
  {"mode": "measured", "points": [{"i", "mv", "mhz", "n"}, ...], "samples": N, "bin_mv": 5}
"""
from __future__ import annotations

import threading
from typing import Optional

BIN_MV = 5
MIN_BUSY_PCT = 20.0        # ignore idle samples (clock gating makes the readings meaningless)
MIN_MHZ = 500.0
MIN_SAMPLES = 2            # a bin needs this many samples before it is plotted


class VfMeasured:
    def __init__(self, bin_mv: int = BIN_MV, min_busy: float = MIN_BUSY_PCT, min_mhz: float = MIN_MHZ):
        self.bin_mv = bin_mv
        self.min_busy = min_busy
        self.min_mhz = min_mhz
        self._lock = threading.Lock()
        self._bins: dict[int, list] = {}      # bin -> [max_mhz, last_mhz, n]
        self.samples = 0
        self.rejected = 0

    def add(self, mv: Optional[float], mhz: Optional[float], busy: Optional[float] = None) -> bool:
        """Record one sample. Returns True if it was accepted into a bin."""
        if mv is None or mhz is None:
            return False
        try:
            mv, mhz = float(mv), float(mhz)
        except (TypeError, ValueError):
            return False
        if mv <= 0 or mhz < self.min_mhz or (busy is not None and float(busy) < self.min_busy):
            self.rejected += 1
            return False
        b = int(round(mv / self.bin_mv))
        with self._lock:
            st = self._bins.get(b)
            if st is None:
                self._bins[b] = [mhz, mhz, 1]
            else:
                st[0] = max(st[0], mhz)
                st[1] = mhz
                st[2] += 1
            self.samples += 1
        return True

    def points(self, min_samples: int = MIN_SAMPLES) -> list[dict]:
        with self._lock:
            items = sorted((b, list(st)) for b, st in self._bins.items() if st[2] >= min_samples)
        return [{"i": k, "mv": b * self.bin_mv, "mhz": round(st[0]), "last": round(st[1]), "n": st[2]}
                for k, (b, st) in enumerate(items)]

    def value(self) -> dict:
        return {"mode": "measured", "points": self.points(), "samples": self.samples, "bin_mv": self.bin_mv}

    def reset(self) -> None:
        with self._lock:
            self._bins.clear()
            self.samples = 0
            self.rejected = 0

    def load(self, pts: list[dict]) -> None:
        """Seed from saved points (e.g. a previous session or a mock)."""
        with self._lock:
            for p in pts:
                b = int(round(float(p["mv"]) / self.bin_mv))
                n = int(p.get("n", 1))
                self._bins[b] = [float(p["mhz"]), float(p.get("last", p["mhz"])), n]
                self.samples += n
