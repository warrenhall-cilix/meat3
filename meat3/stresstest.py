"""All-core CPU stress test: the engine behind the Overclock tab's "Stress test" button and `meat3-stress`.

Presets (what the load is, what counts as a failure):

  quick     stress-ng matrix multiply on every thread, results verified. Clocks and cooling; 3 min default.
  mixed     stress-ng cycling through every CPU method, verified. Broader than `quick`; 10 min.
  avx512    y-cruncher (Zen 5 build when installed), its component stress tests with StopOnError. The
            heaviest current draw and the most vdroop; what finds a marginal manual OC. 5 min.
  p95       Prime95 torture, small FFTs, AVX2+FMA3, one worker per thread. 15 min.
  p95-512   Same with AVX-512.

A run FAILS on a wrong result, a tool error, a non-zero exit, or a machine check / segfault / lockup
in the kernel log while it ran. A crash of the box is the fourth outcome: the daemon's manual OC does
not re-apply at boot, so the machine comes back at stock.

Sampling: `KEY_SENSORS` names the few sensors worth watching (clock, Tctl, package power, current,
rail voltage) by id pattern so the same picker works on the real daemon and on the mock. The GUI
samples from its snapshot store; the CLI polls the daemon socket.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Callable, Optional

from meat3.cpuvalidate import P95_FAIL, P95_PASS, YC_BIN, p95_config, parse_ycruncher, yc_config

KMSG_BAD = re.compile(r"mce:|machine check|hardware error|segfault|general protection|"
                      r"\bMCE\b|watchdog: BUG: soft lockup|rcu_preempt detected stalls", re.I)

# (short label, id regex, unit): the first sensor id matching each regex is shown; real ids first, mock ids after
KEY_SENSORS: list[tuple[str, str]] = [
    ("clock", r"\.ccd0\.clk_eff$|\.clk_eff_avg$"),
    ("Tctl", r"\.k10temp\.temp1$|\.temp\.tctl$"),
    ("power", r"\.pmt\.ppt$|\.pwr\.package$"),
    ("current", r"\.pmt\.tdc$|\.pwr\.tdc$"),
    ("vcore", r"\.pmt\.vddcr_cpu$|\.volt\.vcore$"),
]

PRESETS: dict[str, dict] = {
    "quick":   {"label": "Quick (stress-ng matrix, verified)", "seconds": 180, "tool": "stress-ng",
                "hint": "All threads, matrix multiply with result verification. Clocks and cooling."},
    "mixed":   {"label": "Mixed (stress-ng all methods, verified)", "seconds": 600, "tool": "stress-ng",
                "hint": "All threads, every stress-ng CPU method in turn, verified. Broader coverage."},
    "avx512":  {"label": "AVX-512 (y-cruncher)", "seconds": 300, "tool": "y-cruncher",
                "hint": "Heaviest current and vdroop. Finds a marginal manual OC; a fail here can be a hard crash."},
    "p95":     {"label": "Prime95 small FFT, AVX2", "seconds": 900, "tool": "mprime",
                "hint": "One worker per thread, 4K-64K FFTs in cache. Sustained heat; results checked."},
    "p95-512": {"label": "Prime95 small FFT, AVX-512", "seconds": 900, "tool": "mprime",
                "hint": "As above with AVX-512 enabled."},
}


def pick_sensors(ids) -> dict[str, str]:
    """{short label: sensor id} for the KEY_SENSORS found among `ids` (cpu* ids only)."""
    out = {}
    cpu_ids = [i for i in ids if i.startswith("cpu")]
    for label, pat in KEY_SENSORS:
        rx = re.compile(pat)
        for sid in cpu_ids:
            if rx.search(sid):
                out[label] = sid
                break
    return out


def tool_path(tool: str) -> Optional[str]:
    if tool == "y-cruncher":
        return YC_BIN if os.path.isfile(YC_BIN) else shutil.which("y-cruncher")
    return shutil.which(tool)


def install_hint(tool: str) -> str:
    return {"stress-ng": "stress-ng (pacman -S stress-ng)", "y-cruncher": "y-cruncher (paru -S y-cruncher)",
            "mprime": "mprime (paru -S mprime-bin)"}.get(tool, tool)


def cpu_list() -> list[int]:
    try:
        return sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return list(range(os.cpu_count() or 1))


def build_command(preset: str, seconds: int, workdir: str, cpus: Optional[list[int]] = None) -> list[str]:
    """The command for a preset, writing any config file it needs into `workdir`."""
    cpus = cpus or cpu_list()
    n = max(1, len(cpus))
    p = PRESETS[preset]
    if p["tool"] == "stress-ng":
        method = "matrixprod" if preset == "quick" else "all"
        return ["stress-ng", "--cpu", str(n), "--cpu-method", method, "--verify",
                "--timeout", f"{int(seconds)}s", "--metrics-brief"]
    if p["tool"] == "y-cruncher":
        per = max(10, int(seconds) // 8)
        mem = (256 << 20) * n
        cfg = os.path.join(workdir, "stress.cfg")
        with open(cfg, "w") as f:
            f.write(yc_config(cpus, per, mem))
        return [tool_path("y-cruncher") or "y-cruncher", "skip-warnings", "config", cfg]
    # mprime: one worker per thread; stopped with SIGINT when the time is up
    with open(os.path.join(workdir, "prime.txt"), "w") as f:
        f.write(p95_config("p95-avx512" if preset == "p95-512" else "p95-avx2", threads=n))
    return ["mprime", "-t", f"-W{workdir}"]


def runs_until_stopped(preset: str) -> bool:
    """y-cruncher and mprime loop forever: the run ends with SIGINT when the time is up."""
    return PRESETS[preset]["tool"] != "stress-ng"


def classify(preset: str, output: str, returncode: Optional[int], stopped_by_us: bool,
             workdir: str = "") -> tuple[bool, str]:
    """(ok, why) from the tool's output and how it ended. A tool that was still running when its time
    was up and we stopped it is the pass path for y-cruncher and mprime; stress-ng ends by itself."""
    tool = PRESETS[preset]["tool"]
    if tool == "y-cruncher":
        return parse_ycruncher(output, ran_full=stopped_by_us)
    if tool == "mprime":
        log = output
        for name in ("results.txt", "results.bench.txt"):
            try:
                with open(os.path.join(workdir, name)) as f:
                    log += "\n" + f.read()
            except OSError:
                pass
        bad = [ln.strip() for ln in log.splitlines() if P95_FAIL.search(ln)]
        if bad:
            return False, bad[0][:160]
        if not stopped_by_us:
            return False, f"mprime exited early ({returncode}): " + log.strip()[-160:]
        return True, f"{len(P95_PASS.findall(log))} FFT sizes passed"
    # stress-ng: "fail:" lines and "failed: N" with N > 0 are verification errors
    fails = [ln.strip() for ln in output.splitlines() if re.search(r"\bfail:|failed: *[1-9]", ln)]
    if stopped_by_us:
        return False, "stopped"
    if returncode != 0:
        return False, f"stress-ng exit {returncode}: " + ("; ".join(fails[:2]) or output.strip()[-160:])
    if fails:
        return False, "; ".join(fails[:2])
    m = re.search(r"failed: *0", output)
    return True, "0 failures" if m else "completed"


def kernel_bad_lines(since: float) -> Optional[list[str]]:
    """Machine-check / segfault / lockup lines logged since `since` (epoch). None when the log can't be read
    (journalctl needs group systemd-journal, adm or wheel; dmesg may be restricted)."""
    for cmd in (["journalctl", "-k", "-b", "-o", "cat", "--no-pager", f"--since=@{int(since)}"],
                ["dmesg", "--time-format", "iso", "-l", "emerg,alert,crit,err,warn"]):
        if not shutil.which(cmd[0]):
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode != 0:
            continue
        return [ln.strip()[:200] for ln in r.stdout.splitlines() if KMSG_BAD.search(ln)]
    return None


class StressRun:
    """One stress run. `start()` launches the tool with its output going to a file in a temp dir;
    call `poll()` regularly: it enforces the time limit (SIGINT for the looping tools), and once the
    process has ended it reads the output, checks the kernel log and fills `ok`/`why`/`kernel`.
    `stop()` ends it early (result 'stopped'). Samples are the caller's business (`samples`)."""

    def __init__(self, preset: str, seconds: int, command: Optional[list[str]] = None,
                 kernel_check: Callable[[float], Optional[list[str]]] = kernel_bad_lines):
        if preset not in PRESETS:
            raise ValueError(f"unknown preset {preset!r}")
        self.preset = preset
        self.seconds = int(seconds)
        self.command_override = command
        self.kernel_check = kernel_check
        self.workdir = ""
        self.proc: Optional[subprocess.Popen] = None
        self.started = 0.0
        self.ended = 0.0
        self.stopped_by_us = False
        self.user_stop = False
        self.interrupted_at = 0.0
        self.output = ""
        self.ok: Optional[bool] = None
        self.why = ""
        self.kernel: Optional[list[str]] = None
        self.samples: list[dict] = []
        self.peaks: dict[str, float] = {}

    @property
    def running(self) -> bool:
        return self.proc is not None and self.ended == 0.0

    @property
    def elapsed(self) -> float:
        end = self.ended or time.time()
        return max(0.0, end - self.started) if self.started else 0.0

    def start(self) -> None:
        self.workdir = tempfile.mkdtemp(prefix="meat3-stress-")
        cmd = self.command_override or build_command(self.preset, self.seconds, self.workdir)
        if not self.command_override and not tool_path(PRESETS[self.preset]["tool"]):
            self.ok, self.why = False, f"missing tool: {install_hint(PRESETS[self.preset]['tool'])}"
            self.started = self.ended = time.time()
            return
        self.started = time.time()
        self._out = open(os.path.join(self.workdir, "output.log"), "w+b")
        try:
            self.proc = subprocess.Popen(cmd, cwd=self.workdir, stdin=subprocess.DEVNULL, stdout=self._out,
                                         stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            self.ok, self.why = False, f"cannot start {cmd[0]}: {e}"
            self.ended = time.time()
            self._finish()

    def sample(self, values: dict[str, Optional[float]]) -> None:
        """Record {label: value} read by the caller; keeps peaks."""
        row = {"t": round(self.elapsed, 1)}
        for k, v in values.items():
            row[k] = v
            if isinstance(v, (int, float)):
                self.peaks[k] = max(self.peaks.get(k, v), v)
        self.samples.append(row)

    def poll(self) -> bool:
        """Advance; returns True once the run is finished (result available)."""
        if self.ended:
            return True
        if self.proc is None:
            return True
        rc = self.proc.poll()
        now = time.time()
        if rc is None:
            if not self.stopped_by_us and now - self.started >= self.seconds and runs_until_stopped(self.preset):
                self._interrupt()
            elif self.stopped_by_us and now - self.interrupted_at > 30:
                self._kill()
            elif not self.stopped_by_us and now - self.started > self.seconds + 90:
                self._interrupt()                      # stress-ng overran its own timeout
                self.why = "hung (killed after the time limit)"
            return False
        self.ended = now
        self._finish(rc)
        return True

    def _interrupt(self) -> None:
        self.stopped_by_us = True
        self.interrupted_at = time.time()
        try:
            os.killpg(self.proc.pid, signal.SIGINT)
        except OSError:
            pass

    def _kill(self) -> None:
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except OSError:
            pass

    def stop(self) -> None:
        """User stop: the result is 'stopped', not a pass."""
        if self.running and not self.stopped_by_us:
            self.user_stop = True
            self._interrupt()

    def _finish(self, rc: Optional[int] = None) -> None:
        try:
            self._out.flush()
            self._out.seek(0)
            self.output = self._out.read().decode(errors="replace")
            self._out.close()
        except (OSError, ValueError, AttributeError):
            pass
        if self.ok is None:
            if self.user_stop:
                self.ok, self.why = False, "stopped"
            elif self.why:
                self.ok = False
            else:
                self.ok, self.why = classify(self.preset, self.output, rc, self.stopped_by_us, self.workdir)
        try:
            self.kernel = self.kernel_check(self.started)
        except Exception:                           # the check is best-effort; never hide the tool's result
            self.kernel = None
        if self.kernel:
            self.ok = False
            self.why = "kernel: " + self.kernel[0] + (f" (and {len(self.kernel) - 1} more)" if len(self.kernel) > 1 else "")
        shutil.rmtree(self.workdir, ignore_errors=True)

    def summary(self) -> str:
        """One line for the Results log: result, duration, peaks."""
        state = "stopped" if self.user_stop else ("PASS" if self.ok else "FAIL")
        peaks = "  ".join(f"{k} {v:.0f}" if k in ("clock", "power", "current") else
                          f"{k} {v:.3f}" if k == "vcore" else f"{k} {v:.1f}"
                          for k, v in self.peaks.items())
        return (f"stress {self.preset} {state} after {self.elapsed:.0f}s"
                + (f": {self.why}" if self.why else "") + (f"  [peaks {peaks}]" if peaks else "")
                + ("" if self.kernel is not None else "  (kernel log not readable)"))


# ----------------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="meat3-stress", description="All-core CPU stress test with meat3 telemetry.")
    ap.add_argument("--preset", choices=list(PRESETS), default="quick")
    ap.add_argument("--seconds", type=int, default=0, help="run time (default: the preset's)")
    ap.add_argument("--every", type=float, default=10.0, help="sample interval in seconds")
    ap.add_argument("--socket", default=os.environ.get("MEAT3_SOCKET") or "/run/meat3/meat3.sock")
    ap.add_argument("--list", action="store_true", help="list presets and exit")
    a = ap.parse_args(argv)
    if a.list:
        for k, p in PRESETS.items():
            print(f"{k:8s} {p['seconds']:5d}s  {p['label']}  -  {p['hint']}")
        return 0
    seconds = a.seconds or PRESETS[a.preset]["seconds"]
    from meat3.cotune import Daemon
    d = Daemon(a.socket)
    try:
        ids = pick_sensors(d.sensors().keys())
    except Exception as e:
        print(f"daemon not reachable ({e}); running without telemetry", file=sys.stderr)
        d, ids = None, {}
    run = StressRun(a.preset, seconds)
    run.start()
    print(f"{a.preset}: {PRESETS[a.preset]['label']} for {seconds}s; watching {', '.join(ids.values()) or 'nothing'}")
    stop = False

    def on_sig(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)
    next_sample = time.time() + a.every
    while not run.poll():
        if stop:
            run.stop()
        time.sleep(0.5)
        if d is not None and time.time() >= next_sample:
            next_sample = time.time() + a.every
            try:
                s = d.sensors()
                vals = {k: s.get(sid, {}).get("value") for k, sid in ids.items()}
            except Exception:
                vals = {k: None for k in ids}
            run.sample(vals)
            print(f"{run.elapsed:5.0f}s  " + "  ".join(
                f"{k}={v:.3f}" if k == "vcore" and isinstance(v, float) else
                f"{k}={v:.1f}" if isinstance(v, (int, float)) else f"{k}=—" for k, v in vals.items()), flush=True)
    print(run.summary())
    return 0 if run.ok else 1


if __name__ == "__main__":
    sys.exit(main())
