#!/bin/bash
# meat³ installer. Run as your normal user from the repo root:  ./packaging/install.sh
#   --no-deps   skip distro package installation
#   --uninstall remove meat3 (keeps /etc/meat3/config.json)
set -euo pipefail
PREFIX=/opt/meat3
SRC="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${SUDO_USER:-$USER}"

if [[ "${1:-}" == "--uninstall" ]]; then
    sudo systemctl disable --now meat3d.service 2>/dev/null || true
    sudo rm -rf "$PREFIX" /etc/systemd/system/meat3d.service /usr/local/bin/meat3 /usr/local/bin/meat3d \
        /usr/share/applications/meat3.desktop
    sudo systemctl daemon-reload
    echo "meat3 removed (config kept in /etc/meat3)"; exit 0
fi

if [[ "${1:-}" != "--no-deps" ]]; then
    if command -v pacman >/dev/null; then
        sudo pacman -S --needed --noconfirm python pyside6 python-evdev python-psutil lm_sensors mangohud i2c-tools
        pacman -Qq nvidia-utils >/dev/null 2>&1 && sudo pacman -S --needed --noconfirm python-nvidia-ml-py || true
        echo "Optional (AMD CPU Infinity Fabric / SMU telemetry): install ryzen_smu-dkms-git from the AUR"
    elif command -v apt-get >/dev/null; then
        sudo apt-get install -y python3 python3-pyside6.qtwidgets python3-evdev python3-psutil lm-sensors mangohud i2c-tools || true
        command -v nvidia-smi >/dev/null && (sudo apt-get install -y python3-pynvml || pip3 install --user nvidia-ml-py) || true
    elif command -v dnf >/dev/null; then
        sudo dnf install -y python3 python3-pyside6 python3-evdev python3-psutil lm_sensors mangohud i2c-tools || true
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

sudo tee /usr/local/bin/meat3d >/dev/null <<EOF
#!/bin/sh
cd $PREFIX && exec /usr/bin/python3 -m meat3.daemon "\$@"
EOF
sudo tee /usr/local/bin/meat3 >/dev/null <<EOF
#!/bin/sh
cd $PREFIX && exec /usr/bin/python3 -m meat3.gui "\$@"
EOF
sudo chmod 755 /usr/local/bin/meat3 /usr/local/bin/meat3d

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

sudo cp "$SRC/packaging/meat3d.service" /etc/systemd/system/meat3d.service
sudo cp "$SRC/packaging/meat3.desktop" /usr/share/applications/meat3.desktop
sudo systemctl daemon-reload
sudo systemctl enable --now meat3d.service
sleep 2
systemctl --no-pager --lines=5 status meat3d.service || true
echo
echo "Done. Log out and back in once (group membership), then run: meat3"
echo "OSD hotkey: Right Ctrl + F12. In-game overlay: see README (MangoHud)."
