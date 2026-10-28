# meat³ daemon protocol

Socket: `/run/meat3/meat3.sock` (stream Unix socket). The daemon runs as root. The socket is mode 0660, owned by group `meat3`; the installer adds the desktop user to that group.

Framing: one JSON object per line, in both directions. Every request gets exactly one response line.

## Requests

| Request | Response |
|---|---|
| `{"cmd":"hello"}` | `{"ok":true,"version":"0.1","host":"...","backends":[{"kind":"gpu","name":"..."}]}` |
| `{"cmd":"snapshot"}` | `{"ok":true,"t":<unix time>,"sensors":[S...],"controls":[C...]}` |
| `{"cmd":"apply","id":"<control id>","value":<v>}` | `{"ok":bool,"msg":"..."}` |
| `{"cmd":"reset_stats"}` | `{"ok":true}` (clears min/max/avg) |
| `{"cmd":"osd_get"}` | `{"ok":true,"enabled":bool,"items":["sensor id",...],"hotkey":"KEY_RIGHTCTRL+KEY_F12"}` |
| `{"cmd":"osd_set","enabled":bool?,"items":[...]?}` | `{"ok":true}` |
| `{"cmd":"osd_toggle"}` | `{"ok":true,"enabled":bool}` |

`S` is a Sensor dict (see `meat3/core/model.py`) plus `"min"`, `"max"` and `"avg"`. All three are floats or null, kept by the daemon since start or since the last `reset_stats`.

`C` is a Control dict.

## OSD

The daemon keeps the OSD state and writes the selected sensors as text lines to `/run/meat3/osd.txt` every poll:

- **In-game:** the generated MangoHud config reads that file.
- **Desktop:** the GUI's overlay window reads the socket.

The hotkey (evdev, read by the daemon) flips `enabled`. It also toggles MangoHud through `mangohudctl` when available.
