# Development

## Layout

```
meat3/
  daemon/      meat3d: polls backends, serves the socket, runs guards, hotkey, OSD file
  backends/    one module per kind of hardware; discovered automatically
  core/        data model, config, formatting, updater, mock daemon
  gui/         Qt desktop app (PySide6)
  osd/         MangoHud config generation, hotkey parsing
  web/         read-only web UI and /metrics
  tui.py       curses UI
  cotune.py    meat3-cotune
  gputune.py   meat3-gputune
  skutable.py  meat3-skutable
  cpubin.py    meat3-bin
kmod/          meat3_smu (RDNA4 metrics) and the custom SKU amdgpu patch
packaging/     installer, updater, systemd units, default config, Grafana dashboard
tools/         dashboard generator, RDNA4 helpers, screenshot and validation scripts
tests/         unit tests
```

## How it fits together

`meat3d` runs as root. When it starts, it:
1. imports every module in `meat3/backends/` that doesn't start with `_`;
2. calls `detect()` on each `Backend` subclass;
3. keeps the instances that detect returns.

Then, once each `poll_interval_s`, it:
1. collects `sensors()` and `controls()` from every backend;
2. updates min/max/avg;
3. checks the guards;
4. writes the OSD file.

**Clients:** the desktop app, TUI and web UI connect to `/run/meat3/meat3.sock` and exchange JSON
lines. The protocol is in [PROTOCOL.md](../PROTOCOL.md).

**Permissions:** the socket is group `meat3`, mode 0660. The daemon only accepts requests that
change something from root or members of `meat3`. It checks this with `SO_PEERCRED`.

## Writing a backend

Subclass `meat3.core.model.Backend`:

| Method | Rules |
|---|---|
| `detect()` (classmethod) | Return one instance per device found, or `[]`. Must never raise and never write to hardware. Missing permissions or modules mean returning `[]` or a reduced backend |
| `sensors()` | Read-only, called about once a second. Should finish in under 200 ms. Keep a row with `value=None` when a reading is missing this poll, rather than dropping it |
| `controls()` | Read-only. Report the current value read back from the hardware, with `min`/`max`, and `available=False` plus a `hint` when a control can't be used |
| `apply(id, value)` | Must check type and range itself and refuse anything outside the advertised limits. Returns `(ok, message)` |

**Sensor and control ids** must be stable and unique, for example `nv0.clk.gpc` or
`net.enp14s0.rx`. Saved OSD items, graphs, profiles, guards and Prometheus series all refer to
them.

**Fields:**
- `group` is the tree path, with `/` between levels.
- `hint` is the tooltip. Use it to say where the value comes from and anything the reader should
  know about it.

**Testing a backend:** constructors take root paths (`sys_root`, `proc_root`), so tests can point
them at a fake tree in a temporary directory. See `tests/test_network.py` or
`tests/test_storage.py`.

## Running from the checkout

```sh
python3 -m meat3.core.mockd --socket /tmp/meat3-mock.sock     # fake hardware, no root needed
python3 -m meat3.gui --socket /tmp/meat3-mock.sock
python3 -m meat3.tui --socket /tmp/meat3-mock.sock
sudo python3 -m meat3.daemon --once                           # real hardware: poll once and print
```

`MEAT3_SOCKET` works in place of `--socket`.

## Tests

```sh
python3 -m unittest discover -s tests                         # 221 tests, a few seconds
QT_QPA_PLATFORM=offscreen python3 tests/test_gui_smoke.py     # GUI smoke test
```

The GUI smoke test also writes the screenshots in `docs/screenshots/`.

**Other scripts:**
- `tools/shots_live.py`: takes screenshots against a running daemon instead of the mock.
- `tools/validate_cpu_amd.py` (run as root): cross-checks SMU power table fields against RAPL, MSRs
  and k10temp.

## PySide6 on Python 3.10/3.11

On Python 3.12 and later, any recent PySide6 works; 6.12 on 3.14 has been tested.

On 3.10 and 3.11, use `pip install 'pyside6==6.9.*'`. PySide6 6.12 on 3.10 releases `None` too
many times on every void call, and later crashes with `none_dealloc` or `free(): invalid pointer`.
The desktop app detects this and installs a workaround for the first problem, but not the second.

## What the desktop app relies on from the daemon

These are in addition to [PROTOCOL.md](../PROTOCOL.md):

**osd.txt:**
- One line per OSD item, in `osd_get.items` order, formatted `<label>: <value unit>` by
  `meat3.core.fmt.osd_line`.
- Labels never contain `:` or `#`.
- The file is replaced atomically and is empty while the OSD is off.

**Requests:**
- `osd_get` may include `"file"`, the osd.txt path, which the MangoHud generator then uses.
- Failed or unknown requests return `{"ok": false, "msg": "..."}`.
- `apply` on an `action` control is sent `"value": true`.
- After a successful `apply`, the next `snapshot` shows the new value. A control's `available` and
  `hint` may change between snapshots; for example, fan speed only becomes available in manual
  mode.

**Connections and ids:**
- Connections stay open across requests, and several clients can be connected at once.
- Sensor ids and their order are stable; the tree adds rows in first-seen order.

## Commits

Run the tests before pushing. The update buttons pull with `--ff-only`, so never rewrite history
on `master`.
