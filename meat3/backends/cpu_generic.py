"""Generic CPU backend for non-AMD-Zen systems (Intel and older AMD).

Uses only portable kernel interfaces: cpufreq (clocks + controls), /proc/stat (usage), cpuidle
(C-state residency), coretemp/any CPU hwmon (temperatures) and powercap RAPL (package/core/
uncore/dram power). Read-only except the validated cpufreq controls shared with cpu_amd.
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

from meat3.core.model import Backend, Control, Sensor
from meat3.backends import _cpu_common as cc
from meat3.backends._cpu_common import read_int


class GenericCpuBackend(Backend):
    kind = "cpu"

    def __init__(self, sys_root: str = "/sys", proc_root: str = "/proc", clock=time.monotonic,
                 cpuinfo: Optional[dict] = None):
        self.sys_root, self.proc_root, self.clock = sys_root, proc_root, clock
        self.info = cpuinfo or cc.parse_cpuinfo(proc_root)
        self.name = self.info["model_name"] or "CPU"
        self.prefix = "cpu0"
        self.g = f"CPU: {self.name}"
        self.topo = cc.Topology(sys_root)
        self.stat = cc.ProcStat(proc_root)
        self.idle = cc.CpuIdle(sys_root, self.topo.cpus)
        self.rapl = cc.rapl_zones(sys_root)
        self.cpufreq = cc.CpufreqControls(sys_root, self.prefix, self.g)
        self.hwmon = [(p, cc.read_str(os.path.join(p, "name")), cc.hwmon_channels(p))
                      for p in cc.find_hwmon(sys_root, ("coretemp", "k8temp", "fam15h_power", "via_cputemp"))]

    @classmethod
    def detect(cls, **kw) -> list["GenericCpuBackend"]:
        try:
            info = cc.parse_cpuinfo(kw.get("proc_root", "/proc"))
            if info["vendor"] in ("AuthenticAMD", "HygonGenuine") and info["family"] >= 0x17:
                return []          # handled by cpu_amd
            return [cls(cpuinfo=info, **kw)]
        except Exception:
            return []

    def sensors(self) -> list[Sensor]:
        now = self.clock()
        g, p = self.g, self.prefix
        out: list[Sensor] = []
        util = self.stat.sample()
        idle = self.idle.sample(now)
        clks = []
        for cpu in self.topo.cpus:
            base = os.path.join(self.sys_root, f"devices/system/cpu/cpu{cpu}/cpufreq")
            v, src = None, "no cpufreq"
            for f in ("cpuinfo_avg_freq", "scaling_cur_freq"):
                r = read_int(os.path.join(base, f))
                if r is not None:
                    v, src = r / 1000.0, f"cpufreq {f}"
                    break
            if v is not None:
                clks.append(v)
            k = self.topo.core_of_cpu.get(cpu, 0)
            out.append(Sensor(f"{p}.t{cpu}.clk_eff", f"{g}/Clocks", f"Thread {cpu} (core {k}) clock", v, "MHz", 0, src))
        out.insert(0, Sensor(f"{p}.clk.eff_avg", f"{g}/Clocks", "Average clock",
                             sum(clks) / len(clks) if clks else None, "MHz", 0, "mean of cpufreq per-thread clocks"))
        out.append(Sensor(f"{p}.util.total", f"{g}/Usage", "Total CPU usage", util.get("cpu"), "%", 1, "/proc/stat"))
        for k, cpus in enumerate(self.topo.cores):
            us = [util.get(f"cpu{c}") for c in cpus if util.get(f"cpu{c}") is not None]
            out.append(Sensor(f"{p}.core{k}.util", f"{g}/Usage", f"Core {k} usage",
                              sum(us) / len(us) if us else None, "%", 1, "/proc/stat, mean of SMT siblings"))
        for cpu in self.topo.cpus:
            out.append(Sensor(f"{p}.t{cpu}.util", f"{g}/Threads", f"Thread {cpu} usage", util.get(f"cpu{cpu}"),
                              "%", 1, "/proc/stat"))
            for _, n in self.idle.states.get(cpu, []):
                out.append(Sensor(f"{p}.t{cpu}.idle.{cc.slug(n)}", f"{g}/C-states", f"Thread {cpu} {n} residency",
                                  idle.get((cpu, n)), "%", 1, "cpuidle sysfs"))
        for path, name, chans in self.hwmon:
            dev = os.path.basename(os.path.realpath(os.path.join(path, "device"))) \
                if os.path.exists(os.path.join(path, "device")) else os.path.basename(path)
            for ch in chans:
                out.append(Sensor(f"{p}.{cc.slug(name + '_' + dev)}.{ch['key']}", f"{g}/Temperatures", ch["label"],
                                  cc.read_hwmon_value(ch), ch["unit"], ch["dec"], f"{name} hwmon"))
        if not self.hwmon:
            out.append(Sensor(f"{p}.temp.package", f"{g}/Temperatures", "Package", None, "°C", 1,
                              "no coretemp hwmon (modprobe coretemp)"))
        for z in self.rapl:
            w = z.power(now)
            out.append(Sensor(f"{p}.rapl.{cc.slug(os.path.basename(z.path))}", f"{g}/Power", f"RAPL {z.name}", w, "W", 2,
                              f"powercap {os.path.basename(z.path)}" + (f" ({z.error})" if w is None and z.error else "")))
        out.append(Sensor(f"{p}.info.model", f"{g}/System", self.name, None, hint="/proc/cpuinfo"))
        out.append(Sensor(f"{p}.info.cores", f"{g}/System", "Physical cores", float(len(self.topo.cores)), "", 0))
        out.append(Sensor(f"{p}.info.threads", f"{g}/System", "Logical processors", float(len(self.topo.cpus)), "", 0))
        out += self.cpufreq.info_sensors(f"{g}/System")
        out += cc.misc_sensors(p, f"{g}/System", self.proc_root)
        return out

    def controls(self) -> list[Control]:
        return self.cpufreq.controls()

    def apply(self, control_id: str, value: Any) -> tuple[bool, str]:
        if self.cpufreq.handles(control_id):
            return self.cpufreq.apply(control_id, value)
        return False, f"unknown control {control_id}"


BACKENDS = [GenericCpuBackend]
