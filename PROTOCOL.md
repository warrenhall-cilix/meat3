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
| `{"cmd":"osd_set","enabled":bool?,"items":[...]?,"hotkey":"RIGHTCTRL+F12" or ["KEY_…",...]?}` | `{"ok":true}`; a new hotkey is live at once (1–4 keys, last one not a modifier) |
| `{"cmd":"osd_toggle"}` | `{"ok":true,"enabled":bool}` |
| `{"cmd":"update_status"}` | `{"ok":true,"installed":"<git describe>","behind":int\|null,"commits":["<hash> <subject>",...],"checking":bool,"running":bool,"log":[lines],"error":"...","can_update":bool}`. Starts a background GitHub check if the last one is over 6 h old. |
| `{"cmd":"update_check"}` | `{"ok":true}`; fetches in the background, poll `update_status` |
| `{"cmd":"update_start"}` | `{"ok":bool,"msg":"..."}`; pulls and reinstalls in a separate systemd unit, which restarts meat3d. Only the owner of the git checkout (or root) may start it. |
| `{"cmd":"experimental_get"}` | `{"ok":true,"accepted":bool,"hard_max_scale":1.5,"amdgpu_vrm":{...},"guards":[{name,sensor,above,count,control,value,enabled}],"user_guards_list":bool,"smufw":{"devices":[{pci,pmfw_hex,vrm_bus_open,installed,pinned,reboot_needed,note}],"hook":bool},"smufw_run":{action,running,rc,log},"can_write":bool}`. Read-only. |
| `{"cmd":"experimental_set","settings":{"<key>":v},"guards":{"<guard name>":{"above":n,"enabled":b}},"reset_guards":bool?}` | `{"ok":bool,...}` (same body as `experimental_get` on success). Whitelisted keys only: `experimental.accepted`, `amdgpu_vrm.{allow_writes,restore_on_stop,experimental_mps_writes,max_tbp_scale,max_tdc_scale,max_offset_mv,mps_offset_lsb_mv}`. Writes the user's config file, re-reads it in the daemon and the backends (no restart). |
| `{"cmd":"smufw_run","action":"pin"|"restore"}` | `{"ok":bool,"msg":"..."}`; runs `meat3-smufw` as root in the background; progress in `experimental_get`'s `smufw_run`. Needs the notice accepted. |

`S` is a Sensor dict (see `meat3/core/model.py`) plus `"min"`, `"max"` and `"avg"`. All three are floats or null, kept by the daemon since start or since the last `reset_stats`.

`C` is a Control dict.

## OSD

The daemon keeps the OSD state and writes the selected sensors as text lines to `/run/meat3/osd.txt` every poll, and a copy to `/tmp/meat3/osd.txt`; Steam's game container can see `/tmp` but not `/run`. The values are written even while the OSD is off, because MangoHud keeps its own shown/hidden state:

- **In-game:** the generated MangoHud config reads that file.
- **Desktop:** the GUI's overlay window reads the socket.

The hotkey (evdev, read by the daemon) flips `enabled`. It also toggles MangoHud through `mangohudctl` when available.
