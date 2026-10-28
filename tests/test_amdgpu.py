"""Tests for the amdgpu backend. Runs with pytest or plain `python3 tests/test_amdgpu.py`.

All tests run against fixture trees in temporary directories; nothing touches /sys.
"""
from __future__ import annotations

import os
import shutil
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(HERE, "fixtures", "amdgpu"))

import make_fixtures as fx  # noqa: E402
from meat3.backends import amdgpu as A  # noqa: E402
from meat3.backends import amdgpu_metrics as GM  # noqa: E402
from meat3.backends import amdgpu_metrics_defs as D  # noqa: E402
from meat3.backends import amdgpu_od as OD  # noqa: E402
from meat3.core.model import Control, Sensor  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "amdgpu")
DEV = "/sys/class/drm/card0/device"


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, path, data):
        self.calls.append((path, data))


class TreeCase(unittest.TestCase):
    """Builds a fresh RX 9070 XT tree per test and a backend with a recording writer."""
    od = True

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meat3-amdgpu-")
        self.root = os.path.join(self.tmp, "root")
        fx.build(self.root, od=self.od)
        self.rec = Recorder()
        devs = A.AmdGpuBackend.detect(self.root)
        self.assertEqual(len(devs), 1)
        self.be = devs[0]
        self.be.io.writer = self.rec

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def writes(self):
        return [(self.be.io.rel(p), d) for p, d in self.rec.calls]

    def ctl(self, suffix) -> Control:
        cs = {c.id: c for c in self.be.controls()}
        return cs[f"amdgpu0.oc.{suffix}"]


# ------------------------------------------------------------------------------------
class TestMetricsLayouts(unittest.TestCase):
    def test_known_sizes(self):
        # sizes as compiled by gcc for x86-64 (generator --verify checks every offset)
        expect = {(1, 0): 80, (1, 1): 96, (1, 2): 104, (1, 3): 120, (1, 4): 288, (1, 5): 360,
                  (2, 0): 120, (2, 1): 120, (2, 2): 128, (2, 3): 152, (2, 4): 168, (3, 0): 264}
        for ver, size in expect.items():
            self.assertEqual(D.STRUCTS[D.VERSIONS[ver]][0], size, ver)

    def test_v1_3_layout_matches_independent_pack(self):
        self.assertEqual(struct.calcsize(fx.GM_V1_3_FMT), D.STRUCTS["gpu_metrics_v1_3"][0])
        offs = {n: o for n, o, _k, _c in D.STRUCTS["gpu_metrics_v1_3"][1]}
        self.assertEqual(offs["energy_accumulator"], 24)
        self.assertEqual(offs["throttle_status"], 68)
        self.assertEqual(offs["voltage_gfx"], 106)
        self.assertEqual(offs["indep_throttle_status"], 112)

    def test_v1_0_is_naturally_aligned(self):
        offs = {n: o for n, o, _k, _c in D.STRUCTS["gpu_metrics_v1_0"][1]}
        self.assertEqual(offs["system_clock_counter"], 8)   # 4 bytes of padding after header

    def test_roundtrip_every_version(self):
        """Fill every scalar/array field with a distinct value and decode it back."""
        for ver, sname in D.VERSIONS.items():
            if sname == "gpu_metrics_v1_9":
                continue
            size, fields = D.STRUCTS[sname]
            buf = bytearray(b"\xff" * size)
            struct.pack_into("<HBB", buf, 0, size, ver[0], ver[1])
            expect = {}
            n = 1
            for name, off, kind, count in fields:
                if name == "common_header" or name.startswith("padding") or ":" in kind:
                    continue
                w = struct.calcsize(kind)
                vals = []
                for i in range(count):
                    v = (n * 7 + i) % (1 << (8 * w) - 1) if w > 1 else (n + i) % 250
                    struct.pack_into("<" + kind, buf, off + i * w, v)
                    vals.append(v)
                n += 1
                expect[name] = vals[0] if count == 1 else vals
            p = GM.parse_gpu_metrics(bytes(buf))
            self.assertTrue(p["exact"], sname)
            self.assertEqual(p["struct"], sname)
            for name, v in expect.items():
                self.assertEqual(p["fields"][name], v, f"{sname}.{name}")

    def test_unpopulated_fields_are_none(self):
        p = GM.parse_gpu_metrics(fx.gpu_metrics_v1_3_blob())
        f = p["fields"]
        self.assertEqual(p["version"], (1, 3))
        self.assertIsNone(f["gfx_activity_acc"])
        self.assertIsNone(f["firmware_timestamp"])
        self.assertEqual(f["temperature_hbm"], [None] * 4)
        self.assertEqual(f["temperature_hotspot"], 78)
        self.assertEqual(f["voltage_gfx"], 1050)
        self.assertEqual(f["pcie_link_speed"], 320)
        self.assertEqual(f["indep_throttle_status"], 1)

    def test_attr_encoded_v1_9(self):
        def enc(unit, typ, aid, inst):
            return (unit << 24) | (typ << 20) | (aid << 10) | inst
        hot = D.ATTR_IDS.index("TEMPERATURE_HOTSPOT")
        gfx = D.ATTR_IDS.index("CURRENT_GFXCLK")
        body = struct.pack("<QH", enc(2, 2, hot, 1), 71)
        body += struct.pack("<Q4H", enc(1, 2, gfx, 4), 2100, 2101, 0xFFFF, 2103)
        blob = struct.pack("<HBBi", 8 + len(body), 1, 9, 2) + body
        p = GM.parse_gpu_metrics(blob)
        self.assertEqual(p["struct"], "gpu_metrics_v1_9")
        self.assertEqual(p["fields"]["temperature_hotspot"], 71)
        self.assertEqual(p["fields"]["current_gfxclk"], [2100, 2101, None, 2103])
        rows = {r["key"]: r for r in GM.metric_rows(p)}
        self.assertEqual(rows["temperature_hotspot"]["unit"], "°C")
        self.assertEqual(rows["current_gfxclk.3"]["value"], 2103)

    def test_unknown_revision_falls_back(self):
        size = D.STRUCTS["gpu_metrics_v2_4"][0] + 16
        blob = bytearray(b"\xff" * size)
        struct.pack_into("<HBBH", blob, 0, size, 2, 9, 4321)
        p = GM.parse_gpu_metrics(bytes(blob))
        self.assertFalse(p["exact"])
        self.assertEqual(p["struct"], "gpu_metrics_v2_4")
        self.assertEqual(p["fields"]["temperature_gfx"], 4321)

    def test_garbage(self):
        self.assertIsNone(GM.parse_gpu_metrics(b""))
        self.assertIsNone(GM.parse_gpu_metrics(b"\x01\x02"))
        self.assertIsNone(GM.parse_gpu_metrics(b"\xff" * 64))
        p = GM.parse_gpu_metrics(struct.pack("<HBB", 120, 1, 3) + b"\x00" * 10)  # truncated
        self.assertIsNotNone(p)
        self.assertIsNone(p["fields"]["indep_throttle_status"])

    def test_apu_scaling_v2(self):
        size = D.STRUCTS["gpu_metrics_v2_1"][0]
        blob = bytearray(b"\xff" * size)
        offs = {n: o for n, o, _k, _c in D.STRUCTS["gpu_metrics_v2_1"][1]}
        struct.pack_into("<HBB", blob, 0, size, 2, 1)
        struct.pack_into("<H", blob, offs["temperature_gfx"], 4550)        # centi-degC
        struct.pack_into("<H", blob, offs["average_gfx_activity"], 2500)   # centi-%
        struct.pack_into("<H", blob, offs["average_socket_power"], 15250)  # mW
        rows = {r["key"]: r for r in GM.metric_rows(GM.parse_gpu_metrics(bytes(blob)))}
        self.assertAlmostEqual(rows["temperature_gfx"]["value"], 45.5)
        self.assertAlmostEqual(rows["average_gfx_activity"]["value"], 25.0)
        self.assertAlmostEqual(rows["average_socket_power"]["value"], 15.25)

    def test_throttle_decode(self):
        v = (1 << 0) | (1 << 36) | (1 << 57)
        self.assertEqual(GM.decode_indep_throttle(v), ["PPT0", "TEMP_HOTSPOT", "FIT"])
        self.assertIsNone(GM.decode_indep_throttle(None))


# ------------------------------------------------------------------------------------
class TestTextParsers(unittest.TestCase):
    def test_rdna4_od(self):
        od = OD.parse_od_text(fx.pp_od_clk_voltage(sclk_offset=-30, vo=-45))
        self.assertEqual(od["values"]["OD_SCLK_OFFSET"], -30)
        self.assertEqual(od["values"]["OD_VDDGFX_OFFSET"], -45)
        self.assertEqual(od["entries"]["OD_MCLK"], {0: (97, None), 1: (1258, None)})
        self.assertEqual(od["ranges"], {"SCLK_OFFSET": (-500, 1000), "MCLK": (97, 1500),
                                        "VDDGFX_OFFSET": (-200, 0)})

    def test_rdna3_od(self):
        txt = ("OD_SCLK:\n0: 500Mhz\n1: 2500Mhz\nOD_MCLK:\n0: 97Mhz\n1: 1249MHz\n"
               "OD_RANGE:\nSCLK:     500Mhz       3500Mhz\nMCLK:      97Mhz       1500Mhz\n"
               "VDDGFX_OFFSET:    -450mv          0mv\nOD_VDDGFX_OFFSET:\n0mV\n")
        params, notes = OD.build_clock_params(OD.parse_od_text(txt))
        by = {p.key: p for p in params}
        self.assertEqual(sorted(by), ["mclk_max", "mclk_min", "sclk_max", "sclk_min", "vddgfx_offset"])
        self.assertEqual(by["sclk_max"].command(2700), "s 1 2700")
        self.assertEqual(by["mclk_min"].command(200), "m 0 200")
        self.assertEqual((by["vddgfx_offset"].min, by["vddgfx_offset"].max), (-450, 0))
        self.assertEqual(notes, [])

    def test_rdna2_vo_without_range_gets_conservative_bound(self):
        txt = ("OD_SCLK:\n0: 500Mhz\n1: 2650Mhz\nOD_MCLK:\n0: 97Mhz\n1: 1000MHz\n"
               "OD_RANGE:\nSCLK:     500Mhz       4000Mhz\nMCLK:     674Mhz       1075Mhz\n"
               "OD_VDDGFX_OFFSET:\n0mV\n")
        by = {p.key: p for p in OD.build_clock_params(OD.parse_od_text(txt))[0]}
        self.assertEqual((by["vddgfx_offset"].min, by["vddgfx_offset"].max), OD.RDNA2_VO_FALLBACK)

    def test_rdna1_vddc_curve(self):
        txt = ("OD_SCLK:\n0: 800Mhz\n1: 2100Mhz\nOD_MCLK:\n1: 875MHz\nOD_VDDC_CURVE:\n"
               "0: 800MHz 711mV\n1: 1450MHz 801mV\n2: 2100MHz 1200mV\nOD_RANGE:\n"
               "SCLK:     800Mhz       2150Mhz\nMCLK:     625Mhz        950Mhz\n"
               "VDDC_CURVE_SCLK[0]:     800Mhz       2150Mhz\nVDDC_CURVE_VOLT[0]:     750mV        1200mV\n"
               "VDDC_CURVE_SCLK[1]:     800Mhz       2150Mhz\nVDDC_CURVE_VOLT[1]:     750mV        1200mV\n"
               "VDDC_CURVE_SCLK[2]:     800Mhz       2150Mhz\nVDDC_CURVE_VOLT[2]:     750mV        1200mV\n")
        by = {p.key: p for p in OD.build_clock_params(OD.parse_od_text(txt))[0]}
        self.assertIn("mclk_max", by)
        self.assertNotIn("mclk_min", by)
        self.assertEqual(by["vc1_sclk"].command(1500), "vc 1 1500 801")
        self.assertEqual(by["vc1_volt"].command(780), "vc 1 1450 780")
        self.assertEqual((by["vc2_volt"].min, by["vc2_volt"].max), (750, 1200))

    def test_legacy_table_read_only(self):
        txt = ("OD_SCLK:\n0:        852Mhz        800mV\n1:       1138Mhz        900mV\n"
               "OD_MCLK:\n0:        167Mhz        800mV\nOD_RANGE:\nSCLK:     852MHz       2400MHz\n"
               "MCLK:     167MHz       1500MHz\nVDDC:     800mV        1200mV\n")
        params, notes = OD.build_clock_params(OD.parse_od_text(txt))
        self.assertEqual(params, [])
        self.assertTrue(notes and "legacy" in notes[0])

    def test_apu_renoir_format(self):
        txt = "OD_SCLK\n0:       200Mhz\n1:      1750Mhz\nOD_RANGE\nSCLK:        200Mhz       1750Mhz\n"
        by = {p.key: p for p in OD.build_clock_params(OD.parse_od_text(txt))[0]}
        self.assertEqual((by["sclk_max"].value, by["sclk_max"].min, by["sclk_max"].max), (1750, 200, 1750))

    def test_pp_dpm(self):
        self.assertEqual(A.parse_pp_dpm("0: 500Mhz \n1: 1287Mhz *\n2: 2970Mhz \n"),
                         ([500.0, 1287.0, 2970.0], 1, 1287.0))
        self.assertEqual(A.parse_pp_dpm("S: 25Mhz *\n0: 500Mhz \n1: 1690Mhz \n"),
                         ([500.0, 1690.0], None, 25.0))
        self.assertEqual(A.parse_pp_dpm(""), ([], None, None))
        self.assertEqual(A.parse_pp_dpm_pcie("0: 2.5GT/s, x1 619Mhz \n1: 16.0GT/s, x16 619Mhz *\n"),
                         (16.0, 16))

    def test_profiles(self):
        prof, active = A.parse_power_profiles(fx.pp_power_profile_mode(5))
        self.assertEqual(prof["COMPUTE"], 5)
        self.assertEqual(prof["WINDOW_3D"], 7)
        self.assertEqual(active, "COMPUTE")
        # APU format ("%2d %14s%s\n", no colon) and vega10 powerplay format
        prof, active = A.parse_power_profiles(" 0 BOOTUP_DEFAULT \n 1 3D_FULL_SCREEN*\n 2 POWER_SAVING \n")
        self.assertEqual((prof, active), ({"BOOTUP_DEFAULT": 0, "3D_FULL_SCREEN": 1, "POWER_SAVING": 2},
                                          "3D_FULL_SCREEN"))
        prof, active = A.parse_power_profiles(
            "NUM        MODE_NAME BUSY_SET_POINT FPS USE_RLC_BUSY MIN_ACTIVE_LEVEL\n"
            "  0 BOOTUP_DEFAULT :             70      60          0              0\n"
            "  1 3D_FULL_SCREEN *:             70      60          1              3\n")
        self.assertEqual(active, "3D_FULL_SCREEN")


# ------------------------------------------------------------------------------------
class TestRx9070xtSensors(TreeCase):
    def test_detect_identity(self):
        be = self.be
        self.assertEqual(be.slot, "0000:03:00.0")
        self.assertEqual(be.device_id, 0x7550)
        self.assertEqual(be.gc, (12, 0, 1))
        self.assertEqual(be.mp1, (14, 0, 3))
        self.assertEqual(be.arch, "RDNA4")
        self.assertEqual(be.metrics_version, (1, 3))
        self.assertFalse(be.is_apu)
        self.assertNotIn("/", be.name)

    def test_env_root_and_non_amd_ignored(self):
        other = os.path.join(self.root, "sys/class/drm/card1/device")
        os.makedirs(other)
        with open(os.path.join(other, "vendor"), "w") as fh:
            fh.write("0x10de\n")
        old = os.environ.get(A.SYSFS_ROOT_ENV)
        os.environ[A.SYSFS_ROOT_ENV] = self.root
        try:
            devs = A.AmdGpuBackend.detect()
        finally:
            if old is None:
                del os.environ[A.SYSFS_ROOT_ENV]
            else:
                os.environ[A.SYSFS_ROOT_ENV] = old
        self.assertEqual([d.slot for d in devs], ["0000:03:00.0"])

    def test_detect_empty_root(self):
        self.assertEqual(A.AmdGpuBackend.detect(os.path.join(self.tmp, "nothing")), [])

    def test_sensor_values(self):
        ss = {s.id: s for s in self.be.sensors()}
        for s in ss.values():
            self.assertIsInstance(s, Sensor)
            self.assertTrue(s.group.startswith(self.be.name + "/"), s.group)
            self.assertEqual(s.group.count("/"), 1)
        v = lambda i: ss["amdgpu0." + i].value  # noqa: E731
        self.assertEqual(v("hwmon.temp2"), 78.0)
        self.assertEqual(ss["amdgpu0.hwmon.temp2"].label, "Junction (hotspot)")
        self.assertAlmostEqual(v("hwmon.power1_average"), 287.0)
        self.assertAlmostEqual(v("hwmon.power1_cap"), 304.0)
        self.assertAlmostEqual(v("hwmon.in0"), 1.05)
        self.assertAlmostEqual(v("hwmon.freq2"), 1258.0)
        self.assertAlmostEqual(v("hwmon.pwm1"), 120 * 100 / 255)
        self.assertEqual(v("dev.gpu_busy_percent"), 97)
        self.assertEqual(v("dev.mem_busy_percent"), 45)
        self.assertAlmostEqual(v("mem.vram_total"), 16368.0)
        self.assertEqual(v("dpm.sclk"), 2950)
        self.assertEqual(v("dpm.vclk"), 25)
        self.assertEqual(v("dpm.pcie_speed"), 32.0)
        self.assertEqual(v("pcie.current_link_width"), 16)
        # gpu_metrics v1_3
        self.assertEqual(v("gm.temperature_hotspot"), 78)
        self.assertEqual(v("gm.temperature_vrmem"), 56)
        self.assertEqual(v("gm.average_umc_activity"), 45)
        self.assertEqual(v("gm.average_socket_power"), 287)
        self.assertEqual(v("gm.current_uclk"), 1258)
        self.assertAlmostEqual(v("gm.voltage_mem"), 1.35)
        self.assertEqual(v("gm.current_fan_speed"), 1650)
        self.assertEqual(v("gm.pcie_link_speed"), 32.0)
        self.assertEqual(v("gm.indep_throttle_status.count"), 1)
        self.assertEqual(v("gm.indep_throttle_status.power"), 1)
        self.assertEqual(v("gm.indep_throttle_status.thermal"), 0)
        self.assertIn("PPT0", ss["amdgpu0.gm.indep_throttle_status.count"].hint)
        # unpopulated (0xFF) fields are not shown at all
        self.assertNotIn("amdgpu0.gm.temperature_hbm.0", ss)
        self.assertNotIn("amdgpu0.gm.firmware_timestamp", ss)

    def test_rows_kept_when_unavailable(self):
        ids1 = [s.id for s in self.be.sensors()]
        dev = os.path.join(self.root, DEV.lstrip("/"))
        os.remove(os.path.join(dev, "gpu_metrics"))
        with open(os.path.join(dev, "hwmon/hwmon3/temp2_input"), "w") as fh:
            fh.write("")
        ss = self.be.sensors()
        self.assertEqual([s.id for s in ss], ids1)
        by = {s.id: s for s in ss}
        self.assertIsNone(by["amdgpu0.gm.temperature_hotspot"].value)
        self.assertIsNone(by["amdgpu0.hwmon.temp2"].value)

    def test_sensors_are_read_only(self):
        self.be.sensors()
        self.be.controls()
        self.assertEqual(self.rec.calls, [])
        self.assertEqual(self.be.io.log, [])


# ------------------------------------------------------------------------------------
class TestRx9070xtOverclock(TreeCase):
    OD_FILE = DEV + "/pp_od_clk_voltage"

    def test_control_set(self):
        cs = {c.id: c for c in self.be.controls()}
        for suffix in ("power_cap", "perf_level", "power_profile", "od.sclk_offset", "od.mclk_min",
                       "od.mclk_max", "od.vddgfx_offset", "od.fan_curve0_temp", "od.fan_curve4_pwm",
                       "od.fan_target_temp", "od.fan_zero_rpm", "od.fan_acoustic_limit", "od_reset"):
            c = cs["amdgpu0.oc." + suffix]
            self.assertTrue(c.available, suffix)
        c = cs["amdgpu0.oc.od.sclk_offset"]
        self.assertEqual((c.value, c.min, c.max, c.default), (0, -500, 1000, 0))
        c = cs["amdgpu0.oc.od.vddgfx_offset"]
        self.assertEqual((c.min, c.max, c.danger), (-200, 0, 2))
        c = cs["amdgpu0.oc.power_cap"]
        self.assertEqual((c.value, c.min, c.max, c.default), (304.0, 231.0, 334.0, 304.0))
        self.assertEqual(cs["amdgpu0.oc.power_profile"].value, "3D_FULL_SCREEN")
        self.assertNotIn("CUSTOM", cs["amdgpu0.oc.power_profile"].choices)
        self.assertEqual(cs["amdgpu0.oc.od.fan_zero_rpm"].kind, "bool")
        self.assertIs(cs["amdgpu0.oc.od.fan_zero_rpm"].value, True)

    def test_sclk_offset_exact_writes(self):
        ok, msg = self.be.apply("amdgpu0.oc.od.sclk_offset", 150)
        self.assertTrue(ok, msg)
        self.assertEqual(self.writes(), [(self.OD_FILE, "s 150\n"), (self.OD_FILE, "c\n")])

    def test_negative_offset_and_float_integral(self):
        ok, _ = self.be.apply("amdgpu0.oc.od.sclk_offset", -500.0)
        self.assertTrue(ok)
        self.assertEqual(self.writes()[0], (self.OD_FILE, "s -500\n"))

    def test_mclk_and_vo_exact_writes(self):
        self.assertTrue(self.be.apply("amdgpu0.oc.od.mclk_max", 1300)[0])
        self.assertTrue(self.be.apply("amdgpu0.oc.od.vddgfx_offset", -75)[0])
        self.assertTrue(self.be.apply("amdgpu0.oc.od.mclk_min", 500)[0])
        self.assertEqual(self.writes(), [(self.OD_FILE, "m 1 1300\n"), (self.OD_FILE, "c\n"),
                                         (self.OD_FILE, "vo -75\n"), (self.OD_FILE, "c\n"),
                                         (self.OD_FILE, "m 0 500\n"), (self.OD_FILE, "c\n")])

    def test_out_of_range_refused(self):
        cases = [("od.sclk_offset", 1001), ("od.sclk_offset", -501), ("od.vddgfx_offset", 5),
                 ("od.vddgfx_offset", -201), ("od.mclk_max", 1501), ("od.mclk_min", 96),
                 ("od.mclk_min", 1300),          # above current max 1258
                 ("power_cap", 335), ("power_cap", 230), ("od.fan_target_temp", 111),
                 ("od.fan_curve2_pwm", 14), ("od.fan_acoustic_limit", 3301)]
        for suffix, val in cases:
            ok, msg = self.be.apply("amdgpu0.oc." + suffix, val)
            self.assertFalse(ok, f"{suffix}={val} accepted: {msg}")
        self.assertEqual(self.rec.calls, [])

    def test_bad_types_refused(self):
        for suffix, val in [("od.sclk_offset", 10.5), ("od.sclk_offset", "abc"),
                            ("od.sclk_offset", True), ("od.sclk_offset", None),
                            ("od.sclk_offset", float("nan")), ("power_cap", 300.5),
                            ("perf_level", "turbo"), ("perf_level", 3),
                            ("power_profile", "CUSTOM"), ("od.fan_zero_rpm", 2),
                            ("nonexistent", 1)]:
            ok, msg = self.be.apply("amdgpu0.oc." + suffix, val)
            self.assertFalse(ok, f"{suffix}={val!r} accepted: {msg}")
        self.assertEqual(self.rec.calls, [])

    def test_power_cap_exact_write(self):
        ok, msg = self.be.apply("amdgpu0.oc.power_cap", 334)
        self.assertTrue(ok, msg)
        self.assertEqual(self.writes(), [(DEV + "/hwmon/hwmon3/power1_cap", "334000000\n")])

    def test_perf_level_and_profile_writes(self):
        self.assertTrue(self.be.apply("amdgpu0.oc.perf_level", "manual")[0])
        self.assertTrue(self.be.apply("amdgpu0.oc.power_profile", "COMPUTE")[0])
        self.assertEqual(self.writes(), [(DEV + "/power_dpm_force_performance_level", "manual\n"),
                                         (DEV + "/pp_power_profile_mode", "5\n")])

    def test_fan_writes(self):
        fc = DEV + "/gpu_od/fan_ctrl/"
        self.assertTrue(self.be.apply("amdgpu0.oc.od.fan_curve2_temp", 72)[0])
        self.assertTrue(self.be.apply("amdgpu0.oc.od.fan_curve2_pwm", 60)[0])
        self.assertTrue(self.be.apply("amdgpu0.oc.od.fan_zero_rpm", False)[0])
        self.assertTrue(self.be.apply("amdgpu0.oc.od.fan_target_temp", 80)[0])
        self.assertEqual(self.writes(), [
            (fc + "fan_curve", "2 72 50\n"), (fc + "fan_curve", "c\n"),
            (fc + "fan_curve", "2 70 60\n"), (fc + "fan_curve", "c\n"),
            (fc + "fan_zero_rpm_enable", "0\n"), (fc + "fan_zero_rpm_enable", "c\n"),
            (fc + "fan_target_temperature", "80\n"), (fc + "fan_target_temperature", "c\n")])

    def test_fan_point_with_invalid_companion_refused(self):
        p = os.path.join(self.root, DEV.lstrip("/"), "gpu_od/fan_ctrl/fan_curve")
        with open(p, "w") as fh:   # boot table all zeros, as some RDNA3 boards report
            fh.write("OD_FAN_CURVE:\n" + "".join(f"{i}: 0C 0%\n" for i in range(5)) +
                     "OD_RANGE:\nFAN_CURVE(hotspot temp): 25C 110C\nFAN_CURVE(fan speed): 15% 100%\n")
        ok, msg = self.be.apply("amdgpu0.oc.od.fan_curve0_temp", 50)
        self.assertFalse(ok)
        self.assertIn("speed", msg)
        self.assertEqual(self.rec.calls, [])

    def test_reset(self):
        ok, _ = self.be.apply("amdgpu0.oc.od_reset", None)
        self.assertTrue(ok)
        self.assertEqual(self.writes(), [(self.OD_FILE, "r\n"), (self.OD_FILE, "c\n")])

    def test_driver_rejection_reported(self):
        def failing(path, data):
            raise OSError(22, "Invalid argument")
        self.be.io.writer = failing
        ok, msg = self.be.apply("amdgpu0.oc.od.sclk_offset", 100)
        self.assertFalse(ok)
        self.assertIn("Invalid argument", msg)

    def test_default_writer_writes_inside_root_only(self):
        self.be.io.writer = A._default_writer
        ok, msg = self.be.apply("amdgpu0.oc.perf_level", "high")
        self.assertTrue(ok, msg)
        with open(os.path.join(self.root, DEV.lstrip("/"), "power_dpm_force_performance_level")) as fh:
            self.assertEqual(fh.read(), "high\n")
        outside = os.path.join(self.tmp, "outside")
        open(outside, "w").close()
        with self.assertRaises(PermissionError):
            self.be.io.write(outside, "x")

    def test_dry_run(self):
        os.environ[A.DRY_RUN_ENV] = "1"
        try:
            be = A.AmdGpuBackend.detect(self.root)[0]
        finally:
            del os.environ[A.DRY_RUN_ENV]
        be.io.writer = self.rec
        self.assertTrue(be.apply("amdgpu0.oc.od.sclk_offset", 50)[0])
        self.assertEqual(self.rec.calls, [])
        self.assertEqual(be.io.log, [(self.OD_FILE, "s 50\n"), (self.OD_FILE, "c\n")])

    def test_readonly_user(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores file modes")
        os.chmod(os.path.join(self.root, DEV.lstrip("/"), "pp_od_clk_voltage"), 0o444)
        c = self.ctl("od.sclk_offset")
        self.assertFalse(c.available)
        self.assertIn("root", c.hint)
        self.assertFalse(self.be.apply("amdgpu0.oc.od.sclk_offset", 10)[0])


class TestRx9070xtNoOverdrive(TreeCase):
    od = False

    def test_od_controls_greyed_with_kernel_param_hint(self):
        cs = {c.id: c for c in self.be.controls()}
        for suffix in ("od.sclk_offset", "od.mclk_max", "od.vddgfx_offset", "od.fan_target_temp",
                       "od_reset"):
            c = cs["amdgpu0.oc." + suffix]
            self.assertFalse(c.available, suffix)
            self.assertIn("amdgpu.ppfeaturemask=0xfff7ffff", c.hint)
        self.assertTrue(cs["amdgpu0.oc.power_cap"].available)
        self.assertEqual(cs["amdgpu0.oc.power_cap"].max, 317.0)

    def test_apply_refused(self):
        ok, msg = self.be.apply("amdgpu0.oc.od.sclk_offset", 100)
        self.assertFalse(ok)
        self.assertIn("ppfeaturemask", msg)
        self.assertEqual(self.rec.calls, [])


# ------------------------------------------------------------------------------------
class TestRaphaelLive(unittest.TestCase):
    """Read-only snapshot captured from a real Radeon iGPU (Granite Ridge, GC 10.3.6,
    MP1 13.0.5, kernel 7.3.0-cachyos) -- gpu_metrics v2_1."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meat3-amdgpu-")
        self.root = os.path.join(self.tmp, "root")
        shutil.copytree(os.path.join(FIX, "raphael_live"), self.root)
        self.be = A.AmdGpuBackend.detect(self.root)[0]
        self.rec = Recorder()
        self.be.io.writer = self.rec

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_identity_and_metrics(self):
        be = self.be
        self.assertEqual((be.device_id, be.gc, be.mp1, be.metrics_version, be.is_apu),
                         (0x13C0, (10, 3, 6), (13, 0, 5), (2, 1), True))
        with open(os.path.join(self.root, "sys/class/drm/card1/device/gpu_metrics"), "rb") as fh:
            raw = fh.read()
        t_gfx = struct.unpack_from("<H", raw, 4)[0] / 100.0
        ss = {s.id: s for s in be.sensors()}
        self.assertAlmostEqual(ss["amdgpu0.gm.temperature_gfx"].value, t_gfx)
        self.assertTrue(10 < t_gfx < 110)
        self.assertEqual(ss["amdgpu0.gm.average_gfxclk_frequency"].value, 600)
        self.assertEqual(ss["amdgpu0.hwmon.temp1"].label, "Edge")
        self.assertNotIn("amdgpu0.gm.current_gfxclk", ss)   # 0xFFFF on SMU 13.0.5

    def test_apu_od_needs_manual(self):
        cs = {c.id: c for c in self.be.controls()}
        c = cs["amdgpu0.oc.od.sclk_max"]
        self.assertEqual((c.value, c.min, c.max), (2200, 600, 2200))
        ok, msg = self.be.apply("amdgpu0.oc.od.sclk_max", 2000)
        self.assertFalse(ok)
        self.assertIn("manual", msg)
        with open(os.path.join(self.root, "sys/class/drm/card1/device/power_dpm_force_performance_level"), "w") as fh:
            fh.write("manual\n")
        self.assertFalse(self.be.apply("amdgpu0.oc.od.sclk_max", 2300)[0])   # > OD_RANGE
        self.assertFalse(self.be.apply("amdgpu0.oc.od.sclk_min", 2201)[0])   # > OD_RANGE
        ok, msg = self.be.apply("amdgpu0.oc.od.sclk_max", 2000)
        self.assertTrue(ok, msg)
        od = "/sys/class/drm/card1/device/pp_od_clk_voltage"
        self.assertEqual([(self.be.io.rel(p), d) for p, d in self.rec.calls],
                         [(od, "s 1 2000\n"), (od, "c\n")])


# ------------------------------------------------------------------------------------
class TestRdna4Extras(TreeCase):
    """SMU14 pp_table limits, real per-field OD ranges, SMU14 throttle names, hwmon limits."""

    def test_full_metrics_absent_then_present(self):
        from meat3.backends import amdgpu_smu14_full as F
        ss = {s.id: s for s in self.be.sensors()}
        self.assertIn("load the meat3_smu module", ss["amdgpu0.fm.status"].label)
        self.assertNotIn("amdgpu0.fm.pwr.tbp", ss)
        thr = [0] * 21
        thr[14] = 37                                          # PPT0
        tbl = F.build({"AverageTotalBoardPower": 331, "AverageSocketPower": 290,
                       "AverageGfxclkFrequencyTarget": 3010, "AvgVoltage": (1050, 750, 850, 1350),
                       "AvgCurrent": (24000, 3000, 500, 1200), "AvgTemperature": (60, 78, 80, 70) + (0,) * 8,
                       "ThrottlingPercentage": thr, "VmaxThrottlingPercentage": 12, "serial": (0xBEEF, 0x12)})
        d = os.path.join(self.root, "sys/kernel/debug/meat3_smu")
        os.makedirs(d)
        with open(os.path.join(d, "gpu0"), "wb") as f:
            f.write(F.file_blob(tbl, seq=5))
        ss = {s.id: s for s in self.be.sensors()}
        self.assertIn("serial 000000120000BEEF", ss["amdgpu0.fm.status"].label)
        self.assertEqual(ss["amdgpu0.fm.pwr.tbp"].value, 331)
        self.assertEqual(ss["amdgpu0.fm.a.vdd_gfx"].value, 24)
        self.assertAlmostEqual(ss["amdgpu0.fm.pwr.vdd_gfx"].value, 25.2)
        self.assertAlmostEqual(ss["amdgpu0.fm.v.vddci_mem"].value, 0.85)
        self.assertEqual(ss["amdgpu0.fm.t.hotspot_gfx"].value, 80)
        self.assertNotIn("amdgpu0.fm.t.liquid_0", ss)                 # zero = sensor not fitted
        self.assertEqual(ss["amdgpu0.fm.thr.ppt0"].value, 37)
        self.assertEqual(ss["amdgpu0.fm.thr.vmax"].value, 12)
        self.assertEqual(ss["amdgpu0.fm.clk.gfx_target"].value, 3010)

    def test_real_asus_9070xt_pptable(self):
        """Board pptable from a real ASUS RX 9070 XT VBIOS (115-G295BP00-100, 12/04/24; the copy at
        flash offset 0xf4320), not a synthesized blob."""
        from meat3.backends import amdgpu_pptable as P
        with open(os.path.join(os.path.dirname(__file__), "fixtures/amdgpu/pp_table_asus_9070xt.bin"), "rb") as f:
            pt = P.parse(f.read())
        self.assertEqual(pt["structuresize"], 5812)
        self.assertEqual(pt["clocks"], {"base": 1740, "game": 2460, "boost": 3010})
        self.assertEqual(pt["socket_pl_ac_w"][0], 317)
        self.assertEqual(pt["msg_ppt0_ac_w"], 340)
        self.assertEqual((pt["od_min"]["Ppt"], pt["od_max"]["Ppt"]), (-30, 10))
        self.assertEqual(P.od_param_ranges(pt), {"sclk_offset": (-500, 1000), "mclk_min": (97, 1500),
                                                 "mclk_max": (97, 1500), "vddgfx_offset": (-200, 0)})
        self.assertEqual((pt["temp_limits"]["Hotspot"], pt["fw_ctf_limits"]["Hotspot"]), (110, 118))
        self.assertIn("Power limit (PPT %)", pt["od_features"])

    def test_pptable_roundtrip(self):
        from meat3.backends import amdgpu_pptable as P
        blob = P.build_blob({"clocks": (2400, 2450, 3100), "msg_ppt0_ac_w": 340,
                             "od_max": {"Ppt": 15, "UclkFmax": 1400}})
        pt = P.parse(blob)
        self.assertEqual(pt["clocks"], {"base": 2400, "game": 2450, "boost": 3100})
        self.assertEqual(pt["msg_ppt0_ac_w"], 340)
        self.assertEqual(pt["od_max"]["Ppt"], 15)
        self.assertEqual(P.od_param_ranges(pt)["mclk_max"], (1258, 1400))
        self.assertEqual(pt["temp_limits"]["Hotspot"], 110)
        self.assertIn("Memory clock", pt["od_features"])
        self.assertIsNone(P.parse(blob[:-1]))          # wrong size = not an SMU14 table
        self.assertIsNone(P.parse(None))

    def test_board_limit_sensors(self):
        ss = {s.id: s for s in self.be.sensors()}
        v = lambda i: ss["amdgpu0.limits." + i].value  # noqa: E731
        self.assertEqual(v("clk_boost"), 2970.0)
        self.assertEqual(v("ppt0"), 304.0)
        self.assertEqual(v("od_ppt_max"), 10.0)
        self.assertEqual(v("temp_hotspot"), 110.0)
        self.assertEqual(v("ctf_hotspot"), 115.0)
        self.assertAlmostEqual(v("vmax_gfx"), 1.15)
        self.assertNotIn("amdgpu0.limits.temp_liquid_0", ss)       # 0 = not used on this board
        self.assertEqual(ss["amdgpu0.hwmon.temp2_crit"].value, 110.0)
        self.assertIn("Board limits", ss["amdgpu0.limits.ppt0"].group)

    def test_mclk_real_ranges(self):
        cs = {c.id: c for c in self.be.controls()}
        self.assertEqual((cs["amdgpu0.oc.od.mclk_min"].min, cs["amdgpu0.oc.od.mclk_min"].max), (97, 1258))
        self.assertEqual((cs["amdgpu0.oc.od.mclk_max"].min, cs["amdgpu0.oc.od.mclk_max"].max), (1258, 1500))
        self.assertIn("pp_table range", cs["amdgpu0.oc.od.mclk_max"].hint)
        self.assertIn("message limit 304 W", cs["amdgpu0.oc.power_cap"].hint)
        ok, msg = self.be.apply("amdgpu0.oc.od.mclk_max", 1200)    # inside OD_RANGE union, outside real
        self.assertFalse(ok, msg)
        self.assertEqual(self.rec.calls, [])

    def test_smu14_throttle_names(self):
        from meat3.backends import amdgpu_pptable as P
        self.assertEqual(P.decode_smu14_throttle((1 << 2) | (1 << 14) | (1 << 20)),
                         ["TEMP_HOTSPOT_GFX", "PPT0", "GFX_DVO"])
        ss = {s.id: s for s in self.be.sensors()}
        self.assertIn("amdgpu0.smu14.throttle.count", ss)
        for k in ("power", "current", "thermal", "other"):
            self.assertIn(f"amdgpu0.smu14.throttle.{k}", ss)

    def test_power_cap_readback_reported(self):
        def writer(path, data):
            self.rec(path, data)
            with open(path, "w") as fh:        # driver rounds 330 W down to a whole OD % step
                fh.write("327000000\n")
        self.be.io.writer = writer
        ok, msg = self.be.apply("amdgpu0.oc.power_cap", 330)
        self.assertTrue(ok, msg)
        self.assertIn("applied 327 W", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
