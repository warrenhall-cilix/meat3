# RX 9070 / 9070 XT (RDNA4)

Extra monitoring and tuning for RDNA4 cards. Everything in the [AMD GPU](sensors.md#amd-gpus)
section also applies.

## What has been tested

On a real ASUS RX 9070 XT (VBIOS 115-G295BP00-100), Manjaro, kernel 7.2:

| Feature | Status |
|---|---|
| Standard monitoring and firmware power table limits | Works |
| Hidden firmware metrics (`meat3_smu`) | Works |
| VRM telemetry and VRM controls over PMBus | **Depends on the SMU firmware version.** AMD's PMFW stopped exposing the regulators on the "AMDGPU SMU" I2C buses after `smu_14_0_3.bin` 0x684a00 (linux-firmware 2025-06-20, release 20251111). With 0x684b00 (2025-11-19) or newer the buses answer at no address and meat³ shows "VRM controllers: not reachable" with the firmware version. With the older firmware the two MP2868A controllers at 0x22 and 0x24 answer, and meat³ reads their page-2 telemetry gains (the registers fpsflow's `power_limit_removal` scripts scale to lift TBP/TDC) and corrects its power and current readings by the active scale |
| `meat3-gputune` | Uses the driver's own voltage offset, so it doesn't need the VRMs |
| `meat3-skutable` and the patched amdgpu | Not yet tested on hardware |
| TBP/TDC scaling (`vrm.tbp_scale`, `vrm.tdc_scale`) and `meat3-smufw` | Not yet tested on hardware (register map from fpsflow) |
| `meat3-fasttiming` (Powerplay TA via ta_if) | Not yet tested on hardware (RE of the Windows driver + TA) |

## Hidden firmware metrics (`meat3_smu`)

The firmware keeps a full metrics table. amdgpu fetches it but only copies part of it into
`gpu_metrics`.

**The `meat3_smu` module:**
- copies the full table each time the driver reads it;
- exposes the copy at `/sys/kernel/debug/meat3_smu/gpuN`;
- never sends the firmware anything and never changes driver state.

The installer builds it with DKMS when an RDNA4 card is present, or with `--kmod`.

**Sensors it adds:**
- total board power (the figure Windows shows);
- current and power for each rail: GFX, SoC, VDDCI and VDDIO;
- VDDCI voltage;
- GFX and SoC hotspots;
- GFX target clock and fabric clock;
- PCIe busy;
- for each limit (power, current, every temperature, maximum voltage), the share of time it held
  the card back.

**GFX current wraps around:** the firmware reports GFX current as a 16-bit count in mA, which wraps
every 65.5 A. meat³ works out the number of wraps from board power. On the test card at 345 W load,
a raw 30.3 A is a real 227 A. If board power can't settle the count, the reading shows "—".

## VRM telemetry and controls (PMBus)

These only work on cards whose voltage regulators answer on the GPU's I2C buses, at addresses 0x22,
0x24 and 0x26. See the test status above.

### Telemetry

- For each rail: voltage, current, power and temperature.
- The 12 V input voltage and current.

**Real power (VRM measured):** the larger of two figures:
- 12 V input power;
- rail output power ÷ 0.9.

It's measured at the regulators, so it stays correct when the board-power shunts are bridged. On
such cards the driver's power reads near zero, and meat³ shows "Board power sensing: BYPASSED".
For that case there's a guard (off by default) that cuts the GPU clock when real power stays above a
limit:

```json
"guard_overrides": {"AMD real power (VRM) over limit": {"above": 420, "enabled": true}}
```

**Checked on a real RX 9070 XT (ASUS, MP2868A at 0x22 and 0x24):** the SoC and memory rails agree with
the firmware within 10 %. The GFX loop reports half the firmware's GFX current, so meat³ doubles it;
`READ_POUT` and `READ_IIN` read high, so rail power is `VOUT x IOUT` and the regulators' 12 V input
power is shown as unverified and not used for the real-power figure. Two cross-check rows,
*VRM real power / firmware board power* and *VRM GFX current / firmware GFX current*, should sit
between 0.9 and 1.1 at stock gains; if yours don't, say so before scaling anything.

### Controls

These are under Overclock → VRM voltage offsets.

**Locked by default.** They only appear after you set
`"amdgpu_vrm": {"allow_writes": true}` in `/etc/meat3/config.json` and restart `meat3d`.

**Safety behaviour:**
- Every write is read back.
- The changes are lost at power-off.
- *Restore factory settings* writes back the values meat³ read first.

| Control | Register | Effect | Limit |
|---|---|---|---|
| Voltage offset | `VOUT_CAL_OFFSET` | Adds voltage beyond what the driver allows. RDNA4 partly compensates for it | ±`max_offset_mv` (50 mV) |
| Load line | `VOUT_DROOP` | Less voltage sag under load | 0 to 2× factory |
| Current reporting scale | `IOUT_CAL_GAIN` | The regulator reports current ÷ k, so the firmware's current limit (330 A GFX, 88 A SoC) acts as ×k. The regulator's own over-current protection moves by the same factor. meat³ corrects its own current and power readings | `max_current_scale` (1.5) |

**MPS controllers:** RX 9070 XT boards use MPS MP2686A controllers. These don't use plain PMBus
encodings:
- meat³ decodes their voltage the same way Linux's MP2857 driver does. Their other readings are
  standard.
- The voltage offset shows raw steps until you set `mps_offset_lsb_mv` (5 or 6.25, as verified on
  the card).
- The current scale also needs `experimental_mps_writes: true`.
- Load line isn't offered, because MPS uses a vendor-specific register for it.

`sudo bash tools/collect_amdgpu.sh` dumps the registers needed to check these, and whether the
VRMs are reachable at all.

## Measured V/F curve

RDNA 2 and later expose no editable V/F points (only the SCLK and VDDGFX offsets), so the Overclock tab
shows the curve the card actually runs instead: every poll the GFX voltage and clock from `gpu_metrics`
are binned by voltage (5 mV) and the best and latest clock per bin are kept (control
`amdgpuN.oc.vf_measured`, read-only; `amdgpuN.oc.vf_measured_reset` clears it). Idle samples (GFX
activity below 20 % or clock below 500 MHz) are ignored. Load the GPU for a few minutes to fill it in;
the trace moves when you change the offsets, which makes the effect of an undervolt visible as a curve
rather than a single number. The samples live in the daemon and are lost on restart.

## Throttle timeline

Besides the per-class SMU14 throttle flags, the backend adds one 0/1 row per throttle reason that has
fired since the daemon started (`amdgpuN.smu14.throttle.bit.<reason>`, e.g. `ppt0`, `tdc_gfx`,
`temp_hotspot`) and a time-weighted duty figure per class over the last 60 s
(`amdgpuN.smu14.throttle.duty.{power,current,thermal,other,any}`, in %). Add the duty rows to the
Graphs tab while gaming to see which limit is actually holding the clocks back and how often.

## Power-limit scaling (the fpsflow method, inside meat3)

RDNA4 boards use two MPS MP2868A regulators. The firmware's PPT and TDC limits act on the current and
power the regulators *report*, and those report through gain registers on PMBus page 2. Dividing a gain
by 1.25 makes the firmware see 1/1.25 of the real figure, so its limit is effectively 1.25x. This is what
the fpsflow `power_limit_removal` scripts do and what a shunt mod does with solder. meat3 does the same
writes but keeps the corrected picture:

* controls `amdgpuN.vrm.tbp_scale` and `amdgpuN.vrm.tdc_scale` (1.0 to `amdgpu_vrm.max_tbp_scale` /
  `max_tdc_scale`, default 1.3, hard cap 1.5) and the action `amdgpuN.vrm.gains_reset`;
* the stock gains are backed up on the chip in the rdna4p script's own 68-bit CRC format before the
  first write (an existing script backup is recognised and used as stock), and mirrored in
  `/var/lib/meat3/vrm_factory.json`;
* sensors `vrm.real_power_scaled` (regulator-measured power x TBP scale), `vrm.board_power_corrected`
  (driver board power x scale) and `vrm.gfx_current_corrected` show the real numbers while the driver's
  own rows read low by the scale factor;
* default guards drop both scales to 1.0 when the real power (firmware board power x TBP scale) passes
  450 W, the hotspot passes 100 °C or
  any VRM temperature passes 105 °C (edit `guards` in `/etc/meat3/config.json`);
* meat3d writes the stock gains back when it stops (`amdgpu_vrm.restore_on_stop`); a power cycle also
  clears them, a warm reboot does not.

Prerequisites, in order:

1. `amdgpu_vrm.allow_writes: true` in `/etc/meat3/config.json` (the switch that also gates VID offsets).
2. SMU firmware that exposes the VRM bus: PMFW 0x684a00 or older. Newer builds answer at no address, so
   meat3 shows "VRM controllers: not reachable" with a firmware hint. Fix with `sudo meat3-smufw --pin`
   (below) and reboot.
3. Water or a very good air cooler, because the firmware's thermal limits still work but its power
   limit no longer does, and an 8-pin board. Don't do this on a 12V-2x6 card.

Start at 1.1x, watch `Real power (VR, gain-corrected)` and the hotspot under a real game, and go up in
0.05 steps. A crash under load is the card telling you the clock or voltage is the limit now, not power.

## `meat3-smufw`: SMU firmware pin

```sh
sudo meat3-smufw               # running PMFW, installed file, whether it is the known-good build
sudo meat3-smufw --pin         # download linux-firmware 72a8d254's smu_14_0_3.bin (sha256-checked),
                               # back up the current file, install, rebuild the initramfs
sudo meat3-smufw --restore     # back to the distribution file
```

The blob is AMD-signed and checked by the PSP at load, so nothing is flashed and a bad file is refused,
not run. What the pin costs is any SMU fix released after June 2025 while it is in place. On Arch-based
systems a pacman hook re-pins the file after `linux-firmware` upgrades; elsewhere `meat3-smufw` reports
when a package update has replaced it. Reboot after `--pin` or `--restore`.

If the desktop comes back on one screen at 60 Hz after the reboot, amdgpu refused to start: check
`dmesg | grep smu_14` for `Direct firmware load ... failed`, run `sudo meat3-smufw --restore` and reboot.
The restore does not need a working GPU. Nothing is flashed, so this is always recoverable.

## `meat3-fasttiming`: AMD "Fast Timing" memory timings (experimental)

Adrenalin's memory "Fast Timing" is not an SMU setting, which is why Linux has no knob for it. The
Windows driver loads a PSP trusted application ("AMD POWERPLAY Application"), pins the memory clock
one DPM level below the top, asks the TA to program the UMC timing registers from the VBIOS timing
table for a tighter bin, and releases the clock. meat3 ships the same AMD-signed TA images
(`meat3/firmware/ppta/`, unmodified from the Windows package) and drives them through the kernel's
own `ta_if` debugfs interface, so no kernel patch is needed. Everything is volatile.

```sh
sudo meat3-fasttiming status     # RDNA4 card, debugfs ta_if present, memory DPM levels
sudo meat3-fasttiming load       # tries ppta_a.bin, then b, then c, until the PSP accepts one
sudo meat3-fasttiming query      # TA command 2: timing ranges; status 0 = it runs and has a table
sudo meat3-fasttiming enable     # TA command 0, level 1 (one timing bin tighter), clock pinned meanwhile
sudo meat3-fasttiming disable    # TA command 1
sudo meat3-fasttiming unload
```

Status of this feature: reverse-engineered from amdkmdag.sys 32.0.31044.16 and the TA itself, not
yet run on a card. Expected failure modes are clean (the PSP refuses the image, or the TA returns
status 1 because the ASIC id or the clock does not match its table). The unclean one is a badly
programmed memory controller, which freezes the screen until a reboot. Run it on the desktop first,
with `query` before `enable`, and measure with a memory-bound benchmark afterwards.

## `meat3-gputune`: undervolt finder

Lowers the GFX voltage offset step by step while the GPU is under heavy load, and finds the lowest
stable value. It also works on RX 7000 cards.

It uses the driver's Overdrive voltage offset, so it needs `amdgpu.ppfeaturemask=0xfff7ffff`.

```sh
sudo meat3-gputune                      # run a demanding game or benchmark meanwhile
sudo meat3-gputune --load "some-benchmark --loop"
sudo meat3-gputune --status
sudo meat3-gputune --apply              # apply the result (last pass + margin)
sudo meat3-gputune --save-boot          # apply it whenever meat3d starts
```

| Option | Default | |
|---|---|---|
| `--floor` | -200 mV | Most negative offset to try |
| `--step` | 10 mV | Step size |
| `--step-time` | 300 s | Load time per step |
| `--margin` | 15 mV | Added back to the last passing value |
| `--min-load` | 85% | A step only counts while average GFX activity is at least this |
| `--load CMD` | | Command that loads the GPU. Restarted if it exits; a non-zero exit fails the step |
| `--gpu N` | First GPU with a voltage offset | |
| `--clock` | off | Second phase: raise the GFX clock offset at the found undervolt |
| `--clock-step` | 25 MHz | Clock step size |
| `--clock-max` | 400 MHz | Highest clock offset to try |
| `--clock-margin` | 25 MHz | Taken off the last passing clock offset |

Every step records the average GFX clock, board power, PPT throttle duty and peak hotspot while it
ran, and `--status` prints them as a table, so you can see what each 10 mV bought (on a
power-limited RDNA4 card a good undervolt shows up as a higher clock at the same watts). With
`--clock` the tuner then applies the undervolt result and raises the GFX clock offset step by step
under the same pass/fail rules; `--apply` and `--save-boot` set both results.

**When a step fails:**
- an amdgpu error in the kernel log: a ring or job timeout, a GPU reset, a page fault or a hang;
- the GPU stops reporting;
- the `--load` command exits with an error.

It's crash-safe in the same way as [`meat3-cotune`](overclocking.md#per-core-curve-optimizer-tuner-meat3-cotune).

A lower voltage means less current at the same clock, so undervolting is the first step towards
higher clocks within the 330 A current limit.

## `meat3-skutable`: custom SKU table (untested)

The board's "custom SKU table" is what MoreClockTool calls the SoftTuningTable on Windows. It holds:
- the power and current (TDC) limits;
- the temperature and fan limits;
- the board-power formula.

The stock Linux driver can't send it to the GPU, so `tools/build_patched_amdgpu.sh` builds amdgpu
from kernel.org source with a small patch (`kmod/amdgpu-custom-sku/`). This build is for the
running kernel only.

**What the patched driver does:**
- uploads `/lib/firmware/amdgpu/custom_sku_<pci>.bin` while it starts;
- uploads it again on `sudo meat3-skutable --send`.

**What the firmware does with it:**
- checks it against the board's signed limits;
- reports in the kernel log which parts it rejected.

```sh
sudo meat3-skutable --show
sudo meat3-skutable --tdc-gfx 380 --write
sudo meat3-skutable --send
sudo meat3-skutable --remove            # stock table at the next driver load
```

**Other edits:** `--ppt`, `--tdc-soc`, `--temp NAME=C`, `--zero-rpm on|off` and `--fan-max-rpm`.
Raising temperature limits or changing the board-power formula (`--tbp-m`, `--tbp-b`) needs
`--force`.

**What it can't do:** give more power than the BIOS allows. The realistic gains are a higher current
limit and fan control. Raising the current limit this way leaves the firmware's protection in place,
unlike the VRM current scale.

## Limits from the test card's BIOS

| | |
|---|---|
| GPU clock | 3450 MHz top firmware level (stock boost 3010 MHz) |
| Memory | 1500 in Linux units (stock 1259), about 20 → 24 Gbps |
| Voltage | 1.2 V requested by the firmware; only a VRM offset goes beyond it |
| Current | 330 A GFX, 88 A SoC |
| Memory timings | `meat3-fasttiming` (experimental, via AMD's Powerplay TA) |
