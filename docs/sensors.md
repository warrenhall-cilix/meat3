# Sensors

What meat³ reads on each kind of device. Every sensor's tooltip names its exact source.

Sensors that need something missing (a kernel module, a patched driver) are either left out or
shown as unavailable, with the reason in the tooltip.

## NVIDIA GPUs

Read through NVML, plus a few registers through the GPU's resource manager.

**Clocks:**
- GPC, SM, memory and video;
- XBAR, SYS, HUB and NVD.

**Voltage:**
- NVVDD and MSVDD, with their REL/ALT/OV limits.
- Needs the patched open kernel module; see [install.md](install.md#optional-extras).

**Power:**
- board power, as reported and with a shunt-mod correction applied (see
  [configuration.md](configuration.md));
- the active power and thermal limiters.

**Temperatures:** GPU, hotspot and GDDR7 memory.

**Other:**
- clock offsets;
- VRAM use;
- PCIe link;
- fan speeds.

**12V-2x6 connector:**
- Applies to cards with an ITE IT8915FN connector monitor, such as the ASUS ROG Astral.
- Shows per-pin voltage and current, connector total and imbalance, and the auxiliary 12 V inputs.
- Pins are rated at about 9.5 A. The desktop app turns a pin amber above 8 A and red above 9 A.

## AMD GPUs

**gpu_metrics:**
- Read from amdgpu's `gpu_metrics` table, every version (the decoders are generated from the
  kernel headers).
- Edge, hotspot, memory and VR temperatures.
- GFX, SoC and memory voltages.
- All clocks, socket power, activity, throttle reasons, fan and PCIe.

**Other sources:** amdgpu's hwmon and sysfs files.

**RX 9070 / 9070 XT, from the firmware power table:**
- rated base, game and boost clocks;
- power and current limits;
- throttle and shutdown temperature for each sensor;
- the GFX voltage ceiling and Overdrive headroom.

The throttle reasons use the RDNA4 names. They include the GFX and SoC hotspots, which the driver's
summary leaves out.

**RX 9070 / 9070 XT with the `meat3_smu` module:** further firmware metrics and VRM telemetry.
See [rdna4.md](rdna4.md).

## AMD Ryzen CPUs (Zen 2–5)

| Sensor | Source |
|---|---|
| Per-core effective clock | APERF/MPERF |
| Per-core load and C-state residency | `/proc/stat`, cpuidle, SMU |
| Per-core power, voltage, temperature | SMU power table (`ryzen_smu`) |
| Package power | RAPL MSRs |
| FCLK, UCLK, MCLK | SMU power table |
| SVI3 rail voltages, PPT/TDC/EDC use | SMU power table |
| L3 in use per CCD (MB and %) | resctrl monitoring |
| L3 hit rate per CCX | L3 performance counters (`amd_uncore`) |
| DRAM read/write bandwidth | Data fabric counters |
| IPC and pipeline stalls per core | Core performance counters (Zen 4/5; IPC only on Zen 2/3) |

### Pipeline stalls

For each core, the CPU's own counters split every dispatch slot into five buckets:

| Bucket | Meaning |
|---|---|
| Retiring | Useful work |
| Back-end bound | Waiting on memory, cache or busy execution units |
| Front-end bound | Waiting for instructions: code cache misses, branch redirects |
| Bad speculation | Work thrown away after a mispredict |
| SMT contention | The slot went to the other thread on the same core |

The five add up to 100%. Back-end bound is the one that drops when a game gets more cache.

The counters run in counting mode, with no sampling or interrupts. Reading all 96 on a 9800X3D
takes about 0.1 ms each second. If another tool such as `perf` is using counters, the kernel
time-shares them and the values are scaled; the tooltip says when this happens.

**Per program:** IPC, back-end bound and front-end bound are also counted for the running game, or
for the busiest programs too (setting `cpu_topdown.per_app`). Counting a single program has a cost:
- **Context switches:** the kernel swaps a counted thread's counters on every context switch, at
  about 1.1 µs per switch on a 9800X3D. The *Per-app counting cost* sensor estimates the live
  overhead.
- **Which threads are counted:** only threads using at least 1% of a core, up to 16 per program.
  Threads that switch more than 20,000 times a second are skipped. The tooltip shows how much of
  the program's CPU time is covered.
- **Shared counters:** while a program is being counted, the per-core view and the per-program view
  take turns each second.

### L3 cache per program

The daemon gives each busy program its own resctrl monitoring group, and the CPU counts in hardware
how many cache lines each group holds. The Cores tab's Apps table shows this per program, in MB and
as a share of L3, plus "everything else".

**Requirements:**
- a CPU with `cqm_occup_llc`: AMD Zen 2 and later, most Intel server and HEDT parts;
- a kernel built with `CONFIG_X86_CPU_RESCTRL`.

The daemon mounts `/sys/fs/resctrl` itself.

**Game detection:**
- Steam titles, by their `SteamAppId`, including Proton and Wine helper processes.
- Any program whose name matches `l3_per_app.game_patterns` (default `*.exe`).

The *Game L3 in use* sensor follows whichever game is running.

Cache lines count toward the program that loaded them, so a newly tracked program takes a second or
two to show its full use.

For giving a game most of the L3 to itself, see
[overclocking.md](overclocking.md#l3-reservation-for-games).

## Other CPUs (Intel, older AMD)

These are read through portable kernel interfaces only:
- cpufreq clocks;
- usage from `/proc/stat`;
- cpuidle residency;
- coretemp or other CPU hwmon temperatures;
- RAPL power through powercap (package, core, uncore, DRAM).

## Motherboard

**Super I/O chips:** fans, voltages and temperatures. Supported chips are ITE (`it87`) and Nuvoton
(`nct6775`, `nct6683`).

**Board maps:** known boards get a map from raw channel names like `in3` or `fan2` to real names like
"CPU VCORE" or "System fan 1". Unknown boards show the raw channel names.

**Also read:**
- DDR5 SPD temperatures;
- chipset, NIC and Wi-Fi temperatures from their hwmon drivers.

## Storage

**NVMe:**
- every temperature sensor the drive reports, with the drive's own warning and critical limits;
- health: life used and remaining, available spare, data written and read, power-on hours, unsafe
  shutdowns, media errors, time over temperature, and thermal throttling events;
- PCIe link generation and width;
- read and write throughput and active time.

**SATA:**
- temperature through `drivetemp`;
- SMART through `smartctl`: reallocated and pending sectors, CRC errors, wear and data written.

Sleeping hard drives are never woken up to read them.

## Network

**Which interfaces:**
- **Included:** physical Ethernet, Wi-Fi and USB tethering, plus VPN tunnels (WireGuard, Tailscale,
  ZeroTier, tun/tap).
- **Skipped:** loopback, Docker and VM bridges, veths and Proxmox tap devices.

**For each interface:**

| Sensor | Notes |
|---|---|
| Download / Upload | Mbit/s, from `/sys/class/net/<if>/statistics` |
| Link utilisation | Busier direction against the negotiated speed (Ethernet only) |
| Downloaded / Uploaded since boot | GB |
| Packets received / sent | Per second |
| Errors, Dropped | Counts since boot. Rising errors usually mean a bad cable, port or driver |
| Link up, Link speed | A gigabit port showing 100 Mbit/s points to a cable problem |
| Wi-Fi signal and link quality | From `/proc/net/wireless` |

**System-wide:**
- total download and upload over the physical interfaces;
- established TCP connections;
- TCP retransmit rate. Above about 1% for long periods means packet loss somewhere.
