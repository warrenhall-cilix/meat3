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
