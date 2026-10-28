#!/usr/bin/env bash
# Collect read-only AMD GPU info (VBIOS, power table, overdrive ranges, firmware versions, metrics)
# into one tarball for meat3 development. Writes nothing to the GPU.
#
#   sudo bash collect_amdgpu.sh        -> ./amdgpu-dump-<host>-<date>.tar.gz
set -u
[ "$(id -u)" = 0 ] || { echo "run with sudo (the VBIOS copy is only readable by root)"; exit 1; }
out="amdgpu-dump-$(hostname -s)-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$out"
{ uname -a; echo; lspci -nn | grep -iE 'vga|display|3d'; echo; cat /proc/cpuinfo | grep -m1 'model name'; } > "$out/system.txt" 2>&1
dmesg | grep -iE 'amdgpu|drm' > "$out/dmesg-amdgpu.txt" 2>&1

for card in /sys/class/drm/card[0-9]*; do
    dev="$card/device"
    [ -f "$dev/vendor" ] && [ "$(cat "$dev/vendor")" = "0x1002" ] || continue
    [[ "$card" =~ card[0-9]+$ ]] || continue
    pci=$(basename "$(readlink -f "$dev")")
    d="$out/$(basename "$card")-$pci"
    mkdir -p "$d"
    for f in device subsystem_vendor subsystem_device revision vbios_version current_link_speed current_link_width \
             pp_table pp_od_clk_voltage pp_dpm_sclk pp_dpm_mclk pp_dpm_fclk pp_dpm_socclk pp_dpm_pcie \
             pp_power_profile_mode pp_features power_dpm_force_performance_level gpu_metrics \
             mem_info_vram_total mem_info_vram_vendor; do
        [ -e "$dev/$f" ] && cat "$dev/$f" > "$d/$f" 2>/dev/null
    done
    [ -d "$dev/gpu_od" ] && cp -r "$dev/gpu_od" "$d/gpu_od" 2>/dev/null
    [ -d "$dev/fw_version" ] && for f in "$dev"/fw_version/*; do echo "$(basename "$f")=$(cat "$f")"; done > "$d/fw_version.txt"
    [ -d "$dev/ip_discovery/die/0" ] && for ip in "$dev"/ip_discovery/die/0/*/0; do
        n=$(basename "$(dirname "$ip")"); [[ "$n" =~ ^[A-Z] ]] || continue
        echo "$n $(cat "$ip/major").$(cat "$ip/minor").$(cat "$ip/revision")"; done > "$d/ip_versions.txt"
    for h in "$dev"/hwmon/hwmon*; do
        [ -d "$h" ] && for f in "$h"/*; do [ -f "$f" ] && [ -r "$f" ] && echo "$(basename "$f")=$(cat "$f" 2>/dev/null)"; done > "$d/hwmon.txt"
    done
    # VRM controllers (PMBus) on the GPU's own SMU I2C buses: reads only (plus PAGE select)
    if command -v i2cget >/dev/null; then
        modprobe i2c-dev 2>/dev/null || true
        for a in "$dev"/i2c-*; do
            [[ -f "$a/name" ]] && grep -qi smu "$a/name" || continue
            bus=${a##*i2c-}
            for addr in 0x22 0x24 0x26; do
                i2cget -y "$bus" "$addr" 0x98 b >/dev/null 2>&1 || continue
                {
                    echo "bus $bus ($(cat "$a/name")) addr $addr"
                    echo "MFR_ID    $(i2cget -y "$bus" "$addr" 0x99 s 2>&1)"
                    echo "MFR_MODEL $(i2cget -y "$bus" "$addr" 0x9a s 2>&1)"
                    for page in 0 1; do
                        i2cset -y "$bus" "$addr" 0x00 "$page" b 2>/dev/null || continue
                        echo "-- page $page"
                        for r in 0x20:b 0x23:w 0x24:w 0x28:w 0x38:w 0x39:w 0x1e:b 0x5e:w 0x88:w 0x89:w 0x8b:w 0x8c:w 0x8d:w 0x96:w; do
                            echo "  ${r%%:*} $(i2cget -y "$bus" "$addr" "${r%%:*}" "${r##*:}" 2>&1)"
                        done
                    done
                    i2cset -y "$bus" "$addr" 0x00 0 b 2>/dev/null
                } >> "$d/vrm_pmbus.txt"
            done
        done
    else
        echo "i2c-tools not installed" > "$d/vrm_pmbus.txt"
    fi
    for dbg in /sys/kernel/debug/dri/*/; do
        grep -q "$pci" "$dbg/name" 2>/dev/null || continue
        cat "$dbg/amdgpu_vbios" > "$d/vbios.rom" 2>/dev/null
        cat "$dbg/amdgpu_firmware_info" > "$d/firmware_info.txt" 2>/dev/null
        timeout 5 cat "$dbg/amdgpu_pm_info" > "$d/pm_info.txt" 2>/dev/null
        cat "$dbg/amdgpu_discovery" > "$d/discovery.bin" 2>/dev/null
        break
    done
done
tar czf "$out.tar.gz" "$out" && rm -rf "$out"
echo "Done: $(pwd)/$out.tar.gz  ($(du -h "$out.tar.gz" | cut -f1)) - send this file over."
