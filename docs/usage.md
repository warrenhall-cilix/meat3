# Using meat³

All front ends show the same data from the daemon. Each one can run on its own or alongside the
others.

## Desktop app (`meat3`)

```sh
meat3                      # normal start
meat3 --hidden             # start in the system tray
meat3 --theme system       # dark, light, liquid or system
```

When a system tray is available, closing the window (or Ctrl+W) hides it to the tray. To exit,
choose Quit from the File menu or the tray menu.

**Profiles:** on the Overclock tab, *Save as…* stores the current values under a name, and
*Apply profile* sets them again.

### Sensors

Every sensor in a tree, grouped by device, with current, minimum, maximum and average values. The
daemon keeps the statistics since it started. *Reset min/max/avg* on the toolbar clears them, and
*Favourites only* filters the tree.

Hover over a row to see where the value comes from (sysfs file, NVML call, register) and how to
read it. Right-click a row to:
- mark it as a favourite;
- add it to the OSD;
- add it to the Graphs tab.

### Graphs

A grid of live charts for the sensors you choose.

**Adding sensors:** tick them in the tree on the left, or pick a preset. The presets are Overview,
CPU cores, CPU clocks, CPU stalls, GPU and Network.

**Each chart:** shows the current value and the min/avg/max over the chosen window (1–30 minutes).
Percentages use a fixed 0–100 axis; everything else scales to fit.

**Hovering:** shows a crosshair on every chart at the same moment.

**Combining:** drag one chart onto another with the same unit, for example GPU, hotspot and memory
temperatures together. Right-click a chart to split, move or remove it.

**History:** the last 30 minutes are kept in memory, so a sensor you add shows recent history
straight away. The layout is saved between sessions.

### Cores

One row per CPU core:
- effective and active clock;
- requested voltage;
- temperature, power and usage;
- C0/CC6 residency;
- SMU maximum frequency and CPPC rank.

The highest value this session sits next to clock, voltage, temperature and power. The fastest and
hottest cores are shown in bold.

Below the core rows is the **Apps** table: L3 cache use and pipeline counters for the busiest
programs and the running game. See [sensors.md](sensors.md#l3-cache-per-program).

Per-core voltage is the voltage the SMU requests for that core, not a measurement at the socket. It
is useful for comparing cores and for seeing Curve Optimizer changes.

### Overclock

The controls for every detected device, with their allowed ranges. See
[overclocking.md](overclocking.md).

Profiles are saved in `~/.config/meat3/profiles/<name>.json` as `{control id: value}`.

### OSD

Choose which sensors the on-screen display shows, set the hotkey and generate the MangoHud config.
See [osd.md](osd.md).

### Themes

Choose one under View → Theme:

| Theme | |
|---|---|
| Dark | Default |
| Light | |
| Liquid Ass | Translucent "glass" panels over a painted backdrop |
| System (follow KDE) | Uses your Plasma widget style, colour scheme and accent. Changes in System Settings apply straight away. Outside KDE this is the plain Qt palette |

The desktop app's own settings are in `~/.config/meat3/meat3-gui.conf`: theme, favourites and
overlay style.

## Terminal UI (`meat3-tui`)

```sh
meat3-tui                  # works over SSH; needs only Python's curses
meat3-tui --interval 2     # refresh every 2 s
```

| Key | Action |
|---|---|
| `1` | Overview: per-device panels with bars |
| `2` | Sensors: everything, with min/max/avg. `/` filters |
| `3` | Cores: per-core table and L3 by program |
| `4` | Overclock: select a control and press Enter. Risky values ask you to type `yes` |
| `Tab` | Next view |
| `o` | Toggle the OSD |
| `h` | Change the OSD hotkey |
| `r` | Reset min/max/avg |
| `u` | Check for and install updates |
| `q` | Quit |

## Web UI (`meat3-web`)

A read-only view in the browser. It's off by default. See [web.md](web.md).
