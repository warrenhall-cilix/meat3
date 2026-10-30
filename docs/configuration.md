# Configuration

**Where it lives:**
- **Daemon:** `/etc/meat3/config.json`. Set `MEAT3_CONFIG` to use another path.
- **Desktop app:** its own settings are in `~/.config/meat3/` (see [usage.md](usage.md)).

**When changes take effect:** restart the daemon after editing, with
`sudo systemctl restart meat3d`. The OSD settings are the exception: the desktop app and TUI change
those live.

**How your file combines with the defaults:**
- A key you leave out keeps its default.
- A key you set that holds an object, such as `"osd"` or `"web"`, is merged one level deep with the
  default. So `{"l3_reserve": {"background_mb": 18}}` keeps `enabled: true`.
- `guards` is a list, so setting it replaces the whole list. Use `guard_overrides` to change a single
  guard.

## Reference

### General

| Key | Default | |
|---|---|---|
| `poll_interval_s` | `1.0` | Seconds between sensor polls |
| `nvidia_shunt_factor` | `{}` | Board power correction for shunt-modded NVIDIA cards, by PCI address or `"*"`. Real power = reported × factor. Example: `{"0000:01:00.0": 1.5}` for an R004 on top of an R002 |
| `pin_warn_a` | `8.0` | 12V-2x6 pin current shown amber (A) |
| `pin_crit_a` | `9.0` | 12V-2x6 pin current shown red (A) |

### OSD

| Key | Default | |
|---|---|---|
| `osd.enabled` | `false` | Whether the OSD is showing |
| `osd.hotkey` | `["KEY_RIGHTCTRL", "KEY_F12"]` | evdev key names, 1–4 keys, ending in a non-modifier |
| `osd.items` | `[]` | Sensor ids, in display order |

### CPU

| Key | Default | |
|---|---|---|
| `amd_smu_controls` | `true` | Curve Optimizer and PBO controls through `ryzen_smu` |
| `l3_per_app.enabled` | `true` | L3 use per program (resctrl monitoring) |
| `l3_per_app.top` | `8` | How many of the busiest programs get their own monitoring group |
| `l3_per_app.game_patterns` | `["*.exe"]` | Process names counted as games, as well as Steam titles |
| `l3_reserve.enabled` | `true` | Reserve L3 for the running game |
| `l3_reserve.background_mb` | `12` | L3 that other programs may use while a game runs. Rounded to whole cache ways; at most half the cache |
| `cpu_topdown.enabled` | `true` | Per-core IPC and pipeline stalls |
| `cpu_topdown.per_app` | `"game"` | Per-program counters: `"off"`, `"game"` or `"all"` |
| `cpu_topdown.max_threads` | `16` | Most threads counted per program |
| `cpu_topdown.min_thread_pct` | `1.0` | Only count threads using at least this % of a core |
| `cpu_topdown.max_switches` | `20000` | Skip threads that switch more often than this per second |

### AMD GPU VRM (RDNA4)

See [rdna4.md](rdna4.md) before changing these.

| Key | Default | |
|---|---|---|
| `amdgpu_vrm.enabled` | `true` | Read VRM telemetry over PMBus |
| `amdgpu_vrm.allow_writes` | `false` | Allow the VRM controls |
| `amdgpu_vrm.max_offset_mv` | `50` | Voltage offset limit, ± mV |
| `amdgpu_vrm.max_current_scale` | `1.5` | Highest current reporting scale |
| `amdgpu_vrm.mps_offset_lsb_mv` | unset | MPS voltage offset step size, once verified (5 or 6.25) |
| `amdgpu_vrm.experimental_mps_writes` | `false` | Allow the current scale on MPS controllers |

### Applying settings at boot

`apply_on_start` maps control ids to values. The daemon applies them when it starts:

```json
"apply_on_start": {"cpu0.smu.co_core0": -20, "cpu0.smu.co_core1": -15}
```

`meat3-cotune --save-boot` and `meat3-gputune --save-boot` write this for you.

**Crash protection:** if the machine goes down within 5 minutes of the values being applied, the
next daemon start skips them and logs why. It uses the marker file
`/var/lib/meat3/boot_apply.pending`.

**Finding control ids:** save a profile on the Overclock tab and look in
`~/.config/meat3/profiles/<name>.json`, which maps control ids to values. Sensor ids, which the
guards use, are listed by `sudo meat3d --once`.

### Guards

`guards` is a list of rules the daemon checks every poll:

```json
{"name": "GPU hotspot overtemp", "sensor": "nv*.t.hotspot", "above": 100, "count": 3,
 "control": "nv*.oc.power_limit", "value": 300, "enabled": true}
```

| Field | |
|---|---|
| `sensor` | Sensor id pattern, shell-style (`*`, `?`, `[0-9]`) |
| `above`, `count` | Trips when a matching sensor is above `above` for `count` polls in a row |
| `control`, `value` | Control id pattern and the value to set. `"min"` or `"max"` uses the control's own limit |
| `enabled` | `false` turns the guard off |

When a guard trips, it acts once. It can trip again only after the sensor has dropped back below the
limit.

The default guards are listed in [overclocking.md](overclocking.md#safety-guards).

To change one guard by name without copying the whole list:

```json
"guard_overrides": {"AMD real power (VRM) over limit": {"above": 420, "enabled": true}}
```

### Web UI

The `web` keys are listed in [web.md](web.md#settings).

## Example

```json
{
  "nvidia_shunt_factor": {"0000:01:00.0": 1.5},
  "osd": {"hotkey": ["KEY_LEFTALT", "KEY_O"]},
  "l3_reserve": {"background_mb": 18},
  "guard_overrides": {"GPU hotspot overtemp": {"above": 95}},
  "web": {"listen": "127.0.0.1"}
}
```
