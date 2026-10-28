# meat³ on an RX 9070 XT: power, voltage and current

None of this has been tested on a real 9070 XT yet. Go one step at a time and keep an eye on the
sensors.

## 1. Install

```
tar xf meat3-0.1.tar.gz && cd meat3 && ./packaging/install.sh
```

On a 9070 XT the installer also builds the `meat3_smu` module (hidden firmware metrics) with DKMS.
Log out and back in, then run `meat3` (GUI) or `meat3-tui`.

## 2. Look before touching anything

Open the Graphs tab and pick the **GPU** preset, then play something heavy for a few minutes:

| Sensor | What it tells you |
|---|---|
| Real power (VRM measured) | true power draw, still right with the shunt bridge |
| Board power sensing | says BYPASSED if the bridge is working |
| VDD_GFX current (real) | GPU core amps; the firmware limit is 330 A |
| Limited by TDC GFX % | how often the 330 A current limit is holding the card back |
| Held at voltage max (Vmax) % | how often it's out of voltage instead |
| VDD_GFX VRM temperature | the regulator's own temperature; the firmware throttles at 105 °C |

Then run `sudo bash tools/collect_amdgpu.sh` and send the `.tar.gz` back. It shows whether the
regulator controls will work on this card.

## 3. Set a real-power safety limit

With the bridge in place the power slider does nothing. Add this to `/etc/meat3/config.json` (pick
your own number):

```json
"guard_overrides": {"AMD real power (VRM) over limit": {"above": 420, "enabled": true}}
```

Then run `sudo systemctl restart meat3d`. If real power stays above the limit for 3 seconds, meat3
drops the GPU clock offset to its minimum.

## 4. Undervolt first

`sudo meat3-gputune` while a demanding game or benchmark loops. Lower voltage means less current at
the same clock, so it buys clocks under the 330 A limit. Then run `sudo meat3-gputune --apply` and
`--save-boot`.

## 5. Raise the limits (dangerous, all reset at power-off)

Enable regulator writes in `/etc/meat3/config.json`:

```json
"amdgpu_vrm": {"allow_writes": true, "max_offset_mv": 50, "max_current_scale": 1.5}
```

Then `sudo systemctl restart meat3d`, and use Overclock → **VRM voltage offsets**:

- **Current reporting scale** (GFX): 1.2 means the firmware sees 1/1.2 of the real current, so the
  330 A limit acts like ~396 A. Go up 0.05 at a time and watch the VRM temperature. meat3 still shows
  the real amps. The regulator's own over-current protection is also raised by the same factor.
- **Voltage offset** (GFX): adds real voltage on top of the firmware's 1.2 V ceiling. Go 5 mV at a time.
- **Load line**: lower means less voltage sag under load.
- **Restore factory settings** puts all three back, and so does a full power-off.

The safer alternative for current: `sudo meat3-skutable --tdc-gfx 380 --write`. This asks the
firmware to raise the limit itself, so protection stays intact. It needs the patched amdgpu from
`tools/build_patched_amdgpu.sh`, and the firmware may refuse; the kernel log will say.
