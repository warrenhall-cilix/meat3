# meat³ (meat3)

HWiNFO-style hardware monitor, OSD and overclocking tool for Linux.
**NVIDIA GPUs, AMD GPUs (incl. RX 9070 XT / RDNA4), AMD Ryzen CPUs** and motherboard sensors, with hardware auto-detected.

```
meat3d (root daemon)  ── sensors + OC backends ── /run/meat3/meat3.sock ──  meat3 (GUI, your user)
        │                                                                  ├─ Sensors tree (HWiNFO-style)
        ├─ hotkey (Right Ctrl + F12, evdev)                                ├─ Cores tab (per-core table)
        │                                                                  ├─ Overclock tab
        └─ /tmp/meat3/osd.txt ── MangoHud (in-game OSD)                    └─ OSD tab + desktop overlay
```

## Install (Arch / CachyOS; Debian/Fedora best-effort)

```sh
git clone https://github.com/warrenhall-cilix/meat3.git && cd meat3
./packaging/install.sh          # installs deps, /opt/meat3, meat3d.service, adds you to group 'meat3'
# log out/in once, then:
meat3
```
Uninstall: `./packaging/install.sh --uninstall`. Config: `/etc/meat3/config.json`.

**Updating:** Help → *Check for updates…* in the GUI, or `u` in `meat3-tui`. A "⬆ N updates available"
button also appears in the status bar. Or from a terminal: `~/meat3/packaging/update.sh`.
- Either way it pulls from GitHub, reinstalls and restarts `meat3d`. It only rebuilds the RDNA4 kernel
  module if that changed. Reopen the window afterwards to load the new GUI.
- The buttons only work for the user who owns the git checkout, because the update runs that
  checkout's installer as root.

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

### Pipeline stalls (Zen 4 / Zen 5)

Per core, from the CPU's own performance counters: **IPC** (instructions per clock) and where each dispatch slot went:
- **Back-end bound:** waiting on memory/cache or busy execution units. This is the one that drops when a game gets more cache (compare it with L3 reservation on and off).
- **Front-end bound:** waiting for instructions (code cache misses, branch redirects).
- **SMT contention:** the slot went to the other thread on the same core.
- **Bad speculation:** work thrown away after a mispredict.
- **Retiring:** useful work.

The buckets add up to 100%. They show in the Cores tab, under CPU → Pipeline stalls, and in Graphs → Presets → **CPU stalls**, with whole-CPU totals for the OSD. Zen 2/3 get IPC only.

The counters run in counting mode (no sampling or interrupts). Reading all 96 on a 9800X3D takes about 0.1 ms per second, so it costs nothing measurable. If another tool such as `perf` holds counters, the kernel time-shares them and the values are scaled (the hint says so). Disable with `"cpu_topdown": {"enabled": false}`.

**Per program:** IPC, back-end bound and front-end bound for the running game (or for the busiest apps too). These show in the Cores tab's **Apps** table next to each program's L3 use, and as Game IPC / back-end / front-end sensors that follow whichever game is running (good for the OSD). Choose **off / game / all** under Overclock → L3 cache → Per-app pipeline counters (default: game).

Counting one program isn't free like the per-core view, so it's limited:
- **Context-switch cost:** the kernel swaps a counted thread's counters in and out on every context switch. Measured on the 9800X3D: about 1.1 µs per switch with the 4 events used. The **Per-app counting cost** sensor estimates the live overhead as % of one core.
- **Which threads:** only threads using ≥1% of a core are counted (up to 16 per program). Threads switching more than 20,000 times a second are skipped; the hint says how many, and what share of the program's CPU time is covered.
- **Taking turns:** the CPU always schedules the per-core counters ahead of per-thread ones, so while a program is counted the two views alternate each second. Each view then updates every other second.

On a context-switch torture test, a ping-pong between two threads at about 950k switches/s, counting every thread would cost 40–60%. In practice those threads are skipped, and with the daemon running the benchmark stayed within noise (459k vs 475k round trips/s). A compute-bound game thread costs nothing measurable.

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
Keys: `Tab` cycles views, `o` toggles the OSD, `r` resets stats, `u` checks for and installs updates, `q` quits. Uses only Python's built-in curses.

## RX 9070 / 9070 XT (RDNA4) extras

Everything below is RDNA4-only. Monitoring and the hidden-metrics module have run on a real ASUS
RX 9070 XT (Manjaro, kernel 7.2). The VRM features below have not: that card's "AMDGPU SMU" I2C buses
answer at no address at all (full read scan), so on RDNA4 the regulators look reachable only by the GPU
firmware. `meat3-gputune` doesn't need them; it uses the driver's own voltage offset. The SKU-table
tool is still untested on hardware.

- **Hidden firmware metrics.** `kmod/meat3_smu` (installed via DKMS automatically when an RDNA4 card
  is present, or with `install.sh --kmod`) copies the firmware's full metrics table that amdgpu
  fetches but only partly exposes. New sensors: total board power (what Windows shows), current and
  power per rail (GFX, SoC, VDDCI, VDDIO), VDDCI voltage, GFX and SoC hotspots, GFX target clock,
  fabric clock, PCIe busy, and the share of time each limit (power, current, every temperature,
  voltage max) held the card back. It never talks to the firmware. Checked on a real ASUS RX 9070 XT
  (VBIOS 115-G295BP00-100): rail currents are in mA. The GFX value is a 16-bit count that wraps every 65.5 A under load;
  meat3 works out the wrap count from board power (on that card, 345 W load: 30.3 A raw = 227 A real)
  and shows "—" if board power can't decide it.
- **VRM telemetry over PMBus.** The voltage regulators at 0x22/0x24/0x26 on the GPU's own "AMDGPU SMU"
  I2C buses: measured voltage, current, power and temperature per rail, plus input voltage/current.
  **Real power (VRM measured)** is the larger of the 12 V input power into the regulators and the
  rail output power ÷ 0.9. It's measured at the regulators, so it stays correct on cards whose
  board-power shunts are bridged or shunt-modded. On those cards the driver's power reads near zero,
  and meat3 flags it as "Board power sensing: BYPASSED". A guard (disabled by default) cuts the GPU
  clock when real power stays above a limit. Enable it with e.g.
  `"guard_overrides": {"AMD real power (VRM) over limit": {"above": 420, "enabled": true}}`.
- **VRM voltage, load line and current scale** (Overclock → VRM voltage offsets). All off unless
  `"amdgpu_vrm": {"allow_writes": true}` is set in `/etc/meat3/config.json`, read back after every
  write, and volatile (a power cycle restores factory). "Restore factory settings" writes back what
  meat3 saw first.
  - **Voltage offset** (`VOUT_CAL_OFFSET`): adds voltage the driver doesn't allow, ±`max_offset_mv`
    (50). Linear VOUT mode regulators only. RDNA4 partly compensates for it.
  - **Load line** (`VOUT_DROOP`): less sag under load. 0 to 2× factory.
  - **Current reporting scale** (`IOUT_CAL_GAIN`): the regulator reports current ÷ k, so the
    firmware's current limit (TDC 330 A GFX / 88 A SoC on every 9070 XT) effectively becomes ×k. The
    regulator's own over-current protection moves with it. meat3 corrects its own current and power
    readings, including the hidden firmware metrics. Capped by `max_current_scale` (1.5).
  - **MPS controllers** (the MP2686A used on RX 9070 XT boards) don't use plain PMBus encodings.
    meat3 detects them and decodes their voltage the way Linux's MP2857 driver does. The other
    readings are standard. MPS writes have extra locks:
    - The voltage offset shows raw steps until `"mps_offset_lsb_mv"` is set (5 or 6.25, verified on
      the card).
    - The current scale needs `"experimental_mps_writes": true`.
    - Load line isn't offered (MPS uses a vendor register for it).
    `tools/collect_amdgpu.sh` dumps the registers needed to verify these.
- **`meat3-gputune`**: automatic undervolt finder. It steps the GFX voltage offset down 10 mV at a
  time while you run a demanding game or benchmark (or `--load CMD`). A step only counts while the
  GPU is ≥85% busy, and fails on any amdgpu timeout, reset or page fault in the kernel log. It's
  crash-safe like `meat3-cotune`, with `--apply`, `--save-boot` and `--status`.
- **`meat3-skutable`** + `tools/build_patched_amdgpu.sh`: the board "custom SKU table" (Windows:
  MoreClockTool's SoftTuningTable). This covers power, current (TDC), temperature and fan limits, and
  the board-power formula. The stock Linux driver can't send it, so the script builds amdgpu from
  kernel.org source with a small patch, for the running kernel only. The patched driver uploads
  `/lib/firmware/amdgpu/custom_sku_<pci>.bin` during init, and also on
  `sudo meat3-skutable --send`. The firmware checks it against the signed board limits and the
  kernel log shows which parts it rejected. It can't give more watts than the BIOS allows. The
  realistic gains are higher current limits and fan control. Raising temperature limits or
  changing the board-power formula needs `--force`.


- Pick sensors in the **OSD** tab and toggle with **Right Ctrl + F12**. The hotkey works system-wide, including in fullscreen games, because the daemon reads the keyboard.
- **MangoHud in every game:** tick *In every game* in the OSD tab's MangoHud box. MangoHud then loads
  into every Vulkan game (all Proton games) with no launch options. It starts hidden; press the
  hotkey. It sets `MANGOHUD=1` in `~/.config/environment.d/90-meat3-mangohud.conf`, read at login, so
  log out and back in once. OpenGL-only games still need `mangohud %command%`.
- **Change the hotkey:** OSD tab → *Change…* (press the new combo), or `h` in `meat3-tui`. The daemon
  switches immediately. meat3 also rewrites `toggle_hud` in its MangoHud config, so the same keys toggle
  MangoHud in game; MangoHud reloads its config live. Use 1–4 keys ending in a normal key, e.g.
  Left Alt + O.
- **Desktop:** the GUI shows a click-through overlay.
- **In-game:** launch games with MangoHud using meat³'s config, e.g. Steam launch options:
  `MANGOHUD_CONFIGFILE=$HOME/.config/meat3/MangoHud.conf mangohud %command%`
  (generated from the OSD tab).

## Safety notes

- Board power on shunt-modded cards is under-reported. Set the correction factor in `/etc/meat3/config.json`:
  `"nvidia_shunt_factor": {"0000:01:00.0": 1.5}`. For example, an R004 on top of an R002 gives 1.5×.
- 12V-2x6 pins are rated ~9.5 A. The GUI turns amber above 8 A and red above 9 A.
- Built-in guards (editable under `"guards"` in the config) drop the NVIDIA power limit to 300 W if any single pin exceeds 8.5 A, the hotspot exceeds 100 °C or the memory exceeds 95 °C, and set an AMD GPU's power cap to minimum if its junction temperature exceeds 105 °C.
