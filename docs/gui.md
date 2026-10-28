# meat³ GUI, desktop OSD and in-game OSD

## Running

    python3 -m meat3.core.mockd --socket /tmp/meat3-mock.sock     # fake hardware
    python3 -m meat3.gui --socket /tmp/meat3-mock.sock             # or MEAT3_SOCKET=...; default /run/meat3/meat3.sock
    python3 -m meat3.gui --light | --dark | --hidden               # theme, start in tray

Smoke test (offscreen, also writes `docs/screenshots/*.png`):

    QT_QPA_PLATFORM=offscreen python3 tests/test_gui_smoke.py

Requirements: PySide6. On Python >= 3.12 any recent PySide6 works (tested with 6.12.0 on 3.14).
On Python 3.10/3.11 use `pip install 'pyside6==6.9.*'`: PySide6 6.12.0 on 3.10 over-releases
`None` on every void call and later crashes with `none_dealloc` or `free(): invalid pointer`. The
GUI detects the leak and installs a guard, but that does not fix the second crash.

GUI settings go in `~/.config/meat3/meat3-gui.conf`: favourites, theme, overlay style and MangoHud
options. OC profiles go in `~/.config/meat3/profiles/<name>.json` as a flat `{control id: value}`
map. `XDG_CONFIG_HOME` is honoured.

## In-game OSD (MangoHud)

On the OSD tab, click **Generate config**. This writes `~/.config/meat3/MangoHud.conf` by
default. Then launch the game with:

    MANGOHUD_CONFIGFILE=~/.config/meat3/MangoHud.conf mangohud %command%     # Steam launch options

How it works: MangoHud cannot read a text file directly. Its supported hook for external text is
`exec=`, which needs `legacy_layout=0`. Each meat³ item becomes a `custom_text=<label>` line, which
starts a row, followed by an `exec=` line that prints the value in the next column. MangoHud runs
every `exec` in one long-lived `/bin/sh` on its hw-info thread, every `fps_sampling_period` (500 ms
by default). The generated command uses only shell builtins, so it never forks:

    a=; b=; if [ -r '/run/meat3/osd.txt' ]; then { IFS=: read -r a b; IFS=: read -r a b; } <'/run/meat3/osd.txt'; fi; printf '%s\n' "$b"

This prints line N of `osd.txt`. MangoHud waits a fixed 50 ms for each exec, so keep the OSD to
about 8 items or fewer. MangoHud watches its config file with inotify, so regenerating the config
(or ticking "Regenerate when items change") updates a running game.

Hotkey: by default the generated config sets `toggle_hud` to the daemon's hotkey translated to an
X keysym, e.g. `KEY_RIGHTCTRL+KEY_F12` becomes `Control_R+F12`. It does not set `control=`, so a
`mangohudctl` call from the daemon finds no socket and cannot double-toggle. The alternative
"Daemon toggles MangoHud via mangohudctl" mode writes `control=mangohud` and leaves MangoHud's own
key at its default. Do not combine the two: mangohudctl's `hud` command is a pure toggle.

Steam's pressure-vessel container may not see the host's `/run/meat3`. If that happens, point
"osd.txt path" at a copy the daemon mirrors under `$HOME`.

## Desktop overlay

This is a frameless, translucent, always-on-top, click-through Qt window
(`WindowTransparentForInput`). It is placed in the chosen corner of the primary screen and follows
the daemon's `enabled` flag, which the GUI polls with `osd_get`. On Wayland, compositors ignore
client positioning for normal toplevels, so the corner placement is only reliable on X11/XWayland
(run with `QT_QPA_PLATFORM=xcb`) until a layer-shell backend is added.

## What the GUI expects from the daemon (beyond PROTOCOL.md)

- `osd.txt`: one line per OSD item in `osd_get.items` order, formatted
  `<label>: <value unit>` via `meat3.core.fmt.osd_line`. Labels contain no `:` or `#`. The file is
  replaced atomically (write a temp file, then `rename`) and is empty while the OSD is disabled.
- `osd_get` may include `"file": "<osd.txt path>"`. The MangoHud generator uses it when present.
- Failed requests return `{"ok": false, "msg": "..."}`; unknown commands do the same.
- `apply` on an `action` control is sent `"value": true`.
- After a successful `apply`, the next `snapshot` shows the new `Control.value`. `available` and
  `hint` may change between snapshots (for example, fan speed only becomes available in manual
  mode).
- The connection stays open across requests, several clients can connect at once, and sensor ids
  and order stay stable (the tree inserts rows in first-seen order).
