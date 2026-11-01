"""Experimental features: settings whitelist, config file hygiene (user keys only), smufw state, daemon dispatch."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meat3.core import config, experimental  # noqa: E402
from meat3 import smufw  # noqa: E402


class ConfigFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meat3-exp-")
        self.old_path = config.PATH
        config.PATH = os.path.join(self.tmp, "config.json")
        config._cfg, config._user = None, {}

    def tearDown(self):
        config.PATH = self.old_path
        config._cfg, config._user = None, {}
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _file(self) -> dict:
        with open(config.PATH) as f:
            return json.load(f)

    def test_save_writes_only_user_keys(self):
        # the OSD page saves a full merged config: the file must not freeze the default guard list
        cfg = config.load()
        cfg["osd"]["items"] = ["cpu0.x"]
        config.save(cfg)
        on_disk = self._file()
        self.assertEqual(on_disk, {"osd": {"items": ["cpu0.x"]}})
        self.assertNotIn("guards", on_disk)
        self.assertEqual(len(config.load()["guards"]), len(config.DEFAULTS["guards"]))

    def test_old_full_guard_list_is_dropped_but_a_custom_one_is_kept(self):
        with open(config.PATH, "w") as f:
            json.dump({"guards": copy.deepcopy(config.DEFAULTS["guards"])}, f)
        config.load()
        self.assertNotIn("guards", config.user_settings())
        custom = [{"name": "mine", "sensor": "x", "above": 1, "control": "y", "value": 0}]
        with open(config.PATH, "w") as f:
            json.dump({"guards": custom}, f)
        self.assertEqual(config.load()["guards"], custom)
        self.assertTrue(experimental.effective()["user_guards_list"])

    def test_update_user_merges_and_deletes(self):
        config.update_user({"amdgpu_vrm": {"allow_writes": True}, "guard_overrides": {"GPU hotspot overtemp": {"above": 95}}})
        self.assertEqual(self._file(), {"amdgpu_vrm": {"allow_writes": True},
                                        "guard_overrides": {"GPU hotspot overtemp": {"above": 95}}})
        cfg = config.get()
        self.assertTrue(cfg["amdgpu_vrm"]["allow_writes"])
        self.assertEqual(cfg["amdgpu_vrm"]["max_tbp_scale"], 1.3)                    # defaults still merged
        self.assertEqual(next(g for g in cfg["guards"] if g["name"] == "GPU hotspot overtemp")["above"], 95)
        config.update_user({"guard_overrides": None})
        self.assertNotIn("guard_overrides", self._file())
        self.assertEqual(next(g for g in config.get()["guards"] if g["name"] == "GPU hotspot overtemp")["above"], 100)

    def test_apply_validates_and_writes(self):
        config.load()
        cfg = experimental.apply({"experimental.accepted": True, "amdgpu_vrm.max_tbp_scale": 1.5,
                                  "amdgpu_vrm.mps_offset_lsb_mv": 5},
                                 {"AMD GPU real power (scaled limits)": {"above": 550, "enabled": True}})
        self.assertTrue(cfg["experimental"]["accepted"])
        self.assertEqual(cfg["amdgpu_vrm"]["max_tbp_scale"], 1.5)
        self.assertEqual(cfg["amdgpu_vrm"]["mps_offset_lsb_mv"], 5)
        self.assertEqual(next(g for g in cfg["guards"] if g["name"] == "AMD GPU real power (scaled limits)")["above"], 550)
        on_disk = self._file()
        self.assertEqual(on_disk["guard_overrides"], {"AMD GPU real power (scaled limits)": {"above": 550.0, "enabled": True}})
        for bad_settings, bad_guards in (({"amdgpu_vrm.max_tbp_scale": 1.6}, None),        # above the hard cap
                                         ({"amdgpu_vrm.allow_writes": "yes"}, None),
                                         ({"web.listen": "0.0.0.0"}, None),                 # not whitelisted
                                         ({"amdgpu_vrm.mps_offset_lsb_mv": 7}, None),
                                         ({}, {"no such guard": {"above": 1}}),
                                         ({}, {"GPU hotspot overtemp": {"control": "x"}})):  # action not editable
            with self.assertRaises(ValueError, msg=f"{bad_settings} {bad_guards}"):
                experimental.apply(bad_settings, bad_guards)
        eff = experimental.effective()
        self.assertTrue(eff["accepted"])
        self.assertEqual(eff["amdgpu_vrm"]["max_tbp_scale"], 1.5)
        self.assertEqual(eff["hard_max_scale"], 1.5)
        # reset drops the overrides
        cfg = experimental.apply({}, reset_guards=True)
        self.assertEqual(next(g for g in cfg["guards"] if g["name"] == "AMD GPU real power (scaled limits)")["above"], 450)
        self.assertNotIn("guard_overrides", self._file())


class SmufwState(unittest.TestCase):
    GOOD = b"GOOD-FW-" * 100

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meat3-exp-fw-")
        self.root = os.path.join(self.tmp, "root")
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
        self.old_good = dict(smufw.KNOWN_GOOD)
        smufw.KNOWN_GOOD["smu_14_0_3.bin"] = hashlib.sha256(self.GOOD).hexdigest()

    def tearDown(self):
        smufw.KNOWN_GOOD.clear(); smufw.KNOWN_GOOD.update(self.old_good)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_states(self):
        st = experimental.smufw_state(self.root, cache=os.path.join(self.tmp, "cache"))
        d = st["devices"][0]
        self.assertEqual((d["pci"], d["pmfw_hex"], d["vrm_bus_open"], d["installed"]), ("card0", "0x685000", False, None))
        with open(self.inst, "wb") as f:
            f.write(b"DIST" * 50)
        d = experimental.smufw_state(self.root, cache=os.path.join(self.tmp, "cache"))["devices"][0]
        self.assertIs(d["pinned"], False)
        self.assertFalse(d["reboot_needed"])
        with open(self.inst, "wb") as f:
            f.write(self.GOOD)
        d = experimental.smufw_state(self.root, cache=os.path.join(self.tmp, "cache"))["devices"][0]
        self.assertIs(d["pinned"], True)
        self.assertTrue(d["reboot_needed"])                     # running 0x685000 with the good file installed
        self.assertIn("reboot", d["note"])

    def test_runner_refuses_unknown_action(self):
        r = experimental.SmufwRunner()
        self.assertFalse(r.start("flash")[0])
        self.assertFalse(r.running())
        self.assertEqual(r.status()["rc"], None)

    def test_runner_runs_a_command_and_keeps_its_output(self):
        r = experimental.SmufwRunner(python=sys.executable)
        # swap the module for a tiny script so no firmware is touched
        r._run = lambda action: (r.log.extend(["== fake " + action, "done."]), setattr(r, "rc", 0))  # type: ignore
        ok, msg = r.start("pin")
        self.assertTrue(ok, msg)
        r.thread.join(5)
        self.assertEqual(r.status()["log"][-1], "done.")
        self.assertEqual(r.status()["rc"], 0)


class DaemonDispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meat3-exp-d-")
        self.old_path = config.PATH
        config.PATH = os.path.join(self.tmp, "config.json")
        config._cfg, config._user = None, {}
        config.load()

    def tearDown(self):
        config.PATH = self.old_path
        config._cfg, config._user = None, {}
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_commands(self):
        from meat3.daemon import meat3d

        class FakeBackend:
            kind, name = "gpu", "fake"
            seen = None

            def sensors(self): return []
            def controls(self): return []
            def apply(self, cid, value): return False, "no"
            def close(self): pass
            def reload_config(self, cfg): self.seen = cfg["amdgpu_vrm"]["max_tbp_scale"]
        b = FakeBackend()
        st = meat3d.State([b])
        h = meat3d.Handler.__new__(meat3d.Handler)
        h.state = st
        h.smufw = experimental.SmufwRunner()
        h.smufw._run = lambda action: None       # never touch firmware from a test
        r = h.dispatch({"cmd": "experimental_get"}, can_write=False)
        self.assertTrue(r["ok"]); self.assertFalse(r["accepted"]); self.assertFalse(r["can_write"])
        self.assertIn("smufw", r)
        self.assertFalse(h.dispatch({"cmd": "experimental_set", "settings": {"experimental.accepted": True}}, can_write=False)["ok"])
        self.assertFalse(h.dispatch({"cmd": "smufw_run", "action": "pin"}, can_write=True)["ok"])   # notice not accepted
        r = h.dispatch({"cmd": "experimental_set", "settings": {"experimental.accepted": True, "amdgpu_vrm.max_tbp_scale": 1.45}},
                       can_write=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(b.seen, 1.45)                                            # backend re-read the config live
        self.assertFalse(h.dispatch({"cmd": "experimental_set", "settings": {"amdgpu_vrm.max_tbp_scale": 9}}, can_write=True)["ok"])
        r = h.dispatch({"cmd": "smufw_run", "action": "pin"}, can_write=True)
        self.assertTrue(r["ok"], r)
        h.smufw.thread.join(5)
        self.assertFalse(h.dispatch({"cmd": "smufw_run", "action": "bogus"}, can_write=True)["ok"])


if __name__ == "__main__":
    unittest.main()
