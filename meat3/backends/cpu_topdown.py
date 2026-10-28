"""Pipeline stall breakdown ("top-down") from the core PMU, AMD Zen: per core and per program.

Every dispatch slot (6 per cycle on Zen 4, 8 on Zen 5) ends up in one bucket:
  retiring        ex_ret_ops (PMCx0C1)                        useful work
  frontend bound  de_no_dispatch_per_slot.no_ops_from_frontend (PMCx1A0 umask 0x01)
  backend bound   de_no_dispatch_per_slot.backend_stalls       (PMCx1A0 umask 0x1E)  memory/execution
  SMT contention  de_no_dispatch_per_slot.smt_contention       (PMCx1A0 umask 0x60)  sibling thread had the slot
  bad speculation the remainder (ops dispatched then thrown away)
slots = width x ls_not_halted_cyc (PMCx076). IPC = ex_ret_instr (PMCx0C0) / cycles.
Encodings are from Linux tools/perf/pmu-events/arch/x86/amdzen4,5.  Zen 2/3 get IPC only.

Counting mode only (no sampling, no interrupts). Each set of events is opened as one perf group
(leader = cycles), so the kernel schedules the set as a unit and the ratios are exact even when
counters are shared (then extrapolated by time_enabled / time_running). If a group can never be
scheduled (something pinned a counter, e.g. the NMI watchdog), the events are reopened ungrouped.

Per-CPU and per-thread counters can't share the hardware: the kernel always schedules CPU-wide
events first, so thread events would never run. While programs are being counted, the backend
therefore alternates every sample: per-core sets enabled and thread sets disabled, then the other
way round (one ioctl per group). Each view keeps returning its last result while it is off.

Per program (AppPipeline): a group per hot thread (pid=tid, cpu=-1). Unlike the per-CPU counters
this is not free: the kernel saves/restores the thread's counters on every context switch of that
thread, measured on a 9800X3D (kernel 7.3) at ~0.5 us per switch for 1 event, ~1.1 us for 4 and
~1.7 us for 6. So programs get only 4 events (IPC, back-end, front-end), only threads using >=
min_thread_pct of a core are counted (at most max_threads per program), threads switching more
than max_switches/s are skipped, and cost_pct estimates the overhead from the counted threads'
switch rates. The hint reports how much of the program's CPU time the counted threads cover.
"""
from __future__ import annotations

import os
from typing import Optional

PERF_TYPE_RAW = 4
FMT_TOTAL_TIME_ENABLED, FMT_TOTAL_TIME_RUNNING = 1, 2
READ_FORMAT = FMT_TOTAL_TIME_ENABLED | FMT_TOTAL_TIME_RUNNING
PERF_EVENT_IOC_ENABLE, PERF_EVENT_IOC_DISABLE, PERF_IOC_FLAG_GROUP = 0x2400, 0x2401, 1
RETRY_S = 30.0
SWITCH_COST_S = 1.1e-6     # per context switch of a counted thread, 4 events (measured, see above)
MIN_ACTIVE_HZ = 1e6        # a core busy for less than ~1M cycles/s reads as idle (no percentages)
STARVED_NS = 2e9           # a group enabled this long without ever running can't fit -> ungroup


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
    ev = {"cycles": raw_config(0x76), "instr": raw_config(0xC0)}      # cycles first: group leader
    if dispatch_width(family, model):
        ev.update(ops=raw_config(0xC1), fe=raw_config(0x1A0, 0x01), be=raw_config(0x1A0, 0x1E),
                  smt=raw_config(0x1A0, 0x60))
    return ev


def app_events_for(family: int, model: int) -> dict[str, int]:
    """The smaller per-program set (each event adds context-switch cost): IPC, back-end, front-end."""
    ev = events_for(family, model)
    return {k: v for k, v in ev.items() if k in ("cycles", "instr", "be", "fe")}


def breakdown(d: dict[str, Optional[float]], width: Optional[int]) -> dict[str, Optional[float]]:
    """Counter deltas (summed over threads) -> IPC and % of dispatch slots (those the events allow)."""
    out: dict[str, Optional[float]] = {k: None for k in ("ipc", "retiring", "frontend", "backend", "smt", "badspec")}
    cyc = d.get("cycles")
    if not cyc:
        return out
    if d.get("instr") is not None:
        out["ipc"] = d["instr"] / cyc
    if not width:
        return out
    slots = width * cyc
    clamp = lambda v: max(0.0, min(100.0, v))   # noqa: E731
    for k, src in (("frontend", "fe"), ("backend", "be"), ("retiring", "ops"), ("smt", "smt")):
        if d.get(src) is not None:
            out[k] = clamp(100.0 * d[src] / slots)
    if None not in (out["frontend"], out["backend"], out["retiring"], out["smt"]):
        out["badspec"] = max(0.0, 100.0 - out["frontend"] - out["backend"] - out["retiring"] - out["smt"])
    return out


def _add(acc: dict[str, Optional[float]], d: Optional[dict[str, Optional[float]]]) -> None:
    """acc += d per event; None (unknown) is sticky."""
    for k in list(acc):
        v = None if d is None else d.get(k)
        acc[k] = None if v is None or acc[k] is None else acc[k] + v


def _add_known(acc: dict[str, float], d: dict[str, Optional[float]]) -> bool:
    """acc += d for a thread whose whole set was counted this interval; False if it wasn't."""
    if any(d.get(k) is None for k in acc):
        return False
    for k in acc:
        acc[k] += d[k]
    return True


class CounterSet:
    """One set of events on one target (a CPU, or a thread), as a perf group when possible.

    deltas() returns the extrapolated per-event increase since the previous call: delta value x
    delta time_enabled / delta time_running; events that weren't scheduled at all in the interval
    give None, a target that didn't run gives 0."""

    def __init__(self, perf, events: dict[str, int], cpu: int = -1, pid: int = -1, grouped: bool = True,
                 enabled: bool = True):
        self.perf, self.events, self.cpu, self.pid = perf, events, cpu, pid
        self.grouped = grouped
        self.enabled = enabled
        self.fds: dict[str, int] = {}
        self.prev: dict[str, tuple[int, int, int]] = {}
        self.multiplexed = False
        self._open()

    def _open(self) -> None:
        leader = -1
        try:
            for name, cfg in self.events.items():
                # disabled: the leader alone gates a group; ungrouped, every event
                off = not self.enabled and (leader == -1 or not self.grouped)
                fd = self.perf.open(PERF_TYPE_RAW, cfg, self.cpu, read_format=READ_FORMAT, pid=self.pid,
                                    group_fd=leader if self.grouped else -1, disabled=off)
                self.fds[name] = fd
                if self.grouped and leader == -1:
                    leader = fd
        except (OSError, TypeError):
            self.close()
            raise
        self.deltas()                               # baseline, so the first interval counts

    def set_enabled(self, on: bool) -> None:
        if on == self.enabled or not self.fds:
            return
        req = PERF_EVENT_IOC_ENABLE if on else PERF_EVENT_IOC_DISABLE
        fds = list(self.fds.values())
        for fd in (fds[:1] if self.grouped else fds):
            self.perf.ioctl(fd, req, PERF_IOC_FLAG_GROUP if self.grouped else 0)
        self.enabled = on

    def _ungroup(self) -> None:
        self.close()
        self.grouped = False
        self.prev.clear()
        self._open()

    def deltas(self) -> dict[str, Optional[float]]:
        out: dict[str, Optional[float]] = {}
        self.multiplexed = False
        starved = self.grouped
        for name, fd in self.fds.items():
            r = self.perf.read_scaled(fd)
            p = self.prev.get(name)
            if r is None:
                self.prev.pop(name, None)
                out[name] = None
                continue
            self.prev[name] = r
            if r[2] > 0 or r[1] < STARVED_NS:
                starved = False
            if p is None:
                out[name] = 0.0                     # first read: baseline only
                continue
            dv, den, drun = r[0] - p[0], r[1] - p[1], r[2] - p[2]
            if dv < 0 or den < 0 or drun < 0:
                out[name] = None
            elif den == 0:
                out[name] = 0.0
            elif drun == 0:
                out[name] = None
            else:
                if drun < den:
                    self.multiplexed = True
                out[name] = dv * den / drun
        if starved:
            self._ungroup()
            return {k: None for k in self.events}
        return out

    def close(self) -> None:
        for fd in self.fds.values():
            self.perf.close(fd)
        self.fds.clear()


class TopDown:
    """A CounterSet per CPU thread; sample() returns {core index: breakdown + 'hz' + 'raw'}."""

    def __init__(self, perf, cores: list[list[int]], family: int, model: int, clock):
        self.perf = perf
        self.cores = cores
        self.clock = clock
        self.width = dispatch_width(family, model)
        self.events = events_for(family, model)
        self.sets: dict[int, CounterSet] = {}
        self.prev_t: Optional[float] = None
        self.error = ""
        self.multiplexed = False
        self.enabled = True
        self.last: dict[int, dict] = {}
        self._opened_at = -1e9
        self._open()

    def set_enabled(self, on: bool) -> None:
        for s in self.sets.values():
            s.set_enabled(on)
        self.enabled = on

    @property
    def grouped(self) -> bool:
        return bool(self.sets) and all(s.grouped for s in self.sets.values())

    def _open(self) -> None:
        self._opened_at = self.clock()
        if self.perf is None:
            self.error = "perf_event_open unavailable"
            return
        for cpus in self.cores:
            for cpu in cpus:
                if cpu in self.sets:
                    continue
                try:
                    self.sets[cpu] = CounterSet(self.perf, self.events, cpu=cpu, enabled=self.enabled)
                except (OSError, TypeError) as e:
                    self.error = f"core PMU open failed: {getattr(e, 'strerror', None) or e}"

    def sample(self) -> dict[int, dict]:
        ncpu = sum(len(c) for c in self.cores)
        if self.perf is not None and len(self.sets) < ncpu and self.clock() - self._opened_at > RETRY_S:
            self._open()
        now = self.clock()
        dt = None if self.prev_t is None else now - self.prev_t
        self.prev_t = now
        if not self.enabled:                        # the per-program view had the counters
            return self.last
        self.multiplexed = False
        out: dict[int, dict] = {}
        for k, cpus in enumerate(self.cores):
            sums: dict[str, Optional[float]] = {name: 0.0 for name in self.events}
            for cpu in cpus:
                cs = self.sets.get(cpu)
                _add(sums, cs.deltas() if cs else None)
                self.multiplexed |= bool(cs and cs.multiplexed)
            r = breakdown(sums, self.width) if dt and dt > 0 else breakdown({}, self.width)
            hz = None if not dt or sums.get("cycles") is None else sums["cycles"] / dt
            if hz is not None and hz < MIN_ACTIVE_HZ * len(cpus):
                r = {key: None for key in r}         # idle core: ratios would be noise
            r["hz"] = hz
            r["raw"] = sums
            out[k] = r
        self.last = out
        return out

    def close(self) -> None:
        for s in self.sets.values():
            s.close()
        self.sets.clear()


def _switches(proc_root: str, pid: int, tid: int) -> Optional[int]:
    """Times the thread has been scheduled in (3rd field of /proc/<pid>/task/<tid>/schedstat)."""
    try:
        with open(os.path.join(proc_root, str(pid), "task", str(tid), "schedstat")) as f:
            return int(f.read().split()[2])
    except (OSError, ValueError, IndexError):
        return None


def _thread_ticks(proc_root: str, pid: int) -> dict[int, tuple[int, int]]:
    """{tid: (utime+stime ticks, starttime)} for every thread of a process."""
    out: dict[int, tuple[int, int]] = {}
    base = os.path.join(proc_root, str(pid), "task")
    try:
        tids = os.listdir(base)
    except OSError:
        return out
    for t in tids:
        try:
            with open(os.path.join(base, t, "stat")) as f:
                st = f.read()
            rest = st[st.rindex(")") + 2:].split()
            out[int(t)] = (int(rest[11]) + int(rest[12]), int(rest[19]))
        except (OSError, ValueError, IndexError):
            continue
    return out


class _Thread:
    __slots__ = ("app", "ticks", "cset", "hot_t", "sw", "sw_rate")

    def __init__(self, app: str, ticks: int):
        self.app, self.ticks = app, ticks
        self.cset: Optional[CounterSet] = None
        self.hot_t: Optional[float] = None
        self.sw: Optional[int] = None
        self.sw_rate: Optional[float] = None


class AppPipeline:
    """Per-program IPC / stall breakdown: counters on each program's hot threads.

    update([{key, pids}, ...]) once per sample -> {key: breakdown + coverage/threads}."""

    COLD_S = 10.0            # stop counting a thread after it has been cold this long

    def __init__(self, perf, family: int, model: int, clock, proc_root: str = "/proc",
                 max_threads: int = 16, min_thread_pct: float = 1.0, max_switches: float = 20000.0):
        self.perf = perf
        self.clock = clock
        self.proc = proc_root
        self.width = dispatch_width(family, model)
        self.events = app_events_for(family, model)
        self.max_threads = max_threads
        self.min_thread_pct = min_thread_pct
        self.max_switches = max_switches
        self.cost_pct: Optional[float] = None       # estimated overhead, % of one core
        self.hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        self.threads: dict[tuple[int, int], _Thread] = {}     # (tid, starttime) -> state
        self.prev_t: Optional[float] = None
        self.error = ""
        self.enabled = True
        self.last: dict[str, dict] = {}

    @property
    def counting(self) -> bool:
        return any(t.cset is not None for t in self.threads.values())

    def set_enabled(self, on: bool) -> None:
        for t in self.threads.values():
            if t.cset is not None:
                t.cset.set_enabled(on)
        self.enabled = on

    def update(self, apps: list[dict]) -> dict[str, dict]:
        now = self.clock()
        dt = None if self.prev_t is None else now - self.prev_t
        self.prev_t = now
        seen: set[tuple[int, int]] = set()
        per_app: dict[str, dict] = {}
        for a in apps:
            key = a["key"]
            acc = {"sums": {n: 0.0 for n in self.events}, "ticks_all": 0, "ticks_counted": 0,
                   "counted": 0, "threads": 0, "multiplexed": False, "skipped_busy": 0, "sw": 0.0}
            per_app[key] = acc
            hot: list[tuple[int, tuple[int, int]]] = []
            for pid in a.get("pids") or []:
                for tid, (ticks, start) in _thread_ticks(self.proc, pid).items():
                    k = (tid, start)
                    seen.add(k)
                    th = self.threads.get(k)
                    if th is None:
                        th = self.threads[k] = _Thread(key, ticks)
                        d = 0
                    else:
                        d = max(0, ticks - th.ticks)
                        th.ticks = ticks
                    th.app = key
                    acc["threads"] += 1
                    acc["ticks_all"] += d
                    # counters opened last time: account this interval before deciding to close
                    if self.enabled and th.cset is not None and _add_known(acc["sums"], th.cset.deltas()):
                        acc["multiplexed"] |= th.cset.multiplexed
                        acc["counted"] += 1
                        acc["ticks_counted"] += d
                    pct = 100.0 * d / self.hz / dt if dt else 0.0
                    if pct >= self.min_thread_pct or th.cset is not None:
                        sw = _switches(self.proc, pid, tid)
                        th.sw_rate = None if sw is None or th.sw is None or not dt else max(0, sw - th.sw) / dt
                        th.sw = sw
                    else:
                        th.sw = th.sw_rate = None
                    if th.cset is not None and th.sw_rate is not None:
                        acc["sw"] += th.sw_rate
                    if pct >= self.min_thread_pct:
                        th.hot_t = now
                        if th.sw_rate is not None and th.sw_rate > self.max_switches:
                            acc["skipped_busy"] += 1          # would cost too much to count
                        elif th.sw_rate is not None or th.sw is None or th.cset is not None:
                            hot.append((d, k))                # (new hot thread: wait for its switch rate)
            # keep the hottest max_threads counted; open new ones, close long-cold ones
            hot.sort(reverse=True)
            want = {k for _, k in hot[:self.max_threads]}
            for k, th in self.threads.items():
                if th.app != key or k not in seen:
                    continue
                if k in want and th.cset is None and self.perf is not None:
                    try:
                        th.cset = CounterSet(self.perf, self.events, pid=k[0], enabled=self.enabled)
                    except (OSError, TypeError) as e:
                        self.error = f"thread counters: {getattr(e, 'strerror', None) or e}"
                elif th.cset is not None and k not in want and (th.hot_t is None or now - th.hot_t > self.COLD_S):
                    th.cset.close()
                    th.cset = None
        for k in [k for k in self.threads if k not in seen]:
            th = self.threads.pop(k)
            if th.cset is not None:
                th.cset.close()
        # counters are switched with every context switch while this view has them (half the time)
        self.cost_pct = 100.0 * SWITCH_COST_S * 0.5 * sum(a["sw"] for a in per_app.values()) if per_app else None
        out: dict[str, dict] = {}
        for key, acc in per_app.items():
            if not self.enabled:                    # the per-core view had the counters: last result
                prev = self.last.get(key) or {**breakdown({}, self.width), "coverage": None, "counted": 0,
                                              "multiplexed": False}
                out[key] = {**prev, "threads": acc["threads"], "skipped_busy": acc["skipped_busy"]}
                continue
            r = breakdown(acc["sums"], self.width) if acc["counted"] else breakdown({}, self.width)
            r["coverage"] = (100.0 * acc["ticks_counted"] / acc["ticks_all"]) if acc["ticks_all"] else None
            r["counted"] = acc["counted"]
            r["threads"] = acc["threads"]
            r["multiplexed"] = acc["multiplexed"]
            r["skipped_busy"] = acc["skipped_busy"]
            out[key] = r
        if self.enabled:
            self.last = out
        return out

    def close(self) -> None:
        for th in self.threads.values():
            if th.cset is not None:
                th.cset.close()
        self.threads.clear()
