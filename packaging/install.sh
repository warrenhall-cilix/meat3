#!/bin/bash
# meat³ installer. Run as your normal user from the repo root:  ./packaging/install.sh
#   --no-deps   skip distro package installation
#   --uninstall remove meat3 (keeps /etc/meat3/config.json)
#   --kmod      build the meat3_smu kernel module even without an RDNA4 GPU present
#   --no-kmod   don't (re)build the meat3_smu kernel module
set -euo pipefail
PREFIX=/opt/meat3
SRC="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${SUDO_USER:-$USER}"

if [[ "${1:-}" == "--uninstall" ]]; then
    sudo systemctl disable --now meat3d.service 2>/dev/null || true
    sudo rm -rf "$PREFIX" /etc/systemd/system/meat3d.service /usr/local/bin/meat3 /usr/local/bin/meat3d /usr/local/bin/meat3-cotune /usr/local/bin/meat3-tui \
        /usr/local/bin/meat3-gputune /usr/local/bin/meat3-skutable /usr/share/applications/meat3.desktop /etc/modules-load.d/meat3-smu.conf
    if command -v dkms >/dev/null && dkms status meat3_smu 2>/dev/null | grep -q meat3_smu; then
        sudo dkms remove meat3_smu/0.1 --all || true
    fi
    sudo rm -rf /usr/src/meat3_smu-0.1
    sudo systemctl daemon-reload
    echo "meat3 removed (config kept in /etc/meat3)"; exit 0
fi

WANT_KMOD=0
[[ " $* " == *" --kmod "* ]] && WANT_KMOD=1
for ip in /sys/class/drm/card*/device/ip_discovery/die/0; do      # RDNA4 dGPU = GC 12.x + MP1 14.x
    [[ -f "$ip/GC/0/major" && -f "$ip/MP1/0/major" ]] || continue
    [[ "$(cat "$ip/GC/0/major")" == 12 && "$(cat "$ip/MP1/0/major")" == 14 ]] && WANT_KMOD=1
done
[[ " $* " == *" --no-kmod "* ]] && WANT_KMOD=0

if [[ " $* " != *" --no-deps "* ]]; then
    if command -v pacman >/dev/null; then
        sudo pacman -S --needed --noconfirm python pyside6 python-evdev python-psutil lm_sensors mangohud i2c-tools
        if [[ $WANT_KMOD == 1 ]]; then
            sudo pacman -S --needed --noconfirm dkms
            for k in $(pacman -Qq | grep -E '^linux(-[a-z0-9-]+)?$' | grep -v -- '-headers$'); do
                pacman -Si "$k-headers" >/dev/null 2>&1 && sudo pacman -S --needed --noconfirm "$k-headers"
            done
        fi
        pacman -Qq nvidia-utils >/dev/null 2>&1 && sudo pacman -S --needed --noconfirm python-nvidia-ml-py || true
        echo "Optional (AMD CPU Infinity Fabric / SMU telemetry): install ryzen_smu-dkms-git from the AUR"
    elif command -v apt-get >/dev/null; then
        sudo apt-get install -y python3 python3-pyside6.qtwidgets python3-evdev python3-psutil lm-sensors mangohud i2c-tools || true
        if [[ $WANT_KMOD == 1 ]]; then sudo apt-get install -y dkms "linux-headers-$(uname -r)" || true; fi
        command -v nvidia-smi >/dev/null && (sudo apt-get install -y python3-pynvml || pip3 install --user nvidia-ml-py) || true
    elif command -v dnf >/dev/null; then
        sudo dnf install -y python3 python3-pyside6 python3-evdev python3-psutil lm_sensors mangohud i2c-tools || true
        if [[ $WANT_KMOD == 1 ]]; then sudo dnf install -y dkms kernel-devel || true; fi
        command -v nvidia-smi >/dev/null && pip3 install --user nvidia-ml-py || true
    else
        echo "Unknown distro: install PySide6, python-evdev, psutil, (nvidia-ml-py for NVIDIA) manually"
    fi
fi

echo "== installing to $PREFIX"
sudo rm -rf "$PREFIX"
sudo mkdir -p "$PREFIX"
sudo cp -r "$SRC/meat3" "$SRC/README.md" "$SRC/PROTOCOL.md" "$PREFIX/"
sudo find "$PREFIX" -name "__pycache__" -prune -exec rm -rf {} +
# --no-optional-locks: as root (the update button) describe --dirty would rewrite .git/index root-owned
VER="$(git --no-optional-locks -c safe.directory="$SRC" -C "$SRC" describe --always --dirty 2>/dev/null || echo unknown)"
echo "$VER" | sudo tee "$PREFIX/VERSION" >/dev/null
if [[ -d "$SRC/.git" ]]; then                  # lets the GUI/TUI update button find this checkout
    echo "$SRC" | sudo tee "$PREFIX/SOURCE" >/dev/null
    sudo install -m 755 "$SRC/packaging/update.sh" "$PREFIX/update.sh"
fi

sudo tee /usr/local/bin/meat3d >/dev/null <<EOF
#!/bin/sh
cd $PREFIX && exec /usr/bin/python3 -m meat3.daemon "\$@"
EOF
sudo tee /usr/local/bin/meat3 >/dev/null <<EOF
#!/bin/sh
cd $PREFIX && exec /usr/bin/python3 -m meat3.gui "\$@"
EOF
sudo tee /usr/local/bin/meat3-cotune >/dev/null <<EOF
#!/bin/sh
cd $PREFIX && exec /usr/bin/python3 -m meat3.cotune "\$@"
EOF
sudo tee /usr/local/bin/meat3-tui >/dev/null <<EOF
#!/bin/sh
cd $PREFIX && exec /usr/bin/python3 -m meat3.tui "\$@"
EOF
sudo tee /usr/local/bin/meat3-gputune >/dev/null <<EOF
#!/bin/sh
cd $PREFIX && exec /usr/bin/python3 -m meat3.gputune "\$@"
EOF
sudo tee /usr/local/bin/meat3-skutable >/dev/null <<EOF
#!/bin/sh
cd $PREFIX && exec /usr/bin/python3 -m meat3.skutable "\$@"
EOF
sudo chmod 755 /usr/local/bin/meat3 /usr/local/bin/meat3d /usr/local/bin/meat3-cotune /usr/local/bin/meat3-tui \
    /usr/local/bin/meat3-gputune /usr/local/bin/meat3-skutable

echo "== group 'meat3' (allowed to apply overclocks / change OSD)"
getent group meat3 >/dev/null || sudo groupadd --system meat3
sudo usermod -aG meat3 "$USER_NAME"

sudo mkdir -p /etc/meat3
[[ -f /etc/meat3/config.json ]] || sudo cp "$SRC/packaging/config.default.json" /etc/meat3/config.json

echo "== kernel modules for sensors (loaded at boot)"
MODS=""
for m in i2c-dev msr amd_uncore ryzen_smu it87 nct6775; do
    modinfo "$m" >/dev/null 2>&1 && MODS="$MODS$m\n"
done
printf "$MODS" | sudo tee /etc/modules-load.d/meat3.conf >/dev/null
modinfo it87 >/dev/null 2>&1 && echo "options it87 ignore_resource_conflict=1" | sudo tee /etc/modprobe.d/meat3-it87.conf >/dev/null
for m in $(printf "$MODS"); do sudo modprobe "$m" 2>/dev/null || true; done

if [[ $WANT_KMOD == 1 ]]; then
    echo "== meat3_smu kernel module (RDNA4 hidden firmware metrics, read-only)"
    if command -v dkms >/dev/null; then
        if dkms status meat3_smu 2>/dev/null | grep -q meat3_smu; then
            sudo dkms remove meat3_smu/0.1 --all >/dev/null 2>&1 || true
        fi
        sudo rm -rf /usr/src/meat3_smu-0.1
        sudo cp -r "$SRC/kmod/meat3_smu" /usr/src/meat3_smu-0.1
        if sudo dkms install meat3_smu/0.1; then
            echo meat3_smu | sudo tee /etc/modules-load.d/meat3-smu.conf >/dev/null
            sudo modprobe meat3_smu || echo "   (modprobe meat3_smu failed; see dmesg)"
        else
            echo "   dkms build failed (kernel headers installed?); hidden metrics stay off"
        fi
    else
        echo "   dkms not found; install it and re-run with --kmod for the hidden RDNA4 metrics"
    fi
fi

sudo cp "$SRC/packaging/meat3d.service" /etc/systemd/system/meat3d.service
sudo cp "$SRC/packaging/meat3.desktop" /usr/share/applications/meat3.desktop
sudo systemctl daemon-reload
sudo systemctl enable meat3d.service
sudo systemctl restart meat3d.service     # restart, not just start: a running daemon keeps old code
sleep 2
systemctl --no-pager --lines=5 status meat3d.service || true
echo
echo "Installed meat3 $VER"
echo "Done. Log out and back in once (group membership), then run: meat3"
echo "OSD hotkey: Right Ctrl + F12. In-game overlay: see README (MangoHud)."
