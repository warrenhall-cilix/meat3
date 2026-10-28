#!/usr/bin/env bash
# Build amdgpu with the meat3 custom-SKU-table patch (kmod/amdgpu-custom-sku) for the RUNNING kernel
# and install it to /lib/modules/$(uname -r)/updates, where it overrides the stock amdgpu.
#
#   bash tools/build_patched_amdgpu.sh              # build + install (asks first)
#   bash tools/build_patched_amdgpu.sh --build-only # just build, install nothing
#   bash tools/build_patched_amdgpu.sh --uninstall  # remove it, back to the stock amdgpu
#
# EXPERIMENTAL. Only this kernel version is affected: any other installed kernel keeps the stock
# driver and is your way back if the display doesn't come up. Without a custom_sku_<pci>.bin file the
# patched driver behaves exactly like the stock one.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PATCH="$REPO/kmod/amdgpu-custom-sku/meat3-custom-sku.patch"
KREL="$(uname -r)"
KB="/lib/modules/$KREL/build"
WORK="${XDG_CACHE_HOME:-$HOME/.cache}/meat3-amdgpu"
DEST="/lib/modules/$KREL/updates"

rebuild_initramfs() {
    if command -v mkinitcpio >/dev/null; then sudo mkinitcpio -P
    elif command -v dracut >/dev/null; then sudo dracut --regenerate-all --force
    elif command -v update-initramfs >/dev/null; then sudo update-initramfs -u -k "$KREL"
    else echo "!! rebuild your initramfs yourself if amdgpu is in it"; fi
}

if [[ "${1:-}" == "--uninstall" ]]; then
    sudo rm -f "$DEST"/amdgpu.ko "$DEST"/amdgpu.ko.zst "$DEST"/amdgpu.ko.xz
    sudo depmod -a "$KREL"
    rebuild_initramfs
    echo "removed; the stock amdgpu loads from the next boot"
    exit 0
fi

[[ -f "$KB/Makefile" && -f "$KB/.config" && -f "$KB/Module.symvers" ]] || {
    echo "kernel headers for $KREL not found ($KB); install them first"; exit 1; }
if [[ "${1:-}" != "--build-only" ]]; then
    lock=$(cat /sys/kernel/security/lockdown 2>/dev/null || true)
    if [[ "$(cat /sys/module/module/parameters/sig_enforce 2>/dev/null)" == "Y" || "$lock" == *"[integrity]"* \
          || "$lock" == *"[confidentiality]"* ]]; then
        echo "this kernel only loads signed modules (Secure Boot / lockdown); an unsigned amdgpu would not"
        echo "load and you'd lose the display. Not installing."
        exit 1
    fi
fi

ver() { sed -n "s/^$1 = //p" "$KB/Makefile" | head -1; }
V=$(ver VERSION); P=$(ver PATCHLEVEL); S=$(ver SUBLEVEL)
BASE="$V.$P"; [[ "$S" != "0" && -n "$S" ]] && BASE="$V.$P.$S"
SRC="$WORK/linux-$BASE"
mkdir -p "$WORK"
if [[ ! -d "$SRC" ]]; then
    echo "== downloading linux-$BASE (kernel.org)"
    curl -fL -o "$WORK/linux-$BASE.tar.xz" "https://cdn.kernel.org/pub/linux/kernel/v$V.x/linux-$BASE.tar.xz"
    tar -C "$WORK" -xf "$WORK/linux-$BASE.tar.xz"
    rm -f "$WORK/linux-$BASE.tar.xz"
fi
cd "$SRC"
if patch -p1 -R --dry-run -s < "$PATCH" >/dev/null 2>&1; then
    echo "== patch already applied"
else
    echo "== applying meat3 custom SKU patch"
    patch -p1 < "$PATCH"
fi
cp "$KB/.config" .config
cp "$KB/Module.symvers" Module.symvers
MAKEARGS=()
grep -q '^CONFIG_CC_IS_CLANG=y' .config && MAKEARGS+=(LLVM=1)
if ! command -v bc >/dev/null; then          # only needed for timeconst.h; reuse the distro's copy
    mkdir -p "$WORK/bin"
    printf '#!/bin/sh\ncat "%s"\n' "$KB/include/generated/timeconst.h" > "$WORK/bin/bc"
    chmod +x "$WORK/bin/bc"
    export PATH="$WORK/bin:$PATH"
fi
echo "== building amdgpu for $KREL (a few minutes)"
make "${MAKEARGS[@]}" olddefconfig >/dev/null
make "${MAKEARGS[@]}" -j"$(nproc)" modules_prepare >/dev/null
make "${MAKEARGS[@]}" -j"$(nproc)" M=drivers/gpu/drm/amd/amdgpu modules
KO=drivers/gpu/drm/amd/amdgpu/amdgpu.ko
if command -v llvm-strip >/dev/null; then llvm-strip --strip-debug "$KO"; else strip --strip-debug "$KO"; fi
grep -qa amdgpu_custom_sku "$KO" || { echo "built module lacks the patch?!"; exit 1; }
echo "== built $SRC/$KO ($(du -h "$KO" | cut -f1))"
[[ "${1:-}" == "--build-only" ]] && exit 0

cat <<EOF

About to install the patched amdgpu for kernel $KREL into $DEST.
  - It is built from vanilla kernel.org source $BASE; distro amdgpu patches (if any) are replaced.
  - If the display does not come up: boot another kernel from the boot menu (it still has the
    stock amdgpu), then run: bash $REPO/tools/build_patched_amdgpu.sh --uninstall
EOF
read -r -p "Type yes to install: " ans
[[ "$ans" == "yes" ]] || { echo "not installed"; exit 1; }
EXT=""
ls "/lib/modules/$KREL/kernel/drivers/gpu/drm/amd/amdgpu/"amdgpu.ko.zst >/dev/null 2>&1 && EXT=".zst"
ls "/lib/modules/$KREL/kernel/drivers/gpu/drm/amd/amdgpu/"amdgpu.ko.xz >/dev/null 2>&1 && EXT=".xz"
sudo mkdir -p "$DEST"
case "$EXT" in
    .zst) zstd -q -f -19 "$KO" -o "$WORK/amdgpu.ko.zst" && sudo install -m644 "$WORK/amdgpu.ko.zst" "$DEST/" ;;
    .xz)  xz -c --check=crc32 "$KO" > "$WORK/amdgpu.ko.xz" && sudo install -m644 "$WORK/amdgpu.ko.xz" "$DEST/" ;;
    *)    sudo install -m644 "$KO" "$DEST/" ;;
esac
sudo depmod -a "$KREL"
rebuild_initramfs
echo
echo "Installed. Reboot, then check:  sudo dmesg | grep 'custom SKU'   (nothing = no table file yet)"
echo "Create a table with: sudo meat3-skutable --tdc-gfx 360 --write"
