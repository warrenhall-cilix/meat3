"""Tests for meat3.backends.cpu_amd / pm_tables / amd_smu / cpu_generic (fixture based, no hardware).

Run: python3 -m pytest tests/test_cpu_amd.py   or   python3 tests/test_cpu_amd.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from meat3.backends import amd_smu, cpu_amd, pm_tables  # noqa: E402
from meat3.backends.cpu_amd import AmdCpuBackend, encode_pmu_config  # noqa: E402
from meat3.backends.cpu_generic import GenericCpuBackend  # noqa: E402
from meat3.core.model import Control, Sensor  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
ZEN5 = os.path.join(FIX, "zen5_9800x3d")
INTEL = os.path.join(FIX, "intel_generic")


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakePerf:
    """perf shim: counters advance by a per-(type, config, cpu) step on every read."""

    def __init__(self, steps=None, fail=False):
        self.steps = steps or {}
        self.fail = fail
        self.opened = []        # (type, config, cpu)
        self.counts = {}
        self.closed = []

    def open(self, typ, config, cpu):
        if self.fail:
            raise OSError(13, "Permission denied")
        self.opened.append((typ, config, cpu))
        fd = len(self.opened) + 100
        self.counts[fd] = [0, self.steps.get((typ, config), 0)]
        return fd

    def read(self, fd):
        c = self.counts[fd]
        v = c[0]
        c[0] += c[1]
        return v

    def close(self, fd):
        self.closed.append(fd)


def tree_digest(root: str) -> str:
    h = hashlib.sha256()
    for d, _, files in sorted(os.walk(root)):
        for f in sorted(files):
            p = os.path.join(d, f)
            if os.path.islink(p):
                continue
            st = os.stat(p)
            h.update(p.encode())
            h.update(str(st.st_mtime_ns).encode())
            with open(p, "rb") as fh:
                h.update(fh.read(1 << 16))
    return h.hexdigest()


class FakeMsr:
    """Register-level MSR fake (a sparse file can't model overlapping MSR offsets like 0xE7/0xE8)."""

    def __init__(self):
        self.regs = {}
        self.error = ""

    def available(self, cpu=0):
        return True

    def read(self, cpu, reg):
        return self.regs.get((cpu, reg))

    def close(self):
        pass


class Base(unittest.TestCase):
    fixture = ZEN5

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meat3test")
        self.root = os.path.join(self.tmp, "root")
        shutil.copytree(self.fixture, self.root, symlinks=True)
        self.sys = os.path.join(self.root, "sys")
        self.proc = os.path.join(self.root, "proc")
        self.dev = os.path.join(self.tmp, "devcpu")      # empty -> msr unavailable unless populated
        os.makedirs(self.dev)
        self.smu = os.path.join(self.sys, "kernel/ryzen_smu_drv")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def backend(self, perf=None, env=None, clock=None, msr=None):
        self.clock = clock or Clock()
        kw = {"msr_reader": msr} if msr is not None else {}
        return AmdCpuBackend.detect(sys_root=self.sys, proc_root=self.proc, dev_cpu_root=self.dev,
                                    perf=perf if perf is not None else FakePerf(), clock=self.clock,
                                    env=env if env is not None else {}, **kw)[0]

    def by_id(self, sensors):
        return {s.id: s for s in sensors}

    def put(self, rel, value):
        with open(os.path.join(self.root, rel), "w") as f:
            f.write(f"{value}\n")

    def get(self, rel):
        with open(os.path.join(self.root, rel)) as f:
            return f.read().strip()

    # ---- fake /dev/cpu/N/msr (sparse files; pread at offset = MSR number)
    def msr_write(self, cpu, reg, value):
        d = os.path.join(self.dev, str(cpu))
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "msr")
        with open(p, "r+b" if os.path.exists(p) else "w+b") as f:
            f.seek(reg)
            f.write(struct.pack("<Q", value))


# =========================================================================== detection
class TestDetect(Base):
    def test_detect_amd(self):
        b = self.backend()
        self.assertEqual(b.kind, "cpu")
        self.assertIn("9800X3D", b.name)
        self.assertEqual(len(b.topo.cpus), 16)
        self.assertEqual(len(b.topo.cores), 8)
        self.assertEqual(b.topo.cores[0], [0, 8])
        self.assertEqual(list(b.topo.l3_domains), [0])
        self.assertEqual(GenericCpuBackend.detect(sys_root=self.sys, proc_root=self.proc), [])

    def test_not_on_intel(self):
        self.assertEqual(AmdCpuBackend.detect(sys_root=os.path.join(INTEL, "sys"), proc_root=os.path.join(INTEL, "proc")), [])

    def test_detect_never_raises(self):
        self.assertEqual(AmdCpuBackend.detect(sys_root="/nonexistent", proc_root="/nonexistent"), [])


# =========================================================================== PM table
class TestPmTable(Base):
    def test_version_and_layout(self):
        b = self.backend()
        self.assertEqual(b.pmt.version, 0x620105)
        self.assertEqual(b.pmt.size, 0x724)
        self.assertEqual(b.pmt.layout.name, "Granite Ridge 1-CCD")

    def test_fields_from_real_dump(self):
        s = self.by_id(self.backend().sensors())
        self.assertEqual(s["cpu0.pmt.fclk"].value, 2000.0)
        self.assertAlmostEqual(s["cpu0.pmt.uclk"].value, 3000.0)
        self.assertAlmostEqual(s["cpu0.pmt.mclk"].value, 3000.0)
        self.assertEqual(s["cpu0.pmt.ppt_limit"].value, 162.0)
        self.assertEqual(s["cpu0.pmt.tdc_limit"].value, 120.0)
        self.assertEqual(s["cpu0.pmt.edc_limit"].value, 180.0)
        self.assertAlmostEqual(s["cpu0.pmt.ppt"].value, 30.7536, places=3)
        self.assertAlmostEqual(s["cpu0.pmt.vddcr_soc"].value, 1.19498, places=4)
        self.assertAlmostEqual(s["cpu0.pmt.igpu_clk"].value, 599.96, places=1)
        # per-core blocks: 8 live slots -> "Core k" labels; power block at 301
        self.assertAlmostEqual(s["cpu0.pmt.core0.power"].value, 0.37920, places=4)
        self.assertAlmostEqual(s["cpu0.pmt.core7.volt"].value, 1.26160, places=4)
        self.assertAlmostEqual(s["cpu0.pmt.core0.clk"].value, 5399.07, places=1)
        self.assertEqual(s["cpu0.pmt.core0.power"].label, "Core 0 power (SMU)")
        self.assertIn("validated", s["cpu0.pmt.core0.power"].hint)
        self.assertNotIn("cpu0.pmt.raw.000", s)

    def test_raw_dump_option(self):
        s = self.by_id(self.backend(env={"MEAT3_PMT_RAW": "1"}).sensors())
        raw = [k for k in s if k.startswith("cpu0.pmt.raw.")]
        self.assertEqual(len(raw), 0x724 // 4)
        self.assertEqual(s["cpu0.pmt.raw.071"].value, 2000.0)
        self.assertIn("2000.000000 fclk", self.backend().pmt.raw_dump())

    def _set_version(self, v):
        with open(os.path.join(self.smu, "pm_table_version"), "wb") as f:
            f.write(struct.pack("<I", v))

    def test_unknown_version_only_validated_clocks(self):
        self._set_version(0x621101)
        s = self.by_id(self.backend().sensors())
        pmt = sorted(k for k in s if k.startswith("cpu0.pmt."))
        self.assertEqual(pmt, ["cpu0.pmt.fclk", "cpu0.pmt.mclk", "cpu0.pmt.uclk"])
        self.assertEqual(s["cpu0.pmt.fclk"].value, 2000.0)
        self.assertIn("passed sanity", s["cpu0.pmt.fclk"].hint)

    def test_unknown_version_failing_sanity(self):
        self._set_version(0x621101)
        with open(os.path.join(self.smu, "pm_table"), "r+b") as f:
            f.seek(79 * 4)
            f.write(struct.pack("<f", 12345.0))     # absurd MCLK
        s = self.by_id(self.backend().sensors())
        self.assertIsNone(s["cpu0.pmt.fclk"].value)
        self.assertIsNone(s["cpu0.pmt.mclk"].value)

    def test_unknown_family(self):
        self._set_version(0x990001)
        s = self.by_id(self.backend().sensors())
        self.assertIsNone(s["cpu0.pmt.fclk"].value)

    def test_size_mismatch_rejects_layout(self):
        with open(os.path.join(self.smu, "pm_table_size"), "wb") as f:
            f.write(struct.pack("<Q", 0x994))
        b = self.backend()
        self.assertIsNone(b.pmt.layout)
        self.assertIn("size", b.pmt.error)

    def test_fused_slots(self):
        p = os.path.join(self.smu, "pm_table")
        with open(p, "r+b") as f:
            for blk in (309, 325, 373):
                for slot in (4, 5):
                    f.seek((blk + slot) * 4)
                    f.write(struct.pack("<f", 0.0))
        b = self.backend()
        s = self.by_id(b.sensors())
        self.assertEqual(b.pmt.slots, [0, 1, 2, 3, 6, 7])
        # slot count != core count -> rows labelled by physical slot
        self.assertEqual(s["cpu0.pmt.core4.volt"].label, "Slot 6 voltage")

    def test_no_ryzen_smu(self):
        shutil.rmtree(self.smu)
        b = self.backend()
        s = self.by_id(b.sensors())
        self.assertIsNone(s["cpu0.pmt.fclk"].value)
        self.assertIn("ryzen_smu", s["cpu0.pmt.fclk"].hint)
        self.assertIn("not loaded", s["cpu0.info.smu"].label)

    def test_other_layouts_consistent(self):
        for ver, lay in pm_tables.LAYOUTS.items():
            keys = [f.key for f in lay.fields]
            self.assertEqual(len(keys), len(set(keys)), hex(ver))
            for f in lay.fields:
                self.assertTrue(lay.size == 0 or f.idx < lay.size // 4, (hex(ver), f.key))
            for c in lay.cores:
                self.assertTrue(lay.size == 0 or c.base + lay.slots <= lay.size // 4, (hex(ver), c.key))
            self.assertIn("fclk", keys)

    def test_check_clocks(self):
        self.assertTrue(pm_tables.check_clocks(2000, 3000, 3000))
        self.assertTrue(pm_tables.check_clocks(2000, 1500, 3000))      # UCLK = MCLK/2
        self.assertFalse(pm_tables.check_clocks(2000, 2100, 3000))
        self.assertFalse(pm_tables.check_clocks(0, 3000, 3000))
        self.assertFalse(pm_tables.check_clocks(None, 3000, 3000))


# =========================================================================== MSR / proc / rapl
class TestCounters(Base):
    TSC_HZ = 5_000_000_000

    def msr_write(self, cpu, reg, value):
        self.fake_msr.regs[(cpu, reg)] = value

    def setUp(self):
        super().setUp()
        self.fake_msr = FakeMsr()

    def _msr_all(self, step: int):
        """Advance all MSRs as if `step` seconds passed: thread 0 at 50% C0 @ 5.4 GHz."""
        for cpu in range(16):
            busy = 0.5 if cpu == 0 else 0.1
            tsc = self.TSC_HZ * step
            mperf = int(tsc * busy)
            aperf = int(5.4e9 * busy * step)
            self.msr_write(cpu, cpu_amd.MSR_TSC, tsc)
            self.msr_write(cpu, cpu_amd.MSR_MPERF, mperf)
            self.msr_write(cpu, cpu_amd.MSR_APERF, aperf)
        self.msr_write(0, cpu_amd.MSR_RAPL_PWR_UNIT, 16 << 8)           # 1/65536 J
        # package counter wraps between samples: 0xFFFF0000 -> 0x00090000 = 0xA0000 units = 10 J
        self.msr_write(0, cpu_amd.MSR_PKG_ENERGY, 0xFFFF0000 if step == 1 else 0x00090000)
        for k in range(8):
            self.msr_write(k, cpu_amd.MSR_CORE_ENERGY, 65536 * 3 * step)  # 3 W per core

    def test_msr_clocks_power(self):
        self._msr_all(1)
        b = self.backend(msr=self.fake_msr)
        b.sensors()
        self._msr_all(2)
        self.clock.t += 1.0
        s = self.by_id(b.sensors())
        self.assertAlmostEqual(s["cpu0.clk.base"].value, 5000.0, places=1)
        self.assertAlmostEqual(s["cpu0.t0.clk_eff"].value, 2700.0, places=1)
        self.assertAlmostEqual(s["cpu0.t0.c0"].value, 50.0, places=3)
        self.assertAlmostEqual(s["cpu0.core0.clk_active"].value, 5400.0, places=1)
        # core 0 = max of threads 0 and 8
        self.assertAlmostEqual(s["cpu0.core0.clk_eff"].value, 2700.0, places=1)
        self.assertAlmostEqual(s["cpu0.core1.clk_eff"].value, 540.0, places=1)
        self.assertAlmostEqual(s["cpu0.core3.power"].value, 3.0, places=3)
        self.assertAlmostEqual(s["cpu0.msr.cores_power"].value, 24.0, places=3)
        self.assertAlmostEqual(s["cpu0.msr.pkg_power"].value, 10.0, places=3)

    def test_msrdev_sparse_file(self):
        d = os.path.join(self.dev, "3")
        os.makedirs(d)
        with open(os.path.join(d, "msr"), "wb") as f:
            f.seek(cpu_amd.MSR_PKG_ENERGY)
            f.write(struct.pack("<Q", 0x1234_5678_9ABC))
        m = cpu_amd.MsrDev(self.dev)
        self.assertEqual(m.read(3, cpu_amd.MSR_PKG_ENERGY), 0x1234_5678_9ABC)
        self.assertIsNone(m.read(3, 0xFFFFFFFFFF))         # beyond EOF -> None
        self.assertIsNone(m.read(5, cpu_amd.MSR_TSC))       # no device
        self.assertIn("msr module", m.error)
        m.close()

    def test_no_msr_fallback(self):
        b = self.backend()
        s = self.by_id(b.sensors())
        self.assertIn("msr", s["cpu0.clk.eff_avg"].hint)
        self.assertNotIn("cpu0.core0.clk_active", s)
        # cpufreq cpuinfo_avg_freq fallback (kHz in fixture)
        self.assertAlmostEqual(s["cpu0.t0.clk_eff"].value, int(self.get("sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_avg_freq")) / 1000)
        self.assertIsNone(s["cpu0.msr.pkg_power"].value)
        self.assertIsNone(s["cpu0.core0.power"].value)

    def test_utilisation_and_cstates(self):
        b = self.backend()
        b.sensors()
        stat = self.get("proc/stat").splitlines()
        new = []
        for line in stat:
            p = line.split()
            if p and p[0] in ("cpu0", "cpu8"):
                vals = [int(x) for x in p[1:]]
                vals[0] += 30 if p[0] == "cpu0" else 0      # user
                vals[3] += 70 if p[0] == "cpu0" else 100    # idle
                line = " ".join([p[0]] + [str(v) for v in vals])
            new.append(line)
        self.put("proc/stat", "\n".join(new))
        st3 = "sys/devices/system/cpu/cpu0/cpuidle/state3/time"
        self.put(st3, int(self.get(st3)) + 400_000)       # 0.4 s in C3
        self.clock.t += 1.0
        s = self.by_id(b.sensors())
        self.assertAlmostEqual(s["cpu0.t0.util"].value, 30.0, places=3)
        self.assertAlmostEqual(s["cpu0.core0.util"].value, 15.0, places=3)   # mean with idle sibling
        self.assertAlmostEqual(s["cpu0.t0.idle.c3"].value, 40.0, places=3)
        self.assertAlmostEqual(s["cpu0.t0.idle.c1"].value, 0.0, places=3)

    def test_rapl(self):
        b = self.backend()
        s = self.by_id(b.sensors())
        self.assertIsNone(s["cpu0.rapl.intel_rapl_0"].value)
        p = "sys/class/powercap/intel-rapl:0/energy_uj"
        self.put(p, int(self.get(p)) + 42_000_000)
        self.clock.t += 2.0
        s = self.by_id(b.sensors())
        self.assertAlmostEqual(s["cpu0.rapl.intel_rapl_0"].value, 21.0)
        self.assertIn("core 0 only", s["cpu0.rapl.intel_rapl_0_0"].label)
        # wrap-around
        rng = int(self.get("sys/class/powercap/intel-rapl:0/max_energy_range_uj"))
        self.put(p, 1_000_000)
        before = int(56528593564 + 42_000_000)
        self.clock.t += 1.0
        s = self.by_id(b.sensors())
        self.assertAlmostEqual(s["cpu0.rapl.intel_rapl_0"].value, (rng - before + 1_000_000) / 1e6, places=3)

    def test_temps_and_misc(self):
        s = self.by_id(self.backend().sensors())
        self.assertEqual(s["cpu0.k10temp.temp1"].label, "Tctl")
        self.assertAlmostEqual(s["cpu0.k10temp.temp1"].value, 35.125)
        self.assertEqual(s["cpu0.k10temp.temp3"].label, "Tccd1")
        self.assertEqual(s["cpu0.info.cores"].value, 8)
        self.assertEqual(s["cpu0.info.threads"].value, 16)
        self.assertIn("powersave", s["cpu0.info.governor"].label)
        self.assertIn("active", s["cpu0.info.pstate"].label)
        self.assertGreater(s["cpu0.sys.ram_total"].value, 1)
        self.assertIsNotNone(s["cpu0.sys.load0"].value)

    def test_sensor_contract(self):
        b = self.backend()
        before = tree_digest(self.root)
        t = time.perf_counter()
        ss = b.sensors()
        ss = b.sensors()
        self.assertLess((time.perf_counter() - t) / 2, 0.2)
        self.assertEqual(before, tree_digest(self.root), "sensors() modified the fixture tree")
        ids = [s.id for s in ss]
        self.assertEqual(len(ids), len(set(ids)), "duplicate sensor ids")
        for s in ss:
            self.assertIsInstance(s, Sensor)
            self.assertTrue(s.value is None or isinstance(s.value, float) or isinstance(s.value, int), s.id)
            self.assertTrue(s.group.startswith(b.g))
            s.to_dict()
        b.close()


# =========================================================================== perf uncore
class TestUncore(Base):
    def test_encodings(self):
        df = os.path.join(self.sys, "bus/event_source/devices/amd_df/format")
        self.assertEqual(encode_pmu_config(df, event=0x1F, umask=0xFFE), 0x0F00FE1F)
        self.assertEqual(encode_pmu_config(df, event=0x11F, umask=0x7FF),
                         0x1F | (0xFF << 8) | (0x7 << 24) | (0x1 << 32))
        l3 = os.path.join(self.sys, "bus/event_source/devices/amd_l3/format")
        self.assertEqual(encode_pmu_config(l3, event=0x04, umask=0xFF), 0xFF04)
        umc = os.path.join(self.sys, "bus/event_source/devices/amd_umc_0/format")
        self.assertEqual(encode_pmu_config(umc, event=0x0A, rdwrmask=2), 0x20A)
        with self.assertRaises(ValueError):
            encode_pmu_config(l3, event=0x104)
        with self.assertRaises(ValueError):
            encode_pmu_config(l3, nosuch=1)

    def test_l3_and_umc_rates(self):
        steps = {(17, 0xFF04): 50_000_000, (17, 0x0104): 5_000_000}
        for umc_type in (18, 19):
            steps.update({(umc_type, 0x10A): 10_000_000, (umc_type, 0x20A): 5_000_000,
                          (umc_type, 0x0): 3_000_000_000})
        perf = FakePerf(steps)
        b = self.backend(perf=perf)
        self.assertIn((17, 0xFF04, 0), perf.opened)
        self.assertIn((17, 0x0104, 0), perf.opened)
        self.assertIn((18, 0x10A, 0), perf.opened)
        b.sensors()
        self.clock.t += 1.0
        s = self.by_id(b.sensors())
        self.assertAlmostEqual(s["cpu0.l3.ccx0.hit_rate"].value, 90.0)
        self.assertAlmostEqual(s["cpu0.l3.ccx0.accesses"].value, 50.0)
        self.assertAlmostEqual(s["cpu0.l3.ccx0.misses"].value, 5.0)
        # 2 UMCs x 10M CAS x 64 B = 1.28 GB/s read
        self.assertAlmostEqual(s["cpu0.mem.bw_read"].value, 1.28)
        self.assertAlmostEqual(s["cpu0.mem.bw_write"].value, 0.64)
        self.assertAlmostEqual(s["cpu0.mem.umc_clk"].value, 3000.0)
        b.close()
        self.assertEqual(len(perf.closed), len(perf.opened))

    def test_zen3_l3_events(self):
        self.assertEqual(cpu_amd.l3_events_for(0x19, 0x21)["miss"], {"event": 0x06, "umask": 0x01})
        self.assertEqual(cpu_amd.l3_events_for(0x19, 0x61)["miss"], {"event": 0x04, "umask": 0x01})
        self.assertEqual(cpu_amd.l3_events_for(0x1A, 0x44)["access"], {"event": 0x04, "umask": 0xFF})

    def test_perf_unavailable(self):
        b = self.backend(perf=FakePerf(fail=True))
        s = self.by_id(b.sensors())
        self.assertIsNone(s["cpu0.l3.ccx0.hit_rate"].value)
        self.assertIn("Permission denied", s["cpu0.l3.ccx0.hit_rate"].hint)
        self.assertIsNone(s["cpu0.mem.bw_read"].value)

    def test_no_uncore_pmu(self):
        shutil.rmtree(os.path.join(self.sys, "bus/event_source/devices"))
        s = self.by_id(self.backend().sensors())
        self.assertIn("amd_uncore", s["cpu0.l3.ccx0.hit_rate"].hint)
        self.assertIsNone(s["cpu0.mem.bw_total"].value)


# =========================================================================== cpufreq controls
class TestCpufreqControls(Base):
    def ctl(self, b):
        return {c.id: c for c in b.controls()}

    def pol(self, n, f):
        return self.get(f"sys/devices/system/cpu/cpufreq/policy{n}/{f}")

    def test_controls_listing(self):
        c = self.ctl(self.backend())
        self.assertEqual(c["cpu0.cpufreq.governor"].choices, ["performance", "powersave"])
        self.assertEqual(c["cpu0.cpufreq.governor"].value, "powersave")
        self.assertIn("balance_power", c["cpu0.cpufreq.epp"].choices)
        self.assertIs(c["cpu0.cpufreq.boost"].value, True)
        self.assertEqual(c["cpu0.cpufreq.max_freq"].min, 693)
        self.assertEqual(c["cpu0.cpufreq.max_freq"].max, 6057)
        self.assertEqual(c["cpu0.cpufreq.pstate_mode"].value, "active")
        for x in c.values():
            self.assertIsInstance(x, Control)
            x.to_dict()

    def test_governor(self):
        b = self.backend()
        ok, msg = b.apply("cpu0.cpufreq.governor", "performance")
        self.assertTrue(ok, msg)
        self.assertTrue(all(self.pol(n, "scaling_governor") == "performance" for n in range(16)))
        for bad in ("ondemand", "", None, 1, "performance\n"):
            self.assertFalse(b.apply("cpu0.cpufreq.governor", bad)[0], bad)

    def test_epp(self):
        b = self.backend()
        self.assertTrue(b.apply("cpu0.cpufreq.epp", "power")[0])
        self.assertEqual(self.pol(15, "energy_performance_preference"), "power")
        self.assertFalse(b.apply("cpu0.cpufreq.epp", "turbo")[0])

    def test_boost(self):
        b = self.backend()
        self.assertTrue(b.apply("cpu0.cpufreq.boost", False)[0])
        self.assertEqual(self.get("sys/devices/system/cpu/cpufreq/boost"), "0")
        self.assertTrue(b.apply("cpu0.cpufreq.boost", 1)[0])
        self.assertEqual(self.get("sys/devices/system/cpu/cpufreq/boost"), "1")
        self.assertFalse(b.apply("cpu0.cpufreq.boost", "yes")[0])
        self.assertFalse(b.apply("cpu0.cpufreq.boost", 2)[0])

    def test_freq_limits(self):
        b = self.backend()
        ok, msg = b.apply("cpu0.cpufreq.max_freq", 5000)
        self.assertTrue(ok, msg)
        self.assertEqual(self.pol(3, "scaling_max_freq"), "5000000")
        self.assertFalse(b.apply("cpu0.cpufreq.max_freq", 7000)[0])
        self.assertFalse(b.apply("cpu0.cpufreq.max_freq", 100)[0])
        self.assertFalse(b.apply("cpu0.cpufreq.max_freq", 4000.5)[0])
        self.assertFalse(b.apply("cpu0.cpufreq.max_freq", True)[0])
        self.assertFalse(b.apply("cpu0.cpufreq.min_freq", 5500)[0])       # above current max (5000)
        self.assertTrue(b.apply("cpu0.cpufreq.min_freq", 2000)[0])
        self.assertFalse(b.apply("cpu0.cpufreq.max_freq", 1500)[0])       # below current min (2000)
        # max at the advertised ceiling clamps to cpuinfo_max_freq exactly
        self.assertTrue(b.apply("cpu0.cpufreq.max_freq", 6057)[0])
        self.assertEqual(self.pol(0, "scaling_max_freq"), self.pol(0, "cpuinfo_max_freq"))

    def test_pstate_mode(self):
        b = self.backend()
        self.assertTrue(b.apply("cpu0.cpufreq.pstate_mode", "guided")[0])
        self.assertEqual(self.get("sys/devices/system/cpu/amd_pstate/status"), "guided")
        self.assertFalse(b.apply("cpu0.cpufreq.pstate_mode", "disable")[0])
        self.assertFalse(b.apply("cpu0.nope", 1)[0])


# =========================================================================== SMU (never touches hardware)
class TestSmu(Base):
    def test_encodings(self):
        self.assertEqual(amd_smu.co_margin_bits(-1), 0xFFFF)
        self.assertEqual(amd_smu.co_margin_bits(-30), 0xFFE2)
        self.assertEqual(amd_smu.co_margin_bits(15), 0x000F)
        self.assertEqual(amd_smu.co_core_arg(0, -1), 0x0000FFFF)
        self.assertEqual(amd_smu.co_core_arg(3, -20), 0x0030FFEC)
        self.assertEqual(amd_smu.co_core_arg(9, 5), 0x10100005)       # CCD1 core 1
        self.assertEqual(amd_smu.co_core_mask(15), 0x10700000)
        self.assertEqual(amd_smu.decode_co_reply(0xFFE2), -30)
        self.assertEqual(amd_smu.decode_co_reply(0x12340005), 5)

    def test_disabled_by_default(self):
        b = self.backend()
        c = {x.id: x for x in b.controls()}
        for k in ("cpu0.smu.ppt_limit", "cpu0.smu.co_all", "cpu0.smu.co_core0"):
            self.assertFalse(c[k].available)
            self.assertEqual(c[k].danger, 2)
            self.assertIn("MEAT3_EXPERIMENTAL_SMU", c[k].hint)
        # limits read back from the PM table
        self.assertEqual(c["cpu0.smu.ppt_limit"].value, 162)
        self.assertEqual(c["cpu0.smu.tdc_limit"].value, 120)
        self.assertEqual(c["cpu0.smu.edc_limit"].value, 180)
        self.assertIsNone(c["cpu0.smu.co_core0"].value)
        ok, msg = b.apply("cpu0.smu.co_all", -10)
        self.assertFalse(ok)
        self.assertIn("MEAT3_EXPERIMENTAL_SMU", msg)
        self.assertFalse(os.path.exists(os.path.join(self.smu, "smu_args")))

    def test_validation_before_gate(self):
        b = self.backend(env={"MEAT3_EXPERIMENTAL_SMU": "1"})
        for cid, v in (("cpu0.smu.co_all", -51), ("cpu0.smu.co_all", 31), ("cpu0.smu.co_core0", 1.5),
                       ("cpu0.smu.co_core8", -5), ("cpu0.smu.ppt_limit", 301), ("cpu0.smu.ppt_limit", 10),
                       ("cpu0.smu.tdc_limit", True), ("cpu0.smu.edc_limit", "200"), ("cpu0.smu.bogus", 1)):
            ok, msg = b.apply(cid, v)
            self.assertFalse(ok, (cid, v, msg))
        self.assertFalse(os.path.exists(os.path.join(self.smu, "smu_args")))

    def test_send_protocol_on_fake_files(self):
        # Fake driver files in the TEMP fixture: a plain file echoes the command id back as the
        # "status", so the backend must report failure - but we can check the exact bytes written.
        for f in ("smu_args", "rsmu_cmd", "mp1_smu_cmd"):
            open(os.path.join(self.smu, f), "wb").close()
        b = self.backend(env={"MEAT3_EXPERIMENTAL_SMU": "1"})
        c = {x.id: x for x in b.controls()}
        self.assertTrue(c["cpu0.smu.co_core3"].available)
        ok, msg = b.apply("cpu0.smu.co_core3", -20)
        self.assertFalse(ok)
        self.assertIn("0x35", msg)
        with open(os.path.join(self.smu, "smu_args"), "rb") as f:
            self.assertEqual(struct.unpack("<6I", f.read()), (0x0030FFEC, 0, 0, 0, 0, 0))
        with open(os.path.join(self.smu, "mp1_smu_cmd"), "rb") as f:
            self.assertEqual(struct.unpack("<I", f.read())[0], 0x35)
        ok, msg = b.apply("cpu0.smu.ppt_limit", 120)
        with open(os.path.join(self.smu, "smu_args"), "rb") as f:
            self.assertEqual(struct.unpack("<6I", f.read())[0], 120000)
        with open(os.path.join(self.smu, "mp1_smu_cmd"), "rb") as f:
            self.assertEqual(struct.unpack("<I", f.read())[0], 0x3E)

    def test_send_success_path(self):
        class OkSmu(amd_smu.RyzenSmu):
            sent = []

            def send(self, name, args):
                if not self.experimental():
                    raise amd_smu.SmuDisabled("off")
                self.sent.append((name, list(args)))
                return (1, [0xFFE2, 0, 0, 0, 0, 0]) if name == "get_co_core" else (1, [0] * 6)

        smu = OkSmu(self.smu, env={"MEAT3_EXPERIMENTAL_SMU": "1"})
        ctl = {c.id: c for c in amd_smu.smu_controls(smu, None, "cpu0", "G", ncores=8)}
        self.assertEqual(ctl["cpu0.smu.co_core2"].value, -30)
        n = len(smu.sent)
        amd_smu.smu_controls(smu, None, "cpu0", "G", ncores=8)       # cached: no new reads
        self.assertEqual(len(smu.sent), n)
        ok, msg = amd_smu.smu_apply(smu, None, "cpu0", "cpu0.smu.co_core2", -12, ncores=8)
        self.assertTrue(ok, msg)
        self.assertEqual(smu.sent[-1], ("set_co_core", [0x0020FFF4]))
        self.assertEqual(smu.co_cache[2], -12)
        ok, _ = amd_smu.smu_apply(smu, None, "cpu0", "cpu0.smu.co_all", 0, ncores=8)
        self.assertTrue(ok)
        self.assertEqual(smu.sent[-1], ("set_co_all", [0]))

    def test_unknown_codename_unavailable(self):
        with open(os.path.join(self.smu, "codename"), "w") as f:
            f.write("20\n")    # Raphael: no verified command table
        b = self.backend(env={"MEAT3_EXPERIMENTAL_SMU": "1"})
        c = {x.id: x for x in b.controls()}
        self.assertFalse(c["cpu0.smu.co_all"].available)
        self.assertIn("Raphael", c["cpu0.smu.co_all"].hint)


# =========================================================================== generic (Intel)
class TestGeneric(Base):
    fixture = INTEL

    def test_generic(self):
        clock = Clock()
        b = GenericCpuBackend.detect(sys_root=self.sys, proc_root=self.proc, clock=clock)[0]
        self.assertIn("i5-13400", b.name)
        s = self.by_id(b.sensors())
        self.assertEqual(s["cpu0.t1.clk_eff"].value, 3100.0)
        self.assertAlmostEqual(s["cpu0.clk.eff_avg"].value, 3150.0)
        self.assertEqual(s["cpu0.coretemp_hwmon2.temp1"].label, "Package id 0")
        self.put("sys/class/powercap/intel-rapl:0/energy_uj", 16_000_000)
        clock.t += 1.0
        s = self.by_id(b.sensors())
        self.assertAlmostEqual(s["cpu0.rapl.intel_rapl_0"].value, 15.0)
        c = {x.id: x for x in b.controls()}
        self.assertNotIn("cpu0.cpufreq.pstate_mode", c)
        self.assertTrue(c["cpu0.cpufreq.boost"].value)
        self.assertTrue(b.apply("cpu0.cpufreq.boost", False)[0])
        self.assertEqual(self.get("sys/devices/system/cpu/intel_pstate/no_turbo"), "1")
        self.assertFalse(b.apply("cpu0.cpufreq.max_freq", 5000)[0])
        self.assertTrue(b.apply("cpu0.cpufreq.max_freq", 4000)[0])
        self.assertEqual(self.get("sys/devices/system/cpu/cpufreq/policy2/scaling_max_freq"), "4000000")


class TestResctrlL3(unittest.TestCase):
    """L3 occupancy per L3 domain (CCD) and MBM bandwidth from a fake resctrl tree."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meat3-resctrl-")
        self.mon = {}
        for cid, cpus in ((0, (0, 1)), (1, (8, 9))):
            for c in cpus:
                d = os.path.join(self.tmp, f"devices/system/cpu/cpu{c}/cache/index3")
                os.makedirs(d)
                open(os.path.join(d, "id"), "w").write(f"{cid}\n")
                open(os.path.join(d, "size"), "w").write("32768K\n")
            m = os.path.join(self.tmp, f"fs/resctrl/mon_data/mon_L3_{cid:02d}")
            os.makedirs(m)
            self.mon[cid] = m
        self.write(0, 16_777_216, 1_000_000_000)
        self.write(1, 33_554_432, 0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, cid, occ, mbm):
        open(os.path.join(self.mon[cid], "llc_occupancy"), "w").write(f"{occ}\n")
        open(os.path.join(self.mon[cid], "mbm_total_bytes"), "w").write(f"{mbm}\n")

    def test_occupancy_and_bandwidth(self):
        r = cpu_amd.ResctrlL3(self.tmp, {}, time.monotonic)
        s = r.sample(10.0)
        self.assertEqual(s[0]["occ"], 16_777_216)
        self.assertEqual(s[1]["size"], 32 << 20)
        self.assertNotIn("mbm_rate", s[0])            # first sample: no rate yet
        self.write(0, 16_777_216, 3_000_000_000)
        s = r.sample(12.0)
        self.assertAlmostEqual(s[0]["mbm_rate"], 1e9)
        self.assertIsNone(r.error)

    def test_unmounted_outside_real_sys(self):
        empty = tempfile.mkdtemp(prefix="meat3-resctrl-none-")
        try:
            r = cpu_amd.ResctrlL3(empty, {}, time.monotonic)
            self.assertEqual(r.sample(1.0), {})
            self.assertIn("resctrl", r.error)
        finally:
            shutil.rmtree(empty, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
