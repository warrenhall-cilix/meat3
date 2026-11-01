"""meat3-smufw: pin the RDNA4 SMU firmware to the last release that exposes the VRM I2C bus.

    sudo meat3-smufw                 # status: running PMFW, installed file, whether it is the known-good one
    sudo meat3-smufw --pin           # download (hash-checked), back up the current file, install, rebuild initramfs
    sudo meat3-smufw --restore       # put the backed-up file back and rebuild the initramfs
    reboot after --pin / --restore

Why: AMD's SMU 14.0.x firmware from linux-firmware 2025-11-19 (PMFW 0x684b00) onwards no longer answers
on the "AMDGPU SMU" I2C adapters, so the regulators cannot be read or written (meat3's VRM telemetry and
the TBP/TDC scaling need it). The last release that does is linux-firmware commit 72a8d254 (2025-06-20),
PMFW 0x684a00. The file is AMD-signed and verified by the PSP, so a wrong file is refused at load, never
flashed: this tool only replaces a file under /lib/firmware and regenerates the initramfs.

What you lose: SMU fixes made after June 2025 while the pin is in place. Distribution firmware updates
overwrite the pinned file; `--status` says so, and `--pin` again restores it (a pacman hook is installed
on Arch-based systems so that happens automatically after a linux-firmware upgrade).
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import lzma
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Optional

COMMIT = "72a8d254"
COMMIT_FULL = "72a8d25438c080170c44488f61902e20328c6c23"
# kernel.org refuses Python's default user agent (403), so identify as meat3; the GitLab mirror is the fallback
SOURCES = ["https://git.kernel.org/pub/scm/linux/kernel/git/firmware/linux-firmware.git/plain/amdgpu/{name}?id=" + COMMIT,
           "https://gitlab.com/kernel-firmware/linux-firmware/-/raw/" + COMMIT_FULL + "/amdgpu/{name}"]
USER_AGENT = "meat3-smufw/1 (+https://github.com/warrenhall-cilix/meat3)"
# sha256 of the plain (uncompressed) blobs at that commit
KNOWN_GOOD = {
    "smu_14_0_3.bin": "e7ba91177127f5ed3669165d44195230901834d3c6f0b46dbf2c8ee9ec521b5a",
    "smu_14_0_2.bin": "1f6cb2c4724f8fd368ea735624240f9c42648759e341ddf305d4c48874a0a1c4",
}
PMFW_GOOD = 0x684A00
FW_DIRS = ("/usr/lib/firmware/amdgpu", "/lib/firmware/amdgpu")
CACHE = "/var/lib/meat3/fw"
HOOK = "/etc/pacman.d/hooks/meat3-smufw.hook"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decompress(path: str) -> bytes:
    """Plain, .zst or .xz firmware file -> raw bytes."""
    if path.endswith(".zst"):
        try:
            return subprocess.run(["zstd", "-dc", path], capture_output=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError) as e:
            raise RuntimeError(f"cannot decompress {path}: {e}")
    if path.endswith(".xz"):
        with lzma.open(path) as f:
            return f.read()
    with open(path, "rb") as f:
        return f.read()


def compress_like(data: bytes, path: str) -> bytes:
    """Encode `data` the way the file at `path` is stored (.zst / .xz / plain)."""
    if path.endswith(".zst"):
        return subprocess.run(["zstd", "-q", "-19", "-c"], input=data, capture_output=True, check=True).stdout
    if path.endswith(".xz"):
        return lzma.compress(data, format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC32)
    return data


def find_installed(name: str, root: str = "/") -> Optional[str]:
    for d in FW_DIRS:
        for ext in ("", ".zst", ".xz"):
            p = os.path.join(root, d.lstrip("/"), name + ext)
            if os.path.exists(p):
                return p
    return None


def running_pmfw(root: str = "/") -> list[tuple[str, int, tuple[int, int, int]]]:
    """[(pci, smc_fw_version, mp1 ip version)] for every amdgpu device."""
    out = []
    for dev in sorted(glob.glob(os.path.join(root, "sys/class/drm/card*/device"))):
        try:
            with open(os.path.join(dev, "vendor")) as f:
                if f.read().strip().lower() != "0x1002":
                    continue
            with open(os.path.join(dev, "fw_version/smc_fw_version")) as f:
                ver = int(f.read().strip(), 0)
        except (OSError, ValueError):
            continue
        mp1 = (0, 0, 0)
        try:
            base = os.path.join(dev, "ip_discovery/die/0/MP1/0")
            mp1 = tuple(int(open(os.path.join(base, f)).read().strip()) for f in ("major", "minor", "revision"))
        except (OSError, ValueError):
            pass
        real = os.path.basename(os.path.realpath(dev))
        pci = real if real != "device" else os.path.basename(os.path.dirname(dev))
        out.append((pci, ver, mp1))
    return out


def fw_name(mp1: tuple) -> Optional[str]:
    if tuple(mp1[:2]) == (14, 0) and mp1[2] in (2, 3):
        return f"smu_14_0_{mp1[2]}.bin"
    return None


def fetch(name: str, cache: str = CACHE, local: Optional[str] = None) -> str:
    """Get the known-good blob into the cache (hash-checked) from a local file or the sources; returns its path."""
    os.makedirs(cache, exist_ok=True)
    dst = os.path.join(cache, f"{name}.{COMMIT}")
    if os.path.exists(dst) and sha256(open(dst, "rb").read()) == KNOWN_GOOD[name]:
        return dst
    data = None
    if local:
        data = decompress(local)
        if sha256(data) != KNOWN_GOOD[name]:
            raise RuntimeError(f"{local} is not the known-good {name} (sha256 {sha256(data)[:12]}…)")
    else:
        errors = []
        for url in SOURCES:
            u = url.format(name=name)
            print(f"downloading {u}")
            try:
                req = urllib.request.Request(u, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=60) as r:
                    cand = r.read()
            except (OSError, urllib.error.URLError) as e:
                errors.append(f"{u}: {e}")
                continue
            if sha256(cand) != KNOWN_GOOD[name]:
                errors.append(f"{u}: sha256 mismatch")
                continue
            data = cand
            break
        if data is None:
            raise RuntimeError("could not fetch a verified copy:\n  " + "\n  ".join(errors) +
                               "\nGet the file another way and pass it with --file")
    with open(dst + ".tmp", "wb") as f:
        f.write(data)
    os.replace(dst + ".tmp", dst)
    return dst


PRESET_DIR = "/etc/mkinitcpio.d"


def mkinitcpio_presets(preset_dir: str = PRESET_DIR) -> list[tuple[str, Optional[str]]]:
    """(preset name, kernel image path or None) for every *.preset, sorted by name.

    The image path comes from ALL_kver when it is an absolute path; a bare version string
    (or no ALL_kver) gives None, meaning "cannot check, just run it"."""
    out = []
    try:
        names = sorted(n for n in os.listdir(preset_dir) if n.endswith(".preset"))
    except OSError:
        return out
    for n in names:
        kver = None
        try:
            with open(os.path.join(preset_dir, n)) as f:
                for line in f:
                    m = re.match(r'\s*ALL_kver=["\']?([^"\'\s]+)', line)
                    if m:
                        kver = m.group(1)
        except OSError:
            pass
        out.append((n[:-len(".preset")], kver if kver and kver.startswith("/") else None))
    return out


def initramfs_cmd() -> Optional[list[str]]:
    for cmd in (["mkinitcpio", "-P"], ["dracut", "--regenerate-all", "--force"], ["update-initramfs", "-u", "-k", "all"]):
        if shutil.which(cmd[0]):
            return cmd
    return None


def regen_initramfs(dry: bool = False, preset_dir: str = PRESET_DIR, run=None) -> bool:
    """Rebuild the initramfs. Returns True when every image that can be built was built.

    mkinitcpio -P fails as a whole when one preset is stale (a preset left behind by a removed
    kernel, whose vmlinuz is gone), even though the running kernel's image rebuilt fine. So on
    mkinitcpio systems each preset is run on its own and presets whose kernel image is not
    readable are skipped with a note."""
    run = run or subprocess.run
    cmd = initramfs_cmd()
    if not cmd:
        print("!! no initramfs tool found (mkinitcpio / dracut / update-initramfs): regenerate it yourself")
        return False
    if cmd[0] != "mkinitcpio":
        print("regenerating initramfs:", " ".join(cmd))
        return True if dry else run(cmd).returncode == 0
    presets = mkinitcpio_presets(preset_dir)
    if not presets:
        print("regenerating initramfs:", " ".join(cmd))
        return True if dry else run(cmd).returncode == 0
    ok, built = True, 0
    for name, image in presets:
        if image and not os.access(image, os.R_OK):
            print(f"skipping preset {name}: kernel image {image} is not present (stale preset from a removed kernel; "
                  f"remove {os.path.join(preset_dir, name + '.preset')} if pacman does not own it)")
            continue
        print(f"regenerating initramfs: mkinitcpio -p {name}")
        if dry:
            built += 1
            continue
        if run(["mkinitcpio", "-p", name]).returncode == 0:
            built += 1
        else:
            print(f"!! preset {name} failed")
            ok = False
    if built == 0:
        print("!! no initramfs was built")
        return False
    return ok


def backup_path(installed: str, cache: str = CACHE) -> str:
    return os.path.join(cache, "backup", os.path.basename(installed))


def status(root: str = "/", cache: str = CACHE) -> int:
    devs = running_pmfw(root)
    if not devs:
        print("no amdgpu device with an SMU firmware version found")
    rc = 0
    for pci, ver, mp1 in devs:
        name = fw_name(mp1)
        good = "exposes the VRM bus" if ver <= PMFW_GOOD else "VRM bus closed (newer than 0x%06x)" % PMFW_GOOD
        print(f"{pci}: MP1 {'.'.join(map(str, mp1))}, running PMFW 0x{ver:06x}: {good}")
        if not name:
            print("   not an RDNA4 SMU 14.0.2/14.0.3 device; nothing to pin")
            continue
        inst = find_installed(name, root)
        if not inst:
            print(f"   installed file: none ({name} not found under {', '.join(FW_DIRS)})")
            rc = 1
            continue
        try:
            h = sha256(decompress(inst))
        except RuntimeError as e:
            print(f"   {e}")
            rc = 1
            continue
        pinned = h == KNOWN_GOOD[name]
        print(f"   installed file: {inst}: {'known-good ' + COMMIT if pinned else 'distribution version (' + h[:12] + ')'}")
        bk = backup_path(inst, cache)
        if os.path.exists(bk):
            print(f"   backup of the previous file: {bk}")
        if pinned and ver > PMFW_GOOD:
            print("   pinned file installed but the running firmware is newer: reboot (initramfs rebuilt?)")
        if not pinned and ver <= PMFW_GOOD:
            print("   running the old firmware but the installed file is newer: a distribution update replaced it; "
                  "run --pin again before the next reboot")
    if os.path.exists(os.path.join(root, HOOK.lstrip("/"))):
        print(f"pacman hook installed: {HOOK}")
    return rc


def pin(root: str = "/", cache: str = CACHE, no_initramfs: bool = False, dry: bool = False,
        local: Optional[str] = None) -> int:
    devs = [d for d in running_pmfw(root) if fw_name(d[2])]
    if not devs:
        print("no RDNA4 (SMU 14.0.2 / 14.0.3) GPU found")
        return 2
    name = fw_name(devs[0][2])
    inst = find_installed(name, root)
    if not inst:
        print(f"{name} is not installed under {', '.join(FW_DIRS)}; install your distribution's amdgpu firmware first")
        return 2
    src = fetch(name, cache, local)
    data = open(src, "rb").read()
    cur = decompress(inst)
    if sha256(cur) == KNOWN_GOOD[name]:
        print(f"{inst} is already the known-good {COMMIT} build")
    else:
        bk = backup_path(inst, cache)
        os.makedirs(os.path.dirname(bk), exist_ok=True)
        if not os.path.exists(bk):
            print(f"backing up {inst} -> {bk}")
            if not dry:
                shutil.copy2(inst, bk)
        print(f"installing {name} {COMMIT} as {inst}")
        if not dry:
            blob = compress_like(data, inst)
            with open(inst + ".tmp", "wb") as f:
                f.write(blob)
            os.chmod(inst + ".tmp", 0o644)
            os.replace(inst + ".tmp", inst)
    if not no_initramfs and not regen_initramfs(dry):
        print("!! initramfs regeneration failed; the old firmware will be loaded early at boot until it is rebuilt")
        return 1
    install_hook(root, dry)
    print("done. Reboot; then `meat3-smufw` should report PMFW 0x%06x and meat3's VRM rows should fill in." % PMFW_GOOD)
    return 0


def restore(root: str = "/", cache: str = CACHE, no_initramfs: bool = False, dry: bool = False) -> int:
    done = False
    for name in KNOWN_GOOD:
        inst = find_installed(name, root)
        if not inst:
            continue
        bk = backup_path(inst, cache)
        if not os.path.exists(bk):
            continue
        print(f"restoring {bk} -> {inst}")
        if not dry:
            shutil.copy2(bk, inst + ".tmp")
            os.replace(inst + ".tmp", inst)
        done = True
    hook = os.path.join(root, HOOK.lstrip("/"))
    if os.path.exists(hook):
        print(f"removing {HOOK}")
        if not dry:
            os.unlink(hook)
    if not done:
        print("no backup to restore (nothing was pinned, or the backup is gone: reinstall the distribution firmware package)")
        return 1
    if not no_initramfs and not regen_initramfs(dry):
        return 1
    print("done. Reboot to run the distribution firmware again.")
    return 0


def install_hook(root: str = "/", dry: bool = False) -> None:
    """Arch/Manjaro: re-pin automatically after linux-firmware upgrades."""
    if not shutil.which("pacman"):
        return
    hook = os.path.join(root, HOOK.lstrip("/"))
    text = ("[Trigger]\nOperation = Upgrade\nOperation = Install\nType = Package\nTarget = linux-firmware\n"
            "Target = linux-firmware-amdgpu\n\n[Action]\nDescription = meat3: re-pin the RDNA4 SMU firmware\n"
            "When = PostTransaction\nExec = /usr/local/bin/meat3-smufw --pin --no-initramfs\n")
    if os.path.exists(hook) and open(hook).read() == text:
        return
    print(f"installing pacman hook {HOOK} (re-pins after firmware package upgrades)")
    if dry:
        return
    os.makedirs(os.path.dirname(hook), exist_ok=True)
    with open(hook, "w") as f:
        f.write(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="meat3-smufw", description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true", help="(default)")
    g.add_argument("--pin", action="store_true", help="install the known-good firmware file")
    g.add_argument("--restore", action="store_true", help="put the backed-up distribution file back")
    ap.add_argument("--no-initramfs", action="store_true", help="do not regenerate the initramfs")
    ap.add_argument("--dry-run", action="store_true", help="say what would be done")
    ap.add_argument("--file", help="use this local copy of the firmware (plain, .zst or .xz) instead of downloading; "
                                   "still has to match the known-good sha256")
    ap.add_argument("--root", default="/", help=argparse.SUPPRESS)
    ap.add_argument("--cache", default=CACHE, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if (a.pin or a.restore) and os.geteuid() != 0 and not a.dry_run:
        print("run as root (sudo meat3-smufw ...)")
        return 2
    try:
        if a.pin:
            return pin(a.root, a.cache, a.no_initramfs, a.dry_run, a.file)
        if a.restore:
            return restore(a.root, a.cache, a.no_initramfs, a.dry_run)
        return status(a.root, a.cache)
    except (RuntimeError, OSError) as e:
        print(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
