# On-screen display

The OSD shows the sensors you choose in two places:
- on the desktop, in a small overlay window;
- in games, through MangoHud.

One hotkey toggles both.

## Choosing sensors

Add sensors on the OSD tab, or right-click a sensor on the Sensors or Cores tab. Order them on the
OSD tab.

These summary sensors keep the display short:
- *Hottest core temperature* and *Highest core voltage* (CPU → Per-core summary);
- *Game L3 in use*;
- the *Game IPC* sensors.

## Hotkey

The default hotkey is **Right Ctrl + F12**. The daemon reads the keyboard itself (evdev), so the
hotkey works system-wide, including in fullscreen games and on Wayland.

**To change it:**
- In the desktop app: OSD tab → *Change…*, then press the new combination.
- In the TUI: press `h`.

A hotkey has 1–4 keys and ends in a non-modifier key, for example Left Alt + O. The change takes
effect immediately.

The hotkey is stored as `osd.hotkey` in `/etc/meat3/config.json`.

## Desktop overlay

A small always-on-top window in a screen corner. Mouse clicks pass through it. Choose the corner,
font, colours and opacity on the OSD tab.

Wayland doesn't let an application place its own window. On a Wayland session, the desktop app
therefore runs the overlay as a separate XWayland process so that it can sit in the chosen corner.
To keep it in the same process, set `MEAT3_OVERLAY_INPROCESS=1`; the compositor then decides where
it goes.

## In games (MangoHud)

MangoHud draws the in-game display. meat³ writes the OSD values to a text file and generates a
MangoHud config that reads that file.

### Setting it up

1. On the OSD tab, click **Generate config**. This writes `~/.config/meat3/MangoHud.conf`.
2. Either:
   - tick **In every game**, then log out and back in once. MangoHud then loads into every Vulkan
     game, which includes every Proton game, with no launch options. It starts hidden; press the
     hotkey to show it.
   - or set the Steam launch options for each game:
     ```
     MANGOHUD_CONFIGFILE=$HOME/.config/meat3/MangoHud.conf mangohud %command%
     ```

**What *In every game* changes:** it writes `MANGOHUD=1` and `MANGOHUD_CONFIGFILE` to
`~/.config/environment.d/90-meat3-mangohud.conf`, which is read when you log in.

**OpenGL games:** these always need `mangohud %command%` as a launch option.

If **Regenerate when items change** is ticked, the config is rewritten whenever you change the OSD
items. MangoHud reloads its config while a game is running.

### Hotkey modes

| Mode | Behaviour |
|---|---|
| MangoHud toggles itself (default) | The generated config sets MangoHud's `toggle_hud` to the same keys as the daemon's hotkey |
| Daemon toggles MangoHud via mangohudctl | The config sets `control=mangohud` and the daemon toggles MangoHud through its control socket. MangoHud's own key stays at its default |

Use one mode or the other, not both. `mangohudctl` only flips the current state, so combining the
modes makes the display toggle twice.

### How it works

The daemon writes one line per OSD item, `<label>: <value> <unit>`, on every poll:
- to `/run/meat3/osd.txt`;
- to `/tmp/meat3/osd.txt`. Steam's game container can see `/tmp` but not `/run`.

The values are written even while the OSD is hidden, because MangoHud keeps track of whether it's
showing.

The daemon only writes the `/tmp` copy if `/tmp/meat3` is a directory that is owned by root and
not writable by other users. If it finds anything else there, it logs a message and writes no copy.

MangoHud has no direct way to display text from a file. Its supported hook for external text is
`exec=`, which needs `legacy_layout=0`. Each OSD item becomes a pair of lines in the config:

```
custom_text=GPU hotspot
exec=a=; b=; if [ -r '/tmp/meat3/osd.txt' ]; then { IFS=: read -r a b; ...; } <'/tmp/meat3/osd.txt'; fi; printf '%s\n' "$b"
```

MangoHud runs every `exec` command in one long-lived shell, every `fps_sampling_period` (500 ms by
default). The generated command uses only shell built-ins, so it never starts a new process. Each
command prints line N of the file.

MangoHud waits up to 50 ms for each command, so keep the in-game OSD to about 8 items.

If a game can't see the file, point the *osd.txt path* setting on the OSD tab at a location it can
see.
