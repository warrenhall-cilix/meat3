# Overclocking and tuning

Controls are on the desktop app's Overclock tab and in view 4 of the TUI.

**How a change is applied:**
- The daemon checks every value against the control's range before applying it.
- Only root and members of the `meat3` group can apply changes.
- The web UI can't apply anything.

**What persists:**
- Nothing is persistent: hardware settings return to stock at reboot, and some at driver reload.
- To keep a set of values, save it as a profile on the Overclock tab and re-apply it.
- Alternatively, list the controls under `apply_on_start` in the config (see
  [configuration.md](configuration.md)).

## Controls

### NVIDIA

**Standard (NVML):**
- power limit;
- core and memory clock offsets;
- maximum core clock lock.

**Patched open kernel module only:**
- XBAR and SYS clock offsets;
- MSVDD and NVVDD Vrel voltage offsets;
- clock ratios.

### AMD GPUs

**Always available:**
- power cap;
- performance level;
- power profile.

**Overdrive:**
- Includes clock offset, memory clock, voltage offset, fan curve and zero-RPM.
- Needs the kernel parameter `amdgpu.ppfeaturemask=0xfff7ffff` and a reboot. Without it the
  controls are greyed out.

**On RDNA4:**
- **Memory clock ranges:** the minimum and maximum memory clock controls use the board's real
  per-field ranges from `pp_table`. The `OD_RANGE` the driver prints is the union of those ranges,
  so some values inside it would be rejected.
- **Power limits above the firmware limit:** these are applied as whole-percent Overdrive steps.
  meat³ shows the value the driver actually set.
- **Rejected settings:** if the driver rejects an Overdrive setting, meat³ shows the kernel's
  reason, for example `OD_FAN_CURVE_PWM_ERROR`.

RX 9070 / 9070 XT VRM controls and `meat3-gputune` are covered in [rdna4.md](rdna4.md).

### AMD Ryzen CPUs

**cpufreq:**
- governor;
- energy performance preference;
- boost;
- minimum and maximum frequency;
- amd-pstate mode.

**Through the SMU** (needs `ryzen_smu`):
- Curve Optimizer, all-core and per-core;
- PBO PPT, TDC and EDC limits.

These have been checked on a Ryzen 7 9800X3D:
- a Curve Optimizer write and per-core readback;
- a 45-second verified stress test, then restore;
- PPT 162 → 170 → 162 W, read back from the SMU power table.

SMU settings reset at reboot. To turn the SMU controls off, set `"amd_smu_controls": false`.

Other CPUs get the cpufreq controls only.

### L3 reservation for games

On CPUs with L3 cache allocation (`cat_l3`), meat³ can stop background programs from pushing a
game's data out of L3. It's under Overclock → L3 cache and is on by default.

**How it works:**
- While a game runs, every other program may only fill a small slice of L3: 12 MB by default,
  rounded to whole cache ways.
- The game gets the rest of the cache to itself: 84 MB on a 9800X3D.
- The reservation starts 1–2 s after the game starts and ends when it exits.
- It uses resctrl and changes nothing permanent.

**Measured effect** (9800X3D):

| Test | Without reservation | With reservation |
|---|---|---|
| 64 MB random-access test program, with four background threads thrashing the cache | 128 ns per access | 25.7 ns per access |
| No background load | – | No measurable change |

Game detection is the same as for [L3 per program](sensors.md#l3-cache-per-program).

## Safety guards

Every poll, the daemon checks a list of guards. A guard trips when a sensor stays above its limit
for a set number of polls, and then sets a control to a safe value.

| Guard | Trips when | Action |
|---|---|---|
| 12V-2x6 pin overcurrent | any pin above 8.5 A for 2 polls | NVIDIA power limit to 300 W |
| GPU hotspot overtemp | NVIDIA hotspot above 100 °C for 3 polls | NVIDIA power limit to 300 W |
| GPU memory overtemp | NVIDIA memory above 95 °C for 3 polls | NVIDIA power limit to 300 W |
| AMD GPU hotspot (junction) overtemp | junction above 105 °C for 3 polls | AMD power cap to minimum |
| AMD real power (VRM) over limit (off by default) | VRM-measured power above 450 W for 3 polls | AMD GPU clock offset to minimum |

To change one guard without copying the whole list, use `guard_overrides`:

```json
"guard_overrides": {"AMD real power (VRM) over limit": {"above": 420, "enabled": true}}
```

To replace the whole list, set `guards`. See [configuration.md](configuration.md#guards) for the
format.

The NVIDIA guards watch pin current and temperatures, not board power. A shunt mod doesn't affect
them.

## Per-core Curve Optimizer tuner (`meat3-cotune`)

Finds a stable negative Curve Optimizer value for each core, on Ryzen Zen 3/4/5 with `ryzen_smu`.
It needs `stress-ng`.

```sh
sudo meat3-cotune                                      # every core, best CPPC rank first
sudo meat3-cotune --cores 0,4 --floor -40 --step-time 300
sudo meat3-cotune --status                             # results so far
sudo meat3-cotune --apply                              # apply results (last pass + margin)
sudo meat3-cotune --verify-all 30                      # 30 min all-core stress with results applied
sudo meat3-cotune --save-boot                          # apply results whenever meat3d starts
sudo meat3-cotune --reset                              # forget results (doesn't change CO)
```

| Option | Default | |
|---|---|---|
| `--floor` | -30 | Most negative value to try (limit -50) |
| `--step` | 5 | Step size |
| `--step-time` | 60 s | Test time per step |
| `--margin` | 3 | Added back to the last passing value |
| `--retune` | | Redo cores that already have a result |

**How a step is tested:**
1. One core at a time, its Curve Optimizer value is lowered by one step; the other cores stay where
   they were.
2. That core alone runs single-threaded `stress-ng` with result verification, then a bursty phase
   that exercises boost transitions.
3. The step fails on any of these:
   - a verification error;
   - a non-zero exit from `stress-ng`;
   - a machine check, segfault, general protection fault or soft lockup in the kernel log.

**If the machine crashes:** each step is recorded on disk (`/var/lib/meat3/cotune.json`) before it
runs. If the PC hangs or reboots, the Curve Optimizer goes back to the BIOS values. The next run
marks the value under test as unstable and carries on.

**If a boot fails after applying values:** values saved with `--save-boot` are applied when the
daemon starts. If the machine goes down within 5 minutes of that, the next start skips them and
logs why.

**Quick results aren't proven stable:** a value that passes 60-second steps can still fail in real
use. Before relying on the results day to day, follow up with long `--step-time` runs and
`--verify-all`.

## Binning a CPU per CCD (`meat3-bin`)

Measures a Ryzen CPU one CCD at a time so that two chips can be compared. On X3D chips with two
CCDs, you get separate figures for the V-Cache CCD and the frequency CCD.

**Running it:** put chip A in and run it, then swap to chip B and run it with `--label B`. Keep the
board, BIOS settings, cooler and RAM the same for both. Then compare them.

```sh
sudo meat3-bin --label A              # fused data, boost clocks, voltage/frequency curve (~15 min)
sudo meat3-bin --label A --co         # add the per-core Curve Optimizer search (hours)
sudo meat3-bin --label A --boost      # boost clocks again with the Curve Optimizer results applied
meat3-bin --report A
meat3-bin --compare A B
```

It needs `stress-ng`. Results are saved in `/var/lib/meat3/bin/<label>.json`.

**What each CCD gets:**

| Measurement | How |
|---|---|
| Fused data | Per-core boost limit from the SMU, CPPC preferred-core rank and highest perf, L3 size |
| Best frequency | The clock each core reaches running one thread on its own, and the clock the CCD holds with every core loaded, with voltage, power and temperature at that point |
| Voltage/frequency curve | The frequency limit is lowered step by step, and the voltage the best core (then the whole CCD) asks for is recorded against the clock it actually runs. Lower voltage at the same clock means better silicon |
| Curve Optimizer (`--co`) | The most negative stable value per core, using `meat3-cotune`. With `--boost`, the clocks reached with those values |

**Safety:** the default run only ever lowers clocks, so it can't make the system unstable. If the
CPU reaches 95 °C, it stops. The frequency limits are restored when it finishes, or if it's
interrupted.

**Before you start:**
- **BIOS:** set it to stock or PBO, with Curve Optimizer at 0.
- **No fixed multiplier:** a fixed all-core multiplier makes the CPU ignore frequency limits.
  `meat3-bin` detects this and says so.
- **Same settings for both chips:** for a fair comparison, use the same Curve Optimizer and PBO
  settings for both. If you use PBO's boost override, set the same amount on both.

**How the verdict is decided:** for each CCD, `--compare` compares these, mostly in this order:
- the voltage both chips need at the same clocks (the main measure);
- the Curve Optimizer depth;
- the clocks reached.

It warns if the board or BIOS differed between the two runs, or if either chip was on a fixed
multiplier.

**Running Curve Optimizer on both chips:** `--co` stores its progress in `meat3-cotune`'s state.
Before running `--co` on the second chip, clear the first chip's results with
`sudo meat3-cotune --reset`. `meat3-bin` refuses to mix them up.

**Not yet tested on a dual-CCD chip.** It has been checked on a 9800X3D. On the first dual-CCD
chip, check the report's CCD layout and per-core readings before relying on the verdict.
