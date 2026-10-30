# Installing meat³

## Requirements

- **Linux and Python:** Linux with systemd, Python 3.10 or newer.
- **Desktop app:** PySide6. On Python 3.10/3.11 use PySide6 6.9 (see
  [development.md](development.md#pyside6-on-python-310311)).
- **Daemon:** python-evdev and psutil. Add nvidia-ml-py if you have an NVIDIA card.
- **Optional:** lm_sensors, i2c-tools, smartmontools and MangoHud.

The installer installs these with pacman, apt or dnf.

## Install

Run the installer as your normal user from the checkout. It uses `sudo` where it needs to.

```sh
git clone https://github.com/warrenhall-cilix/meat3.git
cd meat3
./packaging/install.sh
```

It does the following:
- **Program files:** copies meat³ to `/opt/meat3` and creates the commands in `/usr/local/bin`:
  `meat3`, `meat3d`, `meat3-tui`, `meat3-web`, `meat3-cotune`, `meat3-gputune` and
  `meat3-skutable`.
- **Daemon:** installs and starts `meat3d.service`.
- **Group:** creates the `meat3` group and adds you to it. Log out and back in once for this to
  take effect.
- **Config:** writes `/etc/meat3/config.json`, unless it already exists.
- **Sensor modules:** loads the sensor kernel modules that exist on your kernel at boot, via
  `/etc/modules-load.d/meat3.conf`: `i2c-dev`, `msr`, `amd_uncore`, `ryzen_smu`, `it87`,
  `nct6775`, `nct6683` and `drivetemp`.
- **RDNA4 module:** if an RDNA4 GPU is present, builds the `meat3_smu` module with DKMS
  (see [rdna4.md](rdna4.md)).
- **Web token:** creates a login token for the web UI. The web UI itself stays off unless you ask
  for it.

### Installer options

| Option | Effect |
|---|---|
| `--web` | Also enable the read-only web UI on port 8733 ([web.md](web.md)) |
| `--headless` | No desktop app or MangoHud; for servers. Remembered by later installs and updates |
| `--desktop` | Undo `--headless` |
| `--kmod` | Build `meat3_smu` even without an RDNA4 GPU present |
| `--no-kmod` | Don't build or rebuild `meat3_smu` |
| `--no-deps` | Don't install distro packages |
| `--uninstall` | Remove meat³. Keeps `/etc/meat3/config.json` |

**Servers and Proxmox hosts:** run `./packaging/install.sh --headless --web`. Running it as root
is fine. On Debian and Proxmox, run `apt install git` first.

## Optional extras

These add data. meat³ works without them and shows the affected rows as unavailable.

| Package | Adds |
|---|---|
| `ryzen_smu-dkms-git` (AUR) | Ryzen FCLK/UCLK/MCLK, SVI3 voltages, per-core power and voltage, PPT/TDC/EDC, Curve Optimizer and PBO controls |
| `it87-dkms-git` (AUR) | Fans and voltages on Gigabyte and other boards with ITE Super I/O chips |
| Patched open NVIDIA kernel module | NVVDD/MSVDD voltages and limits, XBAR/SYS clock offsets, Vrel offsets. Everything else on NVIDIA uses stock NVML |
| `amdgpu.ppfeaturemask=0xfff7ffff` kernel parameter | AMD GPU Overdrive controls (clock, memory and voltage offsets, fan curve) |

## Updating

Use any of these:
- **Desktop app:** Help → Check for updates. A button in the status bar also appears when
  updates are available.
- **TUI:** press `u` in `meat3-tui`.
- **Terminal:** run `~/meat3/packaging/update.sh`, or the update script in your own checkout.

The update does the following:
1. Pulls from GitHub with `git pull --ff-only`.
2. Reinstalls and restarts `meat3d`.
3. Rebuilds `meat3_smu` only if its source changed.
4. Re-checks distro packages only if the installer changed.

Reopen the desktop app afterwards to load the new version.

The buttons in the desktop app and TUI only work for the user who owns the git checkout, because
the daemon runs that checkout's installer as root. If you've edited files in the checkout, the pull
fails and the update stops. Stash or discard your changes, then try again.

## Uninstalling

```sh
./packaging/install.sh --uninstall
```

This removes:
- the services;
- `/opt/meat3`;
- the commands;
- the desktop entry;
- the `meat3_smu` DKMS module.

It keeps:
- `/etc/meat3/` (config and web token);
- `/etc/modules-load.d/meat3.conf`;
- `/etc/modprobe.d/meat3-it87.conf`.

Delete those by hand if you want them gone.

## Troubleshooting

- **The desktop app says the daemon isn't running:** run `systemctl status meat3d` and
  `journalctl -u meat3d -b`.
- **Overclock controls are greyed out or refuse to apply:**
  - Check that `id` lists `meat3`. If not, log out and back in.
  - Check the control's tooltip, which says what's missing. Examples: `ryzen_smu` not loaded, or no
    `ppfeaturemask`.
- **To see what the daemon detects without starting the service:** run `sudo meat3d --once`. It
  polls once and prints every sensor.
