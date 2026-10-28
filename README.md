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
| **AMD GPU** (amdgpu) | Every `gpu_metrics` version (generated from kernel headers; RX 9070 XT = v1_3): edge/hotspot/memory/VR temperatures, GFX/SoC/memory voltages, all clocks, socket power, throttle reasons, fan, PCIe; plus hwmon and sysfs. **RX 9070 / 9070 XT:** board limits from the firmware power table (rated base/game/boost clocks, power and current limits, per-sensor throttle and shutdown temperatures, GFX voltage ceiling, overdrive headroom) and throttle reasons with RDNA4 names, including the GFX/SoC hotspots the driver's summary drops |
| **AMD CPU** (Zen 2–5) | Per-core effective clock (APERF/MPERF), load, C-states, power, voltage, temperature; package power (RAPL/MSR); **FCLK/UCLK/MCLK**; **L3 in use per CCD (MB and %, via resctrl)**; **L3 hit rate per CCX**; **DRAM read/write bandwidth**; PPT/TDC/EDC; SVI3 rails |
| **Board** | Super I/O fans/voltages/temperatures, NVMe, DDR5 SPD temperatures, chipset/NIC/Wi-Fi temperatures |

### Graphs tab

A Task Manager / netdata style grid of live charts for any sensors you pick.
- **Picking sensors:** tick them in the searchable tree on the left, use **Presets** (Overview, CPU cores, CPU clocks, GPU, Temperatures, Power), or right-click a sensor on the Sensors tab → **Add to Graphs**.
- **Each card** shows the current value, a chart over the chosen window (1–30 min), and min/avg/max for that window. Percentages use a fixed 0–100 axis; everything else scales automatically.
- **Hover** any chart: a crosshair moves across all charts at the same moment and shows every value at that time.
- **Combine** charts by dragging one onto another (same unit only), e.g. GPU, hotspot and memory temperatures together. Right-click a card to split, reorder or remove it.
- The last 30 minutes are kept in memory, so a newly added sensor shows its recent history straight away. Your layout is saved.

### Cores tab

One row per CPU core: effective and active clock, voltage, temperature, power, usage, C0/CC6 residency, SMU Fmax and CPPC rank, with the session max next to clock, voltage, temperature and power. The fastest and hottest cores are bold. Right-click any cell to put that sensor, or the whole column, on the OSD. For a compact OSD, use the summary sensors **Hottest core temperature** and **Highest core voltage** (CPU → Per-core summary).

Per-core voltage is the SMU's requested VID for that core, not a socket measurement. It is good for comparing cores and seeing Curve Optimizer changes.

### L3 cache by app and by game

The **Cores** tab lists how much L3 each busy program is actually holding, live, in MB and as a share of the cache, plus "everything else".
- **Games are detected automatically:** Steam titles through `SteamAppId` (including Proton/Wine helper processes), and other programs whose name matches `l3_per_app.game_patterns` (default `*.exe`).
- **Game L3 in use** is a stable sensor that follows whichever game is running. Put it on the OSD to see how much of the X3D cache a game uses.

**L3 reservation for games** (on by default; Overclock → L3 cache): while a game runs, everything else may only fill a small slice of L3 (default 12 MB) and the game gets the rest (84 MB on a 9800X3D) to itself, so browsers, Discord or shader compiles can't push the game's data out of the cache. On a 9800X3D with four cache-thrashing background threads, a 64 MB random-access test "game" went from 128 ns to 25.7 ns per access. It does nothing measurable without background load. It uses resctrl cache allocation (`cat_l3`), switches on 1–2 s after the game starts and off when it exits, and nothing persists across reboots.

How it works: the daemon gives each tracked program its own resctrl monitoring group, and the CPU counts cache lines per group in hardware. This needs a CPU with `cqm_occup_llc` (AMD Zen 2+ and most Intel server/HEDT parts) and a kernel with `CONFIG_X86_CPU_RESCTRL`. The daemon mounts `/sys/fs/resctrl` itself. Cache lines count toward the program that filled them, so a newly tracked program ramps up over a second or two. Configure or disable it under `"l3_per_app"` in the config.

## Overclocking

- **NVIDIA:** power limit, core/memory offsets, max-clock lock, XBAR/SYS offsets, MSVDD/NVVDD Vrel offsets.
- **AMD GPU:** power cap, performance level, power profile.
  - Overdrive (RDNA4/RDNA3: clock offset, memory clock, voltage offset, fan curve/zero-RPM) needs the kernel parameter **`amdgpu.ppfeaturemask=0xfff7ffff`** and a reboot. Without it the controls show as greyed out.
  - On RDNA4 the memory-clock min/max controls use the board's real per-field ranges from `pp_table`. The `OD_RANGE` the driver prints is the union of the two, so some values inside it would be rejected.
  - Power limits above the firmware limit are applied as whole-% overdrive steps. meat³ reports the value the driver actually set. If the driver rejects an overdrive setting, meat³ shows the kernel's reason (for example `OD_FAN_CURVE_PWM_ERROR`).
- **AMD CPU (Ryzen only):** governor, EPP, boost, min/max frequency, amd-pstate mode, plus **Curve Optimizer (all-core and per-core) and PBO PPT/TDC/EDC limits** through the SMU (needs `ryzen_smu`).
  - Validated on a 9800X3D (Granite Ridge): CO −5 write and per-core readback, 45 s verified stress with no errors, restore; PPT 162→170→162 W read back from the SMU power table.
  - SMU settings are not persistent and reset at reboot.
  - Disable with `"amd_smu_controls": false` in the config.

### Per-core Curve Optimizer tuner (`meat3-cotune`)

```sh
sudo meat3-cotune                           # all cores, best CPPC-ranked first; 60 s per step, down to -30
sudo meat3-cotune --cores 0,4 --floor -40 --step-time 300    # longer, deeper search on chosen cores
sudo meat3-cotune --status                  # results so far
sudo meat3-cotune --apply                   # apply results (last pass + 3 margin)
sudo meat3-cotune --verify-all 30           # 30 min all-core verified stress with the results applied
sudo meat3-cotune --save-boot               # have meat3d apply them at every boot
```
- **How a step is tested:** one core at a time, CO is stepped down by 5 and that core alone runs single-threaded stress-ng with result verification (several methods, then a bursty phase for boost transitions). A step fails on a verification error or a kernel machine-check / segfault / GPF.
- **Crash-safe:** each step is recorded on disk before it runs. If the PC hangs or reboots, CO resets to the BIOS values, and the next `meat3-cotune` run marks that value unstable and carries on.
- **Boot apply is guarded:** if a boot dies within 5 minutes of applying the values, the next start skips them and logs why.
- **Quick mode is a first pass:** a CO that passes it can still fail in real use. For daily use, follow it with long `--step-time` runs and `--verify-all`.

Every control is range-checked by the daemon. Only root or members of group `meat3` can apply changes. Settings are not persistent: save a profile in the GUI and re-apply it.

## Themes

View → Theme: **Dark**, **Light** or **Liquid Ass**, a Liquid Glass-style theme. Liquid Ass layers translucent glass panels over a painted liquid backdrop, with specular rims, a top-lit sheen, and pill-shaped tabs and buttons. Start with a specific theme using `meat3 --theme liquid`.

## TUI

```sh
meat3-tui            # same daemon, in a terminal (works over SSH)
```
Views: **1 Overview** (dashboard panels with bars, per-pin current and per-core clocks), **2 Sensors** (everything, with min/max/avg; `/` to filter), **3 Cores** (per-core table and L3 by app), **4 Overclock** (select a control and press Enter to set it; dangerous settings require typing `yes`).
Keys: `Tab` cycles views, `o` toggles the OSD, `r` resets stats, `q` quits. Uses only Python's built-in curses.

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
