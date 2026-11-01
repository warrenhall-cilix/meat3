"""meat3-smufw: firmware file detection, hashing, pin/restore on a fake root (no download, no initramfs)."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from meat3 import smufw  # noqa: E402

GOOD = b"GOOD-FIRMWARE-" * 100
DIST = b"DIST-FIRMWARE-" * 100


class TestSmufw(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meat3-smufw-")
        self.root = os.path.join(self.tmp, "root")
        self.cache = os.path.join(self.tmp, "cache")
        dev = os.path.join(self.root, "sys/class/drm/card0/device")
        os.makedirs(os.path.join(dev, "fw_version"))
        os.makedirs(os.path.join(dev, "ip_discovery/die/0/MP1/0"))
        with open(os.path.join(dev, "vendor"), "w") as f:
            f.write("0x1002\n")
        with open(os.path.join(dev, "fw_version/smc_fw_version"), "w") as f:
            f.write("0x00685000\n")
        for k, v in (("major", "14"), ("minor", "0"), ("revision", "3")):
            with open(os.path.join(dev, "ip_discovery/die/0/MP1/0", k), "w") as f:
                f.write(v + "\n")
        self.fwdir = os.path.join(self.root, "usr/lib/firmware/amdgpu")
        os.makedirs(self.fwdir)
        self.inst = os.path.join(self.fwdir, "smu_14_0_3.bin")
        with open(self.inst, "wb") as f:
            f.write(DIST)
        # pretend the known-good hash is our GOOD blob and that it is already cached (no network)
        self.old_good = dict(smufw.KNOWN_GOOD)
        smufw.KNOWN_GOOD["smu_14_0_3.bin"] = hashlib.sha256(GOOD).hexdigest()
        os.makedirs(self.cache)
        with open(os.path.join(self.cache, f"smu_14_0_3.bin.{smufw.COMMIT}"), "wb") as f:
            f.write(GOOD)
        self.old_which = shutil.which
        shutil.which = lambda name: None          # no pacman, no initramfs tools

    def tearDown(self):
        smufw.KNOWN_GOOD.clear(); smufw.KNOWN_GOOD.update(self.old_good)
        shutil.which = self.old_which
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_detect_and_status(self):
        devs = smufw.running_pmfw(self.root)
        self.assertEqual(devs, [("card0", 0x685000, (14, 0, 3))])
        self.assertEqual(smufw.fw_name((14, 0, 3)), "smu_14_0_3.bin")
        self.assertIsNone(smufw.fw_name((13, 0, 7)))
        self.assertEqual(smufw.find_installed("smu_14_0_3.bin", self.root), self.inst)
        self.assertEqual(smufw.status(self.root, self.cache), 0)

    def test_pin_backs_up_installs_and_restore_reverts(self):
        self.assertEqual(smufw.pin(self.root, self.cache, no_initramfs=True), 0)
        self.assertEqual(open(self.inst, "rb").read(), GOOD)
        bk = smufw.backup_path(self.inst, self.cache)
        self.assertEqual(open(bk, "rb").read(), DIST)
        self.assertEqual(smufw.pin(self.root, self.cache, no_initramfs=True), 0)   # idempotent
        self.assertEqual(open(bk, "rb").read(), DIST)                               # backup not overwritten by GOOD
        self.assertEqual(smufw.restore(self.root, self.cache, no_initramfs=True), 0)
        self.assertEqual(open(self.inst, "rb").read(), DIST)

    def test_pin_handles_zst_files(self):
        if not self.old_which("zstd"):
            self.skipTest("zstd not installed")
        os.unlink(self.inst)
        import subprocess
        z = subprocess.run(["zstd", "-q", "-c"], input=DIST, capture_output=True, check=True).stdout
        with open(self.inst + ".zst", "wb") as f:
            f.write(z)
        self.assertEqual(smufw.find_installed("smu_14_0_3.bin", self.root), self.inst + ".zst")
        self.assertEqual(smufw.pin(self.root, self.cache, no_initramfs=True), 0)
        self.assertEqual(smufw.decompress(self.inst + ".zst"), GOOD)

    def test_fetch_rejects_wrong_hash(self):
        with open(os.path.join(self.cache, f"smu_14_0_3.bin.{smufw.COMMIT}"), "wb") as f:
            f.write(b"corrupt")
        old = smufw.urllib.request.urlopen

        class R:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return b"not the firmware"
        smufw.urllib.request.urlopen = lambda req, timeout=60: R()
        try:
            with self.assertRaises(RuntimeError):
                smufw.fetch("smu_14_0_3.bin", self.cache)
        finally:
            smufw.urllib.request.urlopen = old
        self.assertEqual(open(self.inst, "rb").read(), DIST)

    def test_local_file_and_user_agent(self):
        seen = []

        class R:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return GOOD
        old = smufw.urllib.request.urlopen

        def fake(req, timeout=60):
            seen.append(req.get_header("User-agent"))
            return R()
        smufw.urllib.request.urlopen = fake
        try:
            os.unlink(os.path.join(self.cache, f"smu_14_0_3.bin.{smufw.COMMIT}"))
            self.assertTrue(smufw.fetch("smu_14_0_3.bin", self.cache).endswith(smufw.COMMIT))
            self.assertEqual(seen, [smufw.USER_AGENT])
        finally:
            smufw.urllib.request.urlopen = old
        # --file: a plain local copy, hash-checked
        os.unlink(os.path.join(self.cache, f"smu_14_0_3.bin.{smufw.COMMIT}"))
        bad = os.path.join(self.tmp, "bad.bin")
        with open(bad, "wb") as f:
            f.write(b"nope")
        with self.assertRaises(RuntimeError):
            smufw.fetch("smu_14_0_3.bin", self.cache, local=bad)
        good = os.path.join(self.tmp, "good.bin")
        with open(good, "wb") as f:
            f.write(GOOD)
        self.assertEqual(smufw.pin(self.root, self.cache, no_initramfs=True, local=good), 0)
        self.assertEqual(open(self.inst, "rb").read(), GOOD)

    def test_restore_without_backup(self):
        self.assertEqual(smufw.restore(self.root, self.cache, no_initramfs=True), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class InitramfsPresets(unittest.TestCase):
    """Liam's case: a stale linux60.preset (kernel removed) must not make the rebuild fail."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.presets = os.path.join(self.tmp, "mkinitcpio.d")
        os.makedirs(self.presets)
        self.live = os.path.join(self.tmp, "vmlinuz-7.2")
        open(self.live, "wb").close()
        with open(os.path.join(self.presets, "linux72.preset"), "w") as f:
            f.write(f'ALL_config="/etc/mkinitcpio.conf"\nALL_kver="{self.live}"\nPRESETS=(\'default\')\n')
        with open(os.path.join(self.presets, "linux60.preset"), "w") as f:
            f.write(f"ALL_kver='{self.tmp}/vmlinuz-6.0'\n")   # not present
        self.old_which = shutil.which
        shutil.which = lambda name: "/usr/bin/mkinitcpio" if name == "mkinitcpio" else None

    def tearDown(self):
        shutil.which = self.old_which
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_presets_parsed(self):
        self.assertEqual(smufw.mkinitcpio_presets(self.presets),
                         [("linux60", f"{self.tmp}/vmlinuz-6.0"), ("linux72", self.live)])
        self.assertEqual(smufw.mkinitcpio_presets(os.path.join(self.tmp, "nope")), [])

    def test_stale_preset_skipped(self):
        calls = []
        def run(cmd):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)
        self.assertTrue(smufw.regen_initramfs(preset_dir=self.presets, run=run))
        self.assertEqual(calls, [["mkinitcpio", "-p", "linux72"]])

    def test_live_preset_failure_is_a_failure(self):
        run = lambda cmd: subprocess.CompletedProcess(cmd, 1)
        self.assertFalse(smufw.regen_initramfs(preset_dir=self.presets, run=run))

    def test_all_stale_is_a_failure(self):
        os.remove(self.live)
        run = lambda cmd: self.fail("mkinitcpio must not run for a stale preset")
        self.assertFalse(smufw.regen_initramfs(preset_dir=self.presets, run=run))

    def test_dry_run(self):
        self.assertTrue(smufw.regen_initramfs(dry=True, preset_dir=self.presets, run=lambda c: self.fail("ran")))
