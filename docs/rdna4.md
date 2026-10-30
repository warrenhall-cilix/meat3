# RX 9070 / 9070 XT (RDNA4)

Extra monitoring and tuning for RDNA4 cards. Everything in the [AMD GPU](sensors.md#amd-gpus)
section also applies.

## What has been tested

On a real ASUS RX 9070 XT (VBIOS 115-G295BP00-100), Manjaro, kernel 7.2:

| Feature | Status |
|---|---|
| Standard monitoring and firmware power table limits | Works |
| Hidden firmware metrics (`meat3_smu`) | Works |
| VRM telemetry and VRM controls over PMBus | **Not usable on that card.** The GPU's "AMDGPU SMU" I2C buses answered at no address during a full read scan, so on RDNA4 the voltage regulators appear to be reachable only by the GPU firmware. On such cards meat³ shows "VRM controllers: not reachable" and offers no VRM controls |
| `meat3-gputune` | Uses the driver's own voltage offset, so it doesn't need the VRMs |
| `meat3-skutable` and the patched amdgpu | Not yet tested on hardware |

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
| Memory timings | No "fast timings" option on Linux |
