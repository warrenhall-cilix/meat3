#!/usr/bin/env python3
"""Build sysfs fixture trees for an AMD Radeon RX 9070 XT (Navi 48, GC 12.0.1 / gfx1201,
MP1/SMU 14.0.3) as the upstream amdgpu driver presents it.

    python3 tests/fixtures/amdgpu/make_fixtures.py      # (re)writes rx9070xt*/ next to this file

Text files reproduce the kernel's printf formats exactly (smu_v14_0_2_ppt.c,
smu_cmn.c, amdgpu_pm.c). Numeric values are representative of a 9070 XT under a gaming
load, not captured from a real card. The gpu_metrics blob is built with struct.pack
straight from `struct gpu_metrics_v1_3` in kgd_pp_interface.h (independent of the
backend's generated layout table), with unpopulated fields left at 0xFF like
smu_cmn_init_soft_gpu_metrics() leaves them.
"""
from __future__ import annotations

import os
import shutil
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SLOT = "0000:03:00.0"
U16, U32, U64 = 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF

# struct gpu_metrics_v1_3, natural alignment works out with no implicit padding:
GM_V1_3_FMT = "<HBB" "6H" "3H" "H" "Q" "Q" "7H" "7H" "I" "H" "H" "H" "H" "I" "I" "4H" "Q" "3H" "H" "Q"

GM_VALUES = dict(
    temperature_edge=62, temperature_hotspot=78, temperature_mem=74,
    temperature_vrgfx=58, temperature_vrsoc=50, temperature_vrmem=56,
    average_gfx_activity=97, average_umc_activity=45, average_mm_activity=0,
    average_socket_power=287,
    energy_accumulator=0x0000_0012_3456_0000,   # 14.0.3 with PMFW >= 0x00685000 only
    system_clock_counter=123_456_789_000,
    average_gfxclk_frequency=2950, average_socclk_frequency=1200, average_uclk_frequency=1258,
    average_vclk0_frequency=0, average_dclk0_frequency=0,
    average_vclk1_frequency=0, average_dclk1_frequency=0,
    current_gfxclk=2950, current_socclk=1200, current_uclk=1258,
    current_vclk0=25, current_dclk0=25, current_vclk1=25, current_dclk1=25,
    throttle_status=0x00000001,                 # ASIC THROTTLER_PPT0_BIT
    current_fan_speed=1650, pcie_link_width=16, pcie_link_speed=320,
    voltage_soc=750, voltage_gfx=1050, voltage_mem=1350,
    indep_throttle_status=1 << 0,                # SMU_THROTTLER_PPT0_BIT
)


def gpu_metrics_v1_3_blob(v: dict = GM_VALUES) -> bytes:
    blob = struct.pack(
        GM_V1_3_FMT, 120, 1, 3,
        v["temperature_edge"], v["temperature_hotspot"], v["temperature_mem"],
        v["temperature_vrgfx"], v["temperature_vrsoc"], v["temperature_vrmem"],
        v["average_gfx_activity"], v["average_umc_activity"], v["average_mm_activity"],
        v["average_socket_power"], v["energy_accumulator"], v["system_clock_counter"],
        v["average_gfxclk_frequency"], v["average_socclk_frequency"], v["average_uclk_frequency"],
        v["average_vclk0_frequency"], v["average_dclk0_frequency"],
        v["average_vclk1_frequency"], v["average_dclk1_frequency"],
        v["current_gfxclk"], v["current_socclk"], v["current_uclk"], v["current_vclk0"],
        v["current_dclk0"], v["current_vclk1"], v["current_dclk1"],
        v["throttle_status"], v["current_fan_speed"], v["pcie_link_width"], v["pcie_link_speed"],
        U16,                      # padding
        U32, U32,                 # gfx_activity_acc, mem_activity_acc (not filled by 14.0.2)
        U16, U16, U16, U16,       # temperature_hbm[4]
        U64,                      # firmware_timestamp (not filled)
        v["voltage_soc"], v["voltage_gfx"], v["voltage_mem"], U16,
        v["indep_throttle_status"])
    assert len(blob) == 120
    return blob


def pp_od_clk_voltage(sclk_offset=0, uclk_min=97, uclk_max=1258, vo=0,
                      r_sclk=(-500, 1000), r_mclk=(97, 1500), r_vo=(-200, 0)) -> str:
    # amdgpu_get_pp_od_clk_voltage() order: OD_SCLK, OD_MCLK, OD_FCLK, OD_VDDC_CURVE,
    # OD_RANGE, OD_VDDGFX_OFFSET, OD_CCLK; strings from smu_v14_0_2_emit_clk_levels().
    s = "OD_SCLK_OFFSET:\n" + "%dMhz\n" % sclk_offset
    s += "OD_MCLK:\n" + "0: %uMhz\n1: %uMHz\n" % (uclk_min, uclk_max)
    s += "%s:\n" % "OD_RANGE"
    s += "SCLK_OFFSET: %7dMhz %10uMhz\n" % r_sclk
    s += "MCLK: %7uMhz %10uMhz\n" % r_mclk
    s += "VDDGFX_OFFSET: %7dmv %10dmv\n" % r_vo
    s += "OD_VDDGFX_OFFSET:\n" + "%dmV\n" % vo
    return s


FAN_CURVE = [(40, 20), (55, 35), (70, 50), (85, 75), (95, 100)]   # NUM_OD_FAN_MAX_POINTS - 1


def fan_files() -> dict[str, str]:
    curve = "OD_FAN_CURVE:\n" + "".join("%d: %dC %d%%\n" % (i, t, p) for i, (t, p) in enumerate(FAN_CURVE))
    curve += "%s:\n" % "OD_RANGE"
    curve += "FAN_CURVE(hotspot temp): %uC %uC\n" % (25, 110)
    curve += "FAN_CURVE(fan speed): %u%% %u%%\n" % (15, 100)
    one = lambda hdr, v, key, lo, hi: f"{hdr}:\n{v}\nOD_RANGE:\n{key}: {lo} {hi}\n"  # noqa: E731
    return {
        "fan_curve": curve,
        "acoustic_limit_rpm_threshold": one("OD_ACOUSTIC_LIMIT", 2900, "ACOUSTIC_LIMIT", 500, 3300),
        "acoustic_target_rpm_threshold": one("OD_ACOUSTIC_TARGET", 2200, "ACOUSTIC_TARGET", 500, 3300),
        "fan_target_temperature": one("FAN_TARGET_TEMPERATURE", 90, "TARGET_TEMPERATURE", 25, 110),
        "fan_minimum_pwm": one("FAN_MINIMUM_PWM", 15, "MINIMUM_PWM", 15, 100),
        "fan_zero_rpm_enable": one("FAN_ZERO_RPM_ENABLE", 1, "ZERO_RPM_ENABLE", 0, 1),
        "fan_zero_rpm_stop_temperature": one("FAN_ZERO_RPM_STOP_TEMPERATURE", 50,
                                             "ZERO_RPM_STOP_TEMPERATURE", 25, 110),
    }


PROFILE_NAMES = ["BOOTUP_DEFAULT", "3D_FULL_SCREEN", "POWER_SAVING", "VIDEO", "VR", "COMPUTE",
                 "CUSTOM", "WINDOW_3D"]   # CAPPED/UNCAPPED have no SMU14.0.2 workload mapping


def pp_power_profile_mode(active: int = 1) -> str:
    title = ["PROFILE_INDEX(NAME)", "CLOCK_TYPE(NAME)", "FPS", "MinActiveFreqType", "MinActiveFreq",
             "BoosterFreqType", "BoosterFreq", "PD_Data_limit_c", "PD_Data_error_coeff",
             "PD_Data_error_rate_coeff"]
    s = "%16s %s %s %s %s %s %s %s %s %s\n" % tuple(title)
    for i, n in enumerate(PROFILE_NAMES):
        s += "%2d %14s%s:\n" % (i, n, "*" if i == active else " ")
        s += "%19s %d(%13s) %7d %7d %7d %7d %7d %7d %7d %7d\n" % (" ", 0, "GFXCLK", 0, 1, 500, 1, 0, 0, 0, 0)
        s += "%19s %d(%13s) %7d %7d %7d %7d %7d %7d %7d %7d\n" % (" ", 1, "FCLK", 0, 1, 800, 1, 0, 0, 0, 0)
    return s


def _w(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(data, bytes) else "w") as fh:
        fh.write(data)


def build(root: str, od: bool = True) -> str:
    """Create the tree under `root`; returns the device dir."""
    if os.path.exists(root):
        shutil.rmtree(root)
    dev = os.path.join(root, "sys/class/drm/card0/device")
    files = {
        "uevent": f"DRIVER=amdgpu\nPCI_CLASS=30000\nPCI_ID=1002:7550\nPCI_SUBSYS_ID=1DA2:E490\n"
                  f"PCI_SLOT_NAME={SLOT}\nMODALIAS=pci:v00001002d00007550sv00001DA2sd0000E490bc03sc00i00\n",
        "vendor": "0x1002\n", "device": "0x7550\n", "revision": "0xc0\n",
        "subsystem_vendor": "0x1da2\n", "subsystem_device": "0xe490\n",
        "vbios_version": "113-NAVI48XTX-D01\n",
        "fw_version/smc_fw_version": "0x00685300\n",
        "ip_discovery/die/0/GC/0/major": "12\n", "ip_discovery/die/0/GC/0/minor": "0\n",
        "ip_discovery/die/0/GC/0/revision": "1\n",
        "ip_discovery/die/0/MP1/0/major": "14\n", "ip_discovery/die/0/MP1/0/minor": "0\n",
        "ip_discovery/die/0/MP1/0/revision": "3\n",
        "gpu_metrics": gpu_metrics_v1_3_blob(),
        "gpu_busy_percent": "97\n", "mem_busy_percent": "45\n",
        "mem_info_vram_total": "17163091968\n", "mem_info_vram_used": "6442450944\n",
        "mem_info_vis_vram_total": "17163091968\n", "mem_info_vis_vram_used": "6442450944\n",
        "mem_info_gtt_total": "33554432000\n", "mem_info_gtt_used": "268435456\n",
        "current_link_speed": "32.0 GT/s PCIe\n", "current_link_width": "16\n",
        "max_link_speed": "32.0 GT/s PCIe\n", "max_link_width": "16\n",
        "pp_dpm_sclk": "0: 500Mhz \n1: 2950Mhz *\n2: 2970Mhz \n",
        "pp_dpm_mclk": "0: 97Mhz \n1: 456Mhz \n2: 772Mhz \n3: 1258Mhz *\n",
        "pp_dpm_socclk": "0: 300Mhz \n1: 1200Mhz *\n",
        "pp_dpm_fclk": "0: 400Mhz \n1: 1300Mhz \n2: 1800Mhz \n3: 2200Mhz *\n",
        "pp_dpm_dcefclk": "0: 400Mhz *\n1: 1200Mhz \n",
        "pp_dpm_vclk": "S: 25Mhz *\n0: 500Mhz \n1: 1690Mhz \n",
        "pp_dpm_dclk": "S: 25Mhz *\n0: 500Mhz \n1: 1690Mhz \n",
        "pp_dpm_pcie": "0: 2.5GT/s, x1 619Mhz \n1: 32.0GT/s, x16 1143Mhz *\n",
        "power_dpm_force_performance_level": "auto\n",
        "pp_power_profile_mode": pp_power_profile_mode(1),
    }
    hw = "hwmon/hwmon3/"
    hwmon = {
        "name": "amdgpu\n",
        "temp1_input": "62000\n", "temp1_label": "edge\n", "temp1_crit": "100000\n",
        "temp2_input": "78000\n", "temp2_label": "junction\n", "temp2_crit": "110000\n",
        "temp3_input": "74000\n", "temp3_label": "mem\n", "temp3_crit": "108000\n",
        "fan1_input": "1650\n", "fan1_min": "0\n", "fan1_max": "3300\n", "fan1_target": "1650\n",
        "fan1_enable": "1\n", "pwm1": "120\n", "pwm1_enable": "2\n",
        "pwm1_min": "0\n", "pwm1_max": "255\n",
        "power1_average": "287000000\n", "power1_label": "PPT\n",
        "power1_cap": "304000000\n", "power1_cap_default": "304000000\n",
        "power1_cap_min": "231000000\n",
        "power1_cap_max": ("334000000\n" if od else "317000000\n"),
        "in0_input": "1050\n", "in0_label": "vddgfx\n",
        "freq1_input": "2950000000\n", "freq1_label": "sclk\n",
        "freq2_input": "1258000000\n", "freq2_label": "mclk\n",
    }
    for k, v in files.items():
        _w(os.path.join(dev, k), v)
    for k, v in hwmon.items():
        _w(os.path.join(dev, hw + k), v)
    if od:
        _w(os.path.join(dev, "pp_od_clk_voltage"), pp_od_clk_voltage())
        for k, v in fan_files().items():
            _w(os.path.join(dev, "gpu_od/fan_ctrl", k), v)
    _w(os.path.join(root, "sys/module/amdgpu/parameters/ppfeaturemask"),
       "0xfff7ffff\n" if od else "0xfff7bfff\n")
    # a DRM connector next to the card must be ignored by detect()
    _w(os.path.join(root, "sys/class/drm/card0-DP-1/status"), "connected\n")
    return dev


def main() -> int:
    build(os.path.join(HERE, "rx9070xt"), od=True)
    build(os.path.join(HERE, "rx9070xt_no_od"), od=False)
    print("fixtures written to", HERE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
