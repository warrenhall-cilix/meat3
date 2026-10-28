"""Per-core pipeline stall breakdown ("top-down") from the core PMU, AMD Zen.

Every dispatch slot (6 per cycle on Zen 4, 8 on Zen 5) ends up in one bucket:
  retiring        ex_ret_ops (PMCx0C1)                        useful work
  frontend bound  de_no_dispatch_per_slot.no_ops_from_frontend (PMCx1A0 umask 0x01)
  backend bound   de_no_dispatch_per_slot.backend_stalls       (PMCx1A0 umask 0x1E)  memory/execution
  SMT contention  de_no_dispatch_per_slot.smt_contention       (PMCx1A0 umask 0x60)  sibling thread had the slot
  bad speculation the remainder (ops dispatched then thrown away)
slots = width x ls_not_halted_cyc (PMCx076). IPC = ex_ret_instr (PMCx0C0) / cycles.
Encodings are from Linux tools/perf/pmu-events/arch/x86/amdzen4,5.  Zen 2/3 get IPC only.

Counting mode only (no sampling, no interrupts): the kernel reads 6 counters per thread at
~1 Hz, which costs well under 0.1 ms per second. 6 events fit the 6 core counters exactly; if
something else holds a counter (NMI watchdog, a running `perf`), the kernel time-shares them and
values are scaled by time_enabled / time_running.
"""
from __future__ import annotations

from typing import Optional

PERF_TYPE_RAW = 4
FMT_TOTAL_TIME_ENABLED, FMT_TOTAL_TIME_RUNNING = 1, 2
RETRY_S = 30.0
MIN_ACTIVE_HZ = 1e6        # a core busy for less than ~1M cycles/s reads as idle (no percentages)


def raw_config(event: int, umask: int = 0) -> int:
    """AMD core PMC: event[7:0] -> config[7:0], umask -> [15:8], event[11:8] -> [35:32]."""
    return (event & 0xFF) | ((umask & 0xFF) << 8) | (((event >> 8) & 0xF) << 32)


def dispatch_width(family: int, model: int) -> Optional[int]:
    """Dispatch slots per cycle, or None where the slot events don't exist (Zen 2/3)."""
    if family >= 0x1A:
        return 8
    if family == 0x19 and (0x10 <= model <= 0x1F or 0x60 <= model <= 0x7F or 0xA0 <= model <= 0xAF):
        return 6
    return None


def events_for(family: int, model: int) -> dict[str, int]:
    ev = {"cycles": raw_config(0x76), "instr": raw_config(0xC0)}
    if dispatch_width(family, model):
        ev.update(ops=raw_config(0xC1), fe=raw_config(0x1A0, 0x01), be=raw_config(0x1A0, 0x1E),
                  smt=raw_config(0x1A0, 0x60))
    return ev


def breakdown(d: dict[str, float], width: Optional[int]) -> dict[str, Optional[float]]:
    """Counter deltas (summed over a core's threads) -> IPC and % of dispatch slots."""
    out: dict[str, Optional[float]] = {k: None for k in ("ipc", "retiring", "frontend", "backend", "smt", "badspec")}
    cyc = d.get("cycles")
    if not cyc:
        return out
    if d.get("instr") is not None:
        out["ipc"] = d["instr"] / cyc
    if width and None not in (d.get("ops"), d.get("fe"), d.get("be"), d.get("smt")):
        slots = width * cyc
        pct = {k: 100.0 * d[src] / slots for k, src in
               (("retiring", "ops"), ("frontend", "fe"), ("backend", "be"), ("smt", "smt"))}
        pct = {k: max(0.0, min(100.0, v)) for k, v in pct.items()}
        pct["badspec"] = max(0.0, 100.0 - sum(pct.values()))
        out.update(pct)
    return out


class TopDown:
    """Opens the events on every thread; sample() returns {core index: breakdown + 'hz'}."""

    def __init__(self, perf, cores: list[list[int]], family: int, model: int, clock):
        self.perf = perf
        self.cores = cores
        self.clock = clock
        self.width = dispatch_width(family, model)
        self.events = events_for(family, model)
        self.fds: dict[tuple[int, str], Optional[int]] = {}
        self.prev: dict[tuple[int, str], int] = {}
        self.prev_t: Optional[float] = None
        self.error = ""
        self.multiplexed = False
        self._opened_at = -1e9
        self._open()

    def _open(self) -> None:
        self._opened_at = self.clock()
        if self.perf is None:
            self.error = "perf_event_open unavailable"
            return
        for cpus in self.cores:
            for cpu in cpus:
                for name, cfg in self.events.items():
                    if self.fds.get((cpu, name)) is not None:
                        continue
                    try:
                        self.fds[cpu, name] = self.perf.open(
                            PERF_TYPE_RAW, cfg, cpu, read_format=FMT_TOTAL_TIME_ENABLED | FMT_TOTAL_TIME_RUNNING)
                    except (OSError, TypeError) as e:
                        self.fds[cpu, name] = None
                        self.error = f"core PMU open failed: {getattr(e, 'strerror', None) or e}"

    def _read(self, fd: int) -> Optional[int]:
        rs = getattr(self.perf, "read_scaled", None)
        if rs is None:
            return self.perf.read(fd)
        r = rs(fd)
        if r is None:
            return None
        v, enabled, running = r
        if running and running < enabled:
            self.multiplexed = True
            return int(v * enabled / running)
        return v

    def sample(self) -> dict[int, dict]:
        if any(v is None for v in self.fds.values()) and self.clock() - self._opened_at > RETRY_S:
            self._open()
        now = self.clock()
        dt = None if self.prev_t is None else now - self.prev_t
        self.prev_t = now
        self.multiplexed = False
        out: dict[int, dict] = {}
        for k, cpus in enumerate(self.cores):
            sums: dict[str, Optional[float]] = {}
            for name in self.events:
                tot: Optional[float] = 0.0
                for cpu in cpus:
                    fd = self.fds.get((cpu, name))
                    v = None if fd is None else self._read(fd)
                    p = self.prev.get((cpu, name))
                    if v is None:
                        self.prev.pop((cpu, name), None)
                    else:
                        self.prev[cpu, name] = v
                    if v is None or p is None or v < p or tot is None:
                        tot = None
                    else:
                        tot += v - p
                sums[name] = tot
            r = breakdown(sums, self.width) if dt and dt > 0 else breakdown({}, self.width)
            hz = None if not dt or sums.get("cycles") is None else sums["cycles"] / dt
            if hz is not None and hz < MIN_ACTIVE_HZ * len(cpus):
                r = {key: None for key in r}         # idle core: ratios would be noise
            r["hz"] = hz
            r["raw"] = sums
            out[k] = r
        return out

    def close(self) -> None:
        if self.perf is None:
            return
        for fd in self.fds.values():
            if fd is not None:
                self.perf.close(fd)
        self.fds.clear()
