import os
import tempfile
import unittest
from unittest import mock

from meat3.osd import mangohud as M


class TestHotkey(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(M.parse_hotkey("rightctrl+f12"), ["KEY_RIGHTCTRL", "KEY_F12"])
        self.assertEqual(M.parse_hotkey(["KEY_LEFTALT", "KEY_LEFTSHIFT", "KEY_O"]),
                         ["KEY_LEFTALT", "KEY_LEFTSHIFT", "KEY_O"])
        self.assertEqual(M.parse_hotkey("KEY_F10"), ["KEY_F10"])
        self.assertEqual(M.parse_hotkey(" Right Ctrl + Home ".replace("Right Ctrl", "RIGHTCTRL")),
                         ["KEY_RIGHTCTRL", "KEY_HOME"])
        for bad, why in (("", "1 to 4"), ("RIGHTCTRL", "normal key"), ("RIGHTCTRL+F12+NOPE", "unsupported"),
                         ("A+B+C+D+E", "1 to 4")):
            with self.assertRaises(ValueError) as cm:
                M.parse_hotkey(bad)
            self.assertIn(why, str(cm.exception))

    def test_set_toggle_key(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "MangoHud.conf")
            self.assertFalse(M.set_toggle_key(p, "KEY_RIGHTCTRL+KEY_F11"))         # no file
            M.write_config(p, M.generate([], hotkey="KEY_RIGHTCTRL+KEY_F12"))
            self.assertIn("toggle_hud=Control_R+F12", open(p).read())
            self.assertTrue(M.set_toggle_key(p, "KEY_LEFTALT+KEY_O"))
            txt = open(p).read()
            self.assertIn("toggle_hud=Alt_L+o", txt)
            self.assertNotIn("Control_R+F12", txt)
            M.write_config(p, M.generate([], hotkey="KEY_RIGHTCTRL+KEY_F12", hotkey_owner="daemon"))
            self.assertFalse(M.set_toggle_key(p, "KEY_LEFTALT+KEY_O"))             # daemon mode: untouched

    def test_generate_style(self):
        txt = M.generate([], font_size=28, text_color="EEEEEE", background_alpha=0.8, background_color="0B0C0F")
        for line in ("font_size=28", "text_color=EEEEEE", "background_alpha=0.8", "background_color=0B0C0F"):
            self.assertIn(line + "\n", txt)
        self.assertIn("background_alpha=1\n", M.generate([], background_alpha=1.7))
        self.assertNotIn("background_color", M.generate([]))

    def test_short_labels_fit_mangohud(self):
        cases = {"GPU 0 GPC (core) clock": "GPU GPC clock", "GPU 0 Board power (real, x1.5 shunt)": "GPU Board power",
                 "GPU 0 Highest pin current": "GPU Max pin amps", "CPU Package power (RAPL)": "CPU Pkg power",
                 "CPU Core 7 active clock": "CPU Core 7 clock", "AMD GPU 0 Junction (hotspot)": "GPU Junction"}
        for full, short in cases.items():
            self.assertEqual(M.short_label(full), short)
        self.assertEqual(M.short_label("AMD GPU 1 Junction", keep_gpu_index=True), "GPU1 Junction")
        for lb in ("A very long sensor label with no abbreviations at all", "CPU L3 hit rate"):
            self.assertLessEqual(len(M.short_label(lb)), M.LABEL_MAX)
        txt = M.generate([{"id": "a", "group": "GPU 0: x/Clocks", "label": "GPC (core) clock"}], font_size=28)
        self.assertIn("custom_text=GPU GPC clock\n", txt)
        self.assertIn("no_small_font\n", txt)
        self.assertIn("font_size_text=28\n", txt)

    def test_global_mangohud(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}):
            self.assertFalse(M.global_enabled())
            p = M.set_global(True, os.path.join(d, "meat3", "MangoHud.conf"))
            self.assertEqual(p, M.env_file())
            self.assertTrue(M.global_enabled())
            txt = open(p).read()
            self.assertIn("MANGOHUD=1\n", txt)
            self.assertIn(f"MANGOHUD_CONFIGFILE={d}/meat3/MangoHud.conf\n", txt)
            with self.assertRaises(ValueError):
                M.set_global(True, "/tmp/my games/MangoHud.conf")
            M.set_global(False, "")
            self.assertFalse(M.global_enabled())
            M.set_global(False, "")                         # already off: fine

    def test_daemon_osd_set_hotkey(self):
        from meat3.daemon import meat3d as D
        st = mock.Mock()
        st.osd = {"hotkey": ["KEY_RIGHTCTRL", "KEY_F12"]}
        st.lock = mock.MagicMock()
        h = D.Handler.__new__(D.Handler)
        h.state = st
        r = h.dispatch({"cmd": "osd_set", "hotkey": "leftalt+o"}, can_write=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(st.osd["hotkey"], ["KEY_LEFTALT", "KEY_O"])
        st.save_osd.assert_called()
        r = h.dispatch({"cmd": "osd_set", "hotkey": "leftalt"}, can_write=True)
        self.assertFalse(r["ok"])
        self.assertEqual(st.osd["hotkey"], ["KEY_LEFTALT", "KEY_O"])
        self.assertFalse(h.dispatch({"cmd": "osd_set", "hotkey": "f9"}, can_write=False)["ok"])


if __name__ == "__main__":
    unittest.main()


class TestOsdFiles(unittest.TestCase):
    def test_safe_tmp_dir(self):
        from meat3.daemon import meat3d as D
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "meat3")
            self.assertTrue(D._safe_tmp_dir(p))                 # created, ours, 0755
            self.assertTrue(D._safe_tmp_dir(p))                 # already there: still fine
            os.chmod(p, 0o777)
            self.assertFalse(D._safe_tmp_dir(p))                # writable by others: refuse
            os.rmdir(p)
            os.symlink(d, p)
            self.assertFalse(D._safe_tmp_dir(p))                # a symlink: refuse

    def test_values_written_even_when_osd_off(self):
        from meat3.daemon import meat3d as D
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(D, "OSD_TXT", os.path.join(d, "run.txt")), \
                mock.patch.object(D, "OSD_TMP_DIR", os.path.join(d, "tmpd")), \
                mock.patch.object(D, "OSD_TXT_GAME", os.path.join(d, "tmpd", "osd.txt")), \
                mock.patch.object(D, "_safe_tmp_dir", lambda: D.__dict__["os"].makedirs(os.path.join(d, "tmpd"), exist_ok=True) or True):
            st = D.State.__new__(D.State)
            st.osd = {"enabled": False, "items": ["a"]}
            st.sensors = [{"id": "a", "label": "GPU clock", "value": 3000, "unit": "MHz", "decimals": 0,
                           "group": "GPU/Clocks"}]
            st.write_osd()
            for f in ("run.txt", "tmpd/osd.txt"):
                txt = open(os.path.join(d, f)).read()
                self.assertIn("3000 MHz", txt, f)
