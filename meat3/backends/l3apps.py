"""L3 occupancy per application / game via resctrl monitoring groups.

How it works (needs root and a mounted resctrl with L3 occupancy monitoring, cqm_occup_llc):
  * Every poll, scan /proc and add up CPU time per *app*. An app is a Steam game (all processes
    sharing a SteamAppId/SteamGameId environment value, i.e. the game plus its Proton/Wine
    helpers) or else an executable name.
  * Track the games plus the top-N apps by CPU. Each tracked app gets a monitoring group
    /sys/fs/resctrl/mon_groups/meat3_<slug>; its threads are written into the group's `tasks`
    file. Threads created later inherit their parent's group (fork/clone), and new processes of
    the app are picked up on the next scan.
  * Occupancy is read per L3 domain (CCD) from the group's mon_data/mon_L3_NN/llc_occupancy.
    The root group's value is the whole L3, so "other" = root - sum(apps).

With an L3Reserve (l3reserve.py) the game's threads are moved into the meat3_game control group
first, and the game's monitoring group lives under it (meat3_game/mon_groups/...), because a
monitoring group can only hold tasks of its parent control group.

Caveats (also shown in sensor hints): cache lines are attributed to the group that was active
when they were *filled*, so a freshly tracked app ramps up over a second or two; and groups of
apps that stop being tracked are removed (their threads go back to the default group).
"""
from __future__ import annotations

import fnmatch
import os
import re
from typing import Optional

GROUP_PREFIX = "meat3_"
DROP_AFTER_S = 60.0          # keep a group this long after an app stops qualifying
MIN_CPU_PCT = 1.0            # an app must average this much CPU to enter the top-N
EMA_ALPHA = 0.3
RESERVE_MIN_CPU = 2.0        # a game must use this much CPU (% of all threads) to trigger the L3 reservation
RESERVE_HOLD_S = 60.0        # ... and keeps it while it was that busy within this window
_rmdir = os.rmdir            # tests swap this (resctrl removes non-empty group dirs, a real fs does not)
_STEAM_KEYS = (b"SteamAppId=", b"SteamGameId=")
_WINE_EXES = ("wine", "wine64", "wine-preloader", "wine64-preloader", "wineserver")
_IGNORE_STEAM_APP = {"0", "769"}   # 769 = Steam's own helper runtime id


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return (s or "app")[:40]


def _read(path: str, mode: str = "r"):
    try:
        with open(path, mode) as f:
            return f.read()
    except OSError:
        return None


class ProcInfo:
    __slots__ = ("key", "name", "game", "ticks")

    def __init__(self, key: str, name: str, game: bool):
        self.key, self.name, self.game = key, name, game
        self.ticks: Optional[int] = None


class App:
    def __init__(self, key: str, name: str, game: bool):
        self.key, self.name, self.game = key, name, game
        self.cpu_ema = 0.0
        self.last_qualified: Optional[float] = None
        self.pids: list[int] = []
        self.group: Optional[str] = None          # dir name under <parent>/mon_groups
        self.parent: Optional[str] = None         # control group dir holding the group (root or meat3_game)
        self.assigned: set[int] = set()           # tids already written to the group


class L3Apps:
    def __init__(self, resctrl_base: str, proc_root: str = "/proc", top: int = 8,
                 game_patterns: Optional[list[str]] = None, max_groups: int = 16, reserve=None):
        self.base = resctrl_base
        self.reserve = reserve                    # l3reserve.L3Reserve or None
        self.game_busy_t: Optional[float] = None
        self.mon_groups = os.path.join(resctrl_base, "mon_groups")
        self.proc = proc_root
        self.top = top
        self.game_patterns = [p.lower() for p in (game_patterns if game_patterns is not None else ["*.exe"])]
        self.max_groups = max_groups
        self.hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        self.ncpu = os.cpu_count() or 1
        self.procs: dict[tuple[int, int], ProcInfo] = {}    # (pid, starttime) -> info
        self.apps: dict[str, App] = {}
        self.prev_t: Optional[float] = None
        self.self_pid = os.getpid()
        self.error: Optional[str] = None
        self._cleanup_stale()

    # ------------------------------------------------------------------ resctrl groups
    def _cleanup_stale(self) -> None:
        """Remove groups left behind by a previous daemon run."""
        try:
            for d in os.listdir(self.mon_groups):
                if d.startswith(GROUP_PREFIX):
                    try:
                        _rmdir(os.path.join(self.mon_groups, d))
                    except OSError:
                        pass
        except OSError as e:
            self.error = f"resctrl mon_groups unavailable: {e.strerror}"

    def _ensure_group(self, app: App) -> bool:
        if app.group:
            return True
        if sum(1 for a in self.apps.values() if a.group) >= self.max_groups:
            return False
        name = GROUP_PREFIX + slug(app.key)
        parent = self._ctrl_dir(app)
        try:
            os.mkdir(os.path.join(parent, "mon_groups", name))
        except FileExistsError:
            pass
        except OSError as e:
            # ENOSPC = out of RMIDs (or RMIDs still in limbo after removals)
            self.error = f"cannot create resctrl group for {app.name}: {e.strerror}"
            return False
        app.group, app.parent, app.assigned = name, parent, set()
        return True

    def _ctrl_dir(self, app: App) -> str:
        r = self.reserve
        return r.game_dir if (app.game and r is not None and r.active) else self.base

    def _drop_group(self, app: App) -> None:
        if app.group:
            try:
                _rmdir(os.path.join(app.parent or self.base, "mon_groups", app.group))
            except OSError:
                pass
        app.group, app.parent, app.assigned = None, None, set()

    def _assign(self, app: App) -> None:
        """Write the app's new threads into its monitoring group (and, for a game while the L3
        reservation is active, into the meat3_game control group first). Without a monitoring
        group (out of RMIDs) a reserved game still gets the control group."""
        ctrl = self._ctrl_dir(app)
        files = []
        if ctrl != self.base:
            files.append(os.path.join(ctrl, "tasks"))
        if app.group:
            files.append(os.path.join(app.parent, "mon_groups", app.group, "tasks"))
        if not files:
            return
        live = set()
        for pid in app.pids:
            try:
                tids = [int(t) for t in os.listdir(os.path.join(self.proc, str(pid), "task"))]
            except (OSError, ValueError):
                continue
            live.update(tids)
            todo = [t for t in tids if t not in app.assigned]
            if not todo:
                continue
            ok = set(todo)
            for path in files:
                try:
                    fd = os.open(path, os.O_WRONLY)
                except OSError:
                    return
                try:
                    for t in todo:
                        if t not in ok:
                            continue
                        try:
                            os.write(fd, f"{t}\n".encode())
                        except OSError:
                            ok.discard(t)  # thread exited (ESRCH) or is in another CTRL group (EINVAL)
                finally:
                    os.close(fd)
            app.assigned |= ok
        app.assigned &= live

    def _occupancy(self, group_dir: str) -> dict[int, Optional[int]]:
        out: dict[int, Optional[int]] = {}
        md = os.path.join(group_dir, "mon_data")
        try:
            doms = sorted(os.listdir(md))
        except OSError:
            return out
        for d in doms:
            m = re.match(r"mon_L3_(\d+)$", d)
            if not m:
                continue
            raw = _read(os.path.join(md, d, "llc_occupancy"))
            try:
                out[int(m.group(1))] = int(raw.split()[0]) if raw else None
            except ValueError:
                out[int(m.group(1))] = None       # "Unavailable" / "Error"
        return out

    # ------------------------------------------------------------------ process scan
    def _classify(self, pid: int) -> Optional[ProcInfo]:
        p = os.path.join(self.proc, str(pid))
        try:
            exe = os.path.basename(os.readlink(os.path.join(p, "exe")))
        except OSError:
            return None                           # kernel thread or already gone
        exe = exe.replace(" (deleted)", "")
        env = _read(os.path.join(p, "environ"), "rb") or b""
        steam = None
        for kv in env.split(b"\0"):
            if kv.startswith(_STEAM_KEYS):
                steam = kv.split(b"=", 1)[1].decode(errors="replace")
                break
        name = exe
        cmd = _read(os.path.join(p, "cmdline"), "rb") or b""
        argv0 = cmd.split(b"\0", 1)[0].decode(errors="replace")
        base0 = re.split(r"[\\/]", argv0)[-1] if argv0 else ""
        if exe in _WINE_EXES or base0.lower().endswith(".exe"):
            name = base0 or exe                   # Wine/Proton: the Windows .exe name
        is_game_name = any(fnmatch.fnmatch(name.lower(), pat) for pat in self.game_patterns)
        if steam and steam not in _IGNORE_STEAM_APP:
            return ProcInfo(f"steam:{steam}", name, True)
        return ProcInfo(name, name, is_game_name)

    def scan(self, now: float) -> None:
        dt = None if self.prev_t is None else now - self.prev_t
        self.prev_t = now
        seen: set[tuple[int, int]] = set()
        cpu: dict[str, float] = {}
        pids: dict[str, list[int]] = {}
        names: dict[str, tuple[float, str, bool]] = {}
        by_pid: dict[int, ProcInfo] = {}
        try:
            entries = os.listdir(self.proc)
        except OSError:
            return
        for e in sorted(entries, key=lambda x: int(x) if x.isdigit() else -1):   # parents first (usually)
            if not e.isdigit():
                continue
            pid = int(e)
            st = _read(os.path.join(self.proc, e, "stat"))
            if not st:
                continue
            try:
                rest = st[st.rindex(")") + 2:].split()
                ticks = int(rest[11]) + int(rest[12])        # utime + stime
                start = int(rest[19])
                ppid = int(rest[1])
            except (ValueError, IndexError):
                continue
            if pid == 2 or ppid == 2 or pid == self.self_pid:
                continue                                     # kernel threads, meat3d itself
            k = (pid, start)
            info = self.procs.get(k)
            if info is None:
                info = self._classify(pid)
                if info is None:
                    continue
                parent = by_pid.get(ppid)
                if parent is not None and parent.key.startswith("steam:") and not info.key.startswith("steam:"):
                    # children of a game belong to the game even if their environment was
                    # rewritten (setproctitle-style argv/env overwrites)
                    info = ProcInfo(parent.key, info.name, True)
                self.procs[k] = info
            by_pid[pid] = info
            seen.add(k)
            d = 0 if info.ticks is None else max(0, ticks - info.ticks)
            info.ticks = ticks
            pct = (100.0 * d / self.hz / dt / self.ncpu) if dt else 0.0
            cpu[info.key] = cpu.get(info.key, 0.0) + pct
            pids.setdefault(info.key, []).append(pid)
            # display name: the busiest process's name (for Steam: the actual game .exe)
            best = names.get(info.key)
            if best is None or pct > best[0] or (info.name.lower().endswith(".exe") and not best[1].lower().endswith(".exe")):
                names[info.key] = (pct, info.name, info.game)
        for k in [k for k in self.procs if k not in seen]:
            del self.procs[k]

        for key, total in cpu.items():
            app = self.apps.get(key)
            nm = names[key]
            if app is None:
                app = self.apps[key] = App(key, nm[1], nm[2])
            app.name = nm[1] if nm[1] else app.name
            app.cpu_ema = EMA_ALPHA * total + (1 - EMA_ALPHA) * app.cpu_ema
            app.pids = pids[key]
        for key in [k for k in self.apps if k not in cpu]:
            app = self.apps[key]
            app.pids, app.cpu_ema = [], app.cpu_ema * (1 - EMA_ALPHA)

        ranked = sorted((a for a in self.apps.values() if a.pids), key=lambda a: -a.cpu_ema)
        want = {a.key for a in ranked if a.game}
        want |= {a.key for a in ranked if a.cpu_ema >= MIN_CPU_PCT}.intersection(
            {a.key for a in ranked[:self.top]})
        for a in self.apps.values():
            if a.key in want:
                a.last_qualified = now
        self._update_reserve(now, ranked)
        for key in list(self.apps):
            a = self.apps[key]
            keep = a.pids and a.last_qualified is not None and (
                a.key in want or now - a.last_qualified < DROP_AFTER_S)
            if keep:
                self._ensure_group(a)
                self._assign(a)
            else:
                self._drop_group(a)
                if not a.pids:
                    del self.apps[key]

    def _update_reserve(self, now: float, ranked: list[App]) -> None:
        r = self.reserve
        if r is None:
            return
        games = [a for a in ranked if a.game]
        if any(a.cpu_ema >= RESERVE_MIN_CPU for a in games):
            self.game_busy_t = now
        want = bool(games) and self.game_busy_t is not None and now - self.game_busy_t < RESERVE_HOLD_S
        r.update(want, [a.name for a in games if a.cpu_ema >= RESERVE_MIN_CPU] or [a.name for a in games[:1]])
        # reservation switched on/off (here or via apply()): game monitoring groups must move to the
        # new parent (on deactivation the kernel already removed them together with meat3_game)
        for a in games:
            if a.group and a.parent != self._ctrl_dir(a):
                self._drop_group(a)

    # ------------------------------------------------------------------ results
    def sample(self, now: float) -> dict:
        """{'apps': [{key,name,game,cpu,procs,threads,occ:{dom:bytes}}], 'total': {dom: bytes}}"""
        self.scan(now)
        apps = []
        for a in self.apps.values():
            if not a.group:
                continue
            apps.append({"key": a.key, "name": a.name, "game": a.game, "cpu": a.cpu_ema,
                         "procs": len(a.pids), "threads": len(a.assigned),
                         "occ": self._occupancy(os.path.join(a.parent, "mon_groups", a.group))})
        apps.sort(key=lambda r: -sum(v or 0 for v in r["occ"].values()))
        total: dict[int, Optional[int]] = {}
        for d in ctrl_groups(self.base):
            for dom, v in self._occupancy(d).items():
                total[dom] = None if v is None or total.get(dom, 0) is None else total.get(dom, 0) + v
        return {"apps": apps, "total": total}

    def close(self) -> None:
        for a in self.apps.values():
            self._drop_group(a)
        self.apps.clear()
        if self.reserve is not None:
            self.reserve.close()


def ctrl_groups(base: str) -> list[str]:
    """The root group plus every control group under it. Each control group's mon_data counts only
    its own tasks, so whole-L3 occupancy is the sum over all of them."""
    out = [base]
    try:
        for d in sorted(os.listdir(base)):
            p = os.path.join(base, d)
            if d not in ("info", "mon_groups", "mon_data") and os.path.isfile(os.path.join(p, "schemata")):
                out.append(p)
    except OSError:
        pass
    return out
