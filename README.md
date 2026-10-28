# meat³ (meat3)

HWiNFO-style hardware monitor, OSD and overclocking tool for Linux.
**NVIDIA GPUs, AMD GPUs (incl. RX 9070 XT / RDNA4), AMD Ryzen CPUs** and motherboard sensors, with hardware auto-detected.

```
meat3d (root daemon)  ── sensors + OC backends ── /run/meat3/meat3.sock ──  meat3 (GUI, your user)
        │                                                                  ├─ Sensors tree (HWiNFO-style)
        ├─ hotkey (Right Ctrl + F12, evdev)                                ├─ Cores tab (per-core table)
        │                                                                  ├─ Overclock tab
        └─ /run/meat3/osd.txt ── MangoHud (in-game OSD)                    └─ OSD tab + desktop overlay
```

## Install (Arch / CachyOS; Debian/Fedora best-effort)

```sh
git clone <this repo> meat3 && cd meat3
./packaging/install.sh          # installs deps, /opt/meat3, meat3d.service, adds you to group 'meat3'
# log out/in once, then:
meat3
```
Uninstall: `./packaging/install.sh --uninstall`. Config: `/etc/meat3/config.json`.

Optional extras:
- **AMD CPU fabric/SMU data** (FCLK/UCLK/MCLK, SVI3 voltages, per-core power/voltage, PPT/TDC/EDC): `ryzen_smu-dkms-git` (AUR).
- **Motherboard fans/voltages** on Gigabyte and other ITE boards: `it87-dkms-git` (AUR).
- **NVIDIA extras** (NVVDD/MSVDD voltages and limits, XBAR/SYS offsets, Vrel offsets) need the patched open NVIDIA module from the nvoc work. Without it, those rows and controls show as unavailable. Everything else uses stock NVML.

## What it reads

| Device | Highlights |
|---|---|
| **NVIDIA** | GPC/SM/memory/video clocks, plus XBAR/SYS/HUB/NVD clocks; NVVDD/MSVDD voltage and REL/ALT/OV limits; board power raw and **shunt-mod corrected**; **GDDR7 memory temperature**; limiter flags; offsets; VRAM; PCIe; fans |
| **ASUS ROG Astral** | **12V-2x6 per-pin voltage/current** (ITE IT8915FN), connector total, imbalance %, aux 12 V inputs |
| **AMD GPU** (amdgpu) | Every `gpu_metrics` version (generated from kernel headers; RX 9070 XT = v1_3): edge/hotspot/memory/VR temperatures, GFX/SoC/memory voltages, all clocks, socket power, throttle reasons, fan, PCIe; plus hwmon and sysfs |
| **AMD CPU** (Zen 2–5) | Per-core effective clock (APERF/MPERF), load, C-states, power, voltage, temperature; package power (RAPL/MSR); **FCLK/UCLK/MCLK**; **L3 hit rate per CCX**; **DRAM read/write bandwidth**; PPT/TDC/EDC; SVI3 rails |
| **Board** | Super I/O fans/voltages/temperatures, NVMe, DDR5 SPD temperatures, chipset/NIC/Wi-Fi temperatures |

### Cores tab

One row per CPU core: effective and active clock, voltage, temperature, power, usage, C0/CC6 residency, SMU Fmax and CPPC rank, with the session max next to clock, voltage, temperature and power. The fastest and hottest cores are bold. Right-click any cell to put that sensor, or the whole column, on the OSD. For a compact OSD, use the summary sensors **Hottest core temperature** and **Highest core voltage** (CPU → Per-core summary).

Per-core voltage is the SMU's requested VID for that core, not a socket measurement. It is good for comparing cores and seeing Curve Optimizer changes.

## Overclocking

- **NVIDIA:** power limit, core/memory offsets, max-clock lock, XBAR/SYS offsets, MSVDD/NVVDD Vrel offsets.
- **AMD GPU:** power cap, performance level, power profile.
  - Overdrive (RDNA4/RDNA3: clock offset, memory clock, voltage offset, fan curve/zero-RPM) needs the kernel parameter **`amdgpu.ppfeaturemask=0xfff7ffff`** and a reboot. Without it the controls show as greyed out.
- **AMD CPU (Ryzen only):** governor, EPP, boost, min/max frequency, amd-pstate mode, plus **Curve Optimizer (all-core and per-core) and PBO PPT/TDC/EDC limits** through the SMU (needs `ryzen_smu`).
  - Validated on a 9800X3D (Granite Ridge): CO −5 write and per-core readback, 45 s verified stress with no errors, restore; PPT 162→170→162 W read back from the SMU power table.
  - SMU settings are not persistent and reset at reboot.
  - Disable with `"amd_smu_controls": false` in the config.

Every control is range-checked by the daemon. Only root or members of group `meat3` can apply changes. Settings are not persistent: save a profile in the GUI and re-apply it.

## OSD

- Pick sensors in the **OSD** tab and toggle with **Right Ctrl + F12**. The hotkey works system-wide, including in fullscreen games, because the daemon reads the keyboard.
- **Desktop:** the GUI shows a click-through overlay.
- **In-game:** launch games with MangoHud using meat³'s config, e.g. Steam launch options:
  `MANGOHUD_CONFIGFILE=$HOME/.config/meat3/MangoHud.conf mangohud %command%`
  (generated from the OSD tab).

## Safety notes

- Board power on shunt-modded cards is under-reported. Set the correction factor in `/etc/meat3/config.json`:
  `"nvidia_shunt_factor": {"0000:01:00.0": 1.5}`. For example, an R004 on top of an R002 gives 1.5×.
- 12V-2x6 pins are rated ~9.5 A. The GUI turns amber above 8 A and red above 9 A.
- Built-in guards (editable under `"guards"` in the config) drop the NVIDIA power limit to 300 W if any single pin exceeds 8.5 A, the hotspot exceeds 100 °C or the memory exceeds 95 °C, and set an AMD GPU's power cap to minimum if its junction temperature exceeds 105 °C.
