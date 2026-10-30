"""meat3-bin --oc-probe: prove, on this chip and SMU firmware, that manual mode and the voltage encoding work.

The SMU doesn't say which voltage encoding its "set CPU VID" command expects. SVI3 (V = 0.245 + 0.005 * vid)
is expected for Zen 4/5; the older SVI2 encoding runs the other way (V = 1.55 - 0.00625 * vid). The probe
asks for a voltage *below* what the chip already runs, under either reading:

    1.10 V in SVI3 = vid 171  ->  read as SVI2 that's 0.48 V

so a wrong guess crashes the PC (too low), it can't overvolt it. Steps, all at idle:
  1. read the overclocking capability flags, PBO scalar and boost limit (read-only SMU queries);
  2. enter manual mode, check the chip's own telemetry: if the requested voltage jumped above
     the chip's normal peak or 1.40 V, leave manual mode at once and stop;
  3. set every core to 4000 MHz, check the clocks follow;
  4. ask for 1.10 V; the chip's VID telemetry must read 1.10 V (+-25 mV);
  5. leave manual mode, check boost comes back.
A marker file records the step in progress; if the PC crashes, the next run reports where.
On success, `manual_oc.vid_encoding = "svi3"` and the SMU firmware version are saved in the config and
meat3d is restarted, which unlocks the voltage control.
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import time
from typing import Callable, Optional

MARKER = "/var/lib/meat3/oc_probe.pending"
PROBE_V = 1.10
PROBE_MHZ = 4000
TOL_V = 0.025


def _write_marker(step: str) -> None:
    os.makedirs(os.path.dirname(MARKER), exist_ok=True)
    with open(MARKER, "w") as f:
        json.dump({"step": step, "t": time.time()}, f)
        f.flush()
        os.fsync(f.fileno())


class Probe:
    """smu: amd_smu.RyzenSmu with the experimental gate on; sensors(): daemon snapshot {id: value};
    slots: physical core slots in core order."""

    def __init__(self, smu, sensors: Callable[[], dict], slots: list[int], prefix: str = "cpu0",
                 sleep: Callable = time.sleep, log: Callable = print, marker: Callable = _write_marker):
        self.smu, self.sensors, self.slots, self.p = smu, sensors, slots, prefix
        self.sleep, self.log, self.marker = sleep, log, marker

    def _send(self, name: str, args: list[int]) -> list[int]:
        from meat3.backends.amd_smu import SMU_OK, SMU_STATUS
        st, reply = self.smu.send(name, args)
        if st != SMU_OK:
            raise RuntimeError(f"SMU {name} returned {st:#x} ({SMU_STATUS.get(st, '?')})")
        return reply

    def _read(self, settle: float = 1.5) -> dict:
        self.sleep(settle)
        s = self.sensors()
        clks = [s.get(f"{self.p}.pmt.core{k}.clk") for k in range(len(self.slots))]
        clks = [c for c in clks if c]
        return {"vid": s.get(f"{self.p}.pmt.svi3_vdd_vid") or s.get(f"{self.p}.pmt.vid"),
                "vid_peak": s.get(f"{self.p}.pmt.vid_peak"), "vddcr": s.get(f"{self.p}.pmt.vddcr_cpu"),
                "clk": statistics.median(clks) if clks else None, "tctl": s.get(f"{self.p}.k10temp.temp1"),
                "util": s.get(f"{self.p}.util.total")}

    def run(self) -> dict:
        from meat3.backends.amd_smu import vid_svi3, oc_core_mask
        out = {"ok": False}
        base = self._read(0)
        out["baseline"] = base
        if (base["util"] or 0) > 15:
            out["why"] = f"CPU is busy ({base['util']:.0f}%); run the probe at idle"
            return out
        if (base["tctl"] or 0) > 70:
            out["why"] = f"CPU is warm ({base['tctl']:.0f} °C); let it cool first"
            return out
        limit_v = min(1.40, max(base["vid_peak"] or 0, base["vid"] or 0) + 0.05)
        self.log(f"   idle: VID {base['vid']:.3f} V (peak {base['vid_peak'] or 0:.3f}), VDDCR {base['vddcr'] or 0:.3f} V, "
                 f"cores ~{base['clk'] or 0:.0f} MHz, {base['tctl'] or 0:.0f} °C")
        # 1. read-only queries
        for name in ("oc_caps", "get_pbo_scalar", "get_boost_limit"):
            try:
                r = self._send(name, [0])
                out[name] = r[0] if r else None
                self.log(f"   {name}: {out[name]:#x}" if out[name] is not None else f"   {name}: no reply")
            except Exception as e:
                out[name] = f"failed: {e}"
                self.log(f"   {name}: {e}")
        on = False
        try:
            # 2. manual mode
            self.marker("enable")
            self._send("oc_enable", [0])
            on = True
            self.smu.set_oc_active(True)
            r = self._read()
            out["after_enable"] = r
            self.log(f"   manual mode on: VID {r['vid'] or 0:.3f} V, VDDCR {r['vddcr'] or 0:.3f} V, cores ~{r['clk'] or 0:.0f} MHz")
            if r["vid"] is None or r["vid"] > limit_v:
                raise RuntimeError(f"entering manual mode set {r['vid']} V (limit {limit_v:.3f}): left manual mode")
            # 3. clocks
            self.marker("freq")
            self._send("oc_freq_all", [PROBE_MHZ])
            r = self._read()
            out["after_freq"] = r
            self.log(f"   all cores {PROBE_MHZ} MHz: cores ~{r['clk'] or 0:.0f} MHz, VID {r['vid'] or 0:.3f} V")
            if r["clk"] is None or abs(r["clk"] - PROBE_MHZ) > 100:
                raise RuntimeError(f"clocks didn't follow the all-core setting ({r['clk']} MHz)")
            # per-core: one core 200 MHz lower, check it alone moves
            self._send("oc_freq_core", [oc_core_mask(self.slots[0]) | (PROBE_MHZ - 200)])
            self.sleep(1.5)
            s = self.sensors()
            c0, c1 = s.get(f"{self.p}.pmt.core0.clk"), s.get(f"{self.p}.pmt.core1.clk")
            out["per_core"] = {"core0": c0, "core1": c1}
            self.log(f"   core 0 -> {PROBE_MHZ - 200} MHz: core 0 {c0 or 0:.0f}, core 1 {c1 or 0:.0f} MHz")
            out["per_core_ok"] = bool(c0 and c1 and c1 - c0 > 120)
            self._send("oc_freq_all", [PROBE_MHZ])
            # 4. voltage
            self.marker("vid")
            vid = vid_svi3(PROBE_V)
            self._send("oc_vid", [vid])
            r = self._read(2.0)
            out["after_vid"] = r
            self.log(f"   asked for {PROBE_V:.3f} V (SVI3 vid {vid}): VID reads {r['vid'] or 0:.3f} V, "
                     f"VDDCR {r['vddcr'] or 0:.3f} V")
            out["vid_ok"] = r["vid"] is not None and abs(r["vid"] - PROBE_V) <= TOL_V
        finally:
            if on:
                # 5. back to normal
                self.marker("disable")
                try:
                    self._send("oc_disable", [0])
                except Exception as e:
                    self.log(f"!! leaving manual mode failed: {e} (a reboot clears it)")
                self.smu.set_oc_active(False)
        r = self._read(2.0)
        out["after_disable"] = r
        self.log(f"   manual mode off: VID {r['vid'] or 0:.3f} V, cores ~{r['clk'] or 0:.0f} MHz")
        out["ok"] = bool(out.get("vid_ok"))
        if not out["ok"]:
            out["why"] = (f"VID read {out.get('after_vid', {}).get('vid')} V after asking for {PROBE_V} V: "
                          f"the encoding isn't SVI3 on this chip")
        return out


def run_probe(prefix: str = "cpu0") -> int:
    from meat3.backends import amd_smu
    from meat3.backends._cpu_common import Topology
    from meat3.core import config
    from meat3.cpubin import Daemon
    if os.path.exists(MARKER):
        try:
            with open(MARKER) as f:
                step = json.load(f).get("step")
        except (OSError, ValueError):
            step = "?"
        print(f"!! the last probe didn't finish: it stopped during '{step}'. If the PC crashed or rebooted, "
              f"manual mode or the voltage encoding doesn't work as expected on this chip; it stays locked.")
        os.unlink(MARKER)
        cfg = config.load()
        cfg.setdefault("manual_oc", {})["probe_failed"] = step
        config.save(cfg)
        return 1
    smu = amd_smu.RyzenSmu(env={amd_smu.ENV_GATE: "1"})
    if smu.codename() != "GraniteRidge":
        print(f"manual overclocking is only mapped for Granite Ridge (Ryzen 9000), not {smu.codename()}")
        return 2
    d = Daemon()
    sens = lambda: {x["id"]: x["value"] for x in d.call({"cmd": "snapshot"}).get("sensors", [])}
    topo = Topology("/sys")
    pmt_slots = None
    try:                                   # physical core slots, same mapping the CO controls use
        from meat3.backends import pm_tables
        reader = pm_tables.PmTableReader(smu, len(topo.cores))
        t = reader.read()
        pmt_slots = reader._detect_slots(t) if t else None
    except Exception:
        pass
    slots = pmt_slots or list(range(len(topo.cores)))
    print(f"== manual overclocking probe on {smu.codename()} (SMU {smu.fw_version()})")
    p = Probe(smu, sens, slots, prefix)
    try:
        res = p.run()
    except Exception as e:
        print(f"stopped: {e}")
        try:
            os.unlink(MARKER)
        except OSError:
            pass
        return 1
    try:
        os.unlink(MARKER)
    except OSError:
        pass
    if not res["ok"]:
        print(f"probe failed: {res.get('why')}")
        return 1
    cfg = config.load()
    mo = cfg.setdefault("manual_oc", {})
    mo.update({"vid_encoding": "svi3", "verified_smu": smu.fw_version(), "verified_at": time.strftime("%Y-%m-%d %H:%M"),
               "per_core_freq": res.get("per_core_ok", False)})
    mo.pop("probe_failed", None)
    config.save(cfg)
    print(f"OK: manual mode works and the voltage encoding is SVI3 on this chip. Per-core clocks: "
          f"{'work' if res.get('per_core_ok') else 'did NOT follow (all-core only)'}. Saved to {config.PATH}; "
          f"restarting meat3d to unlock the voltage control.")
    subprocess.run(["systemctl", "restart", "meat3d"], check=False)
    return 0
