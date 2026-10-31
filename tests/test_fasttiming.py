"""meat3-fasttiming: ta_if buffer formats, command sequencing and memory-clock pinning (no hardware)."""
from __future__ import annotations

import os
import shutil
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from meat3 import fasttiming as FT  # noqa: E402


class FakeTaIf(FT.TaIf):
    """Mimics the kernel: ta_load returns a session id in the buffer, ta_invoke runs a fake TA."""

    def __init__(self, path, fail_load=False, status=0):
        super().__init__(path)
        self.fail_load, self.status = fail_load, status
        self.loads, self.invokes, self.unloads = [], [], []

    def _write(self, name, buf):
        if name == "ta_load":
            ta_type, n = struct.unpack_from("<II", buf, 0)
            assert ta_type == FT.TA_TYPE_RAS and len(buf) == 8 + n
            if self.fail_load:
                raise OSError(22, "Invalid argument")
            self.loads.append(bytes(buf[8:]))
            struct.pack_into("<I", buf, 0, 0x1234)
        elif name == "ta_invoke":
            ta_type, session, cmd, n = struct.unpack_from("<IIII", buf, 0)
            shared = bytearray(buf[16:16 + n])
            assert struct.unpack_from("<I", shared, 0)[0] == cmd
            self.invokes.append((session, cmd, bytes(shared[FT.IN_OFF:FT.IN_OFF + 0x20])))
            struct.pack_into("<II", shared, 4, cmd | 0x80000000, self.status)
            shared[FT.OUT_OFF:FT.OUT_OFF + 8] = struct.pack("<II", 0xAB, 0xCD)
            buf[16:16 + n] = shared
        elif name == "ta_unload":
            self.unloads.append(struct.unpack_from("<II", buf, 0)[1])
        return len(buf)


class TestFastTiming(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meat3-ft-")
        dev = os.path.join(self.tmp, "card1", "device")
        os.makedirs(dev)
        with open(os.path.join(dev, "pp_dpm_mclk"), "w") as f:
            f.write("0: 97Mhz\n1: 1259Mhz *\n")
        with open(os.path.join(dev, "power_dpm_force_performance_level"), "w") as f:
            f.write("auto\n")
        self.card = FT.Card(dev, 1)
        FT.STATE = os.path.join(self.tmp, "state.json")
        self.old_taif, self.old_dbg, self.old_dmesg = FT.TaIf, FT.Card.dbg, FT.dmesg_tail
        FT.Card.dbg = property(lambda c: "/fake/ta_if")
        FT.dmesg_tail = lambda n=6: ""
        self.fake = FakeTaIf("/fake/ta_if")
        FT.TaIf = lambda path: self.fake
        self.writes = []
        self.old_write = FT.Card.write
        FT.Card.write = lambda c, name, val: self.writes.append((name, val))

    def tearDown(self):
        FT.TaIf, FT.Card.dbg, FT.dmesg_tail = self.old_taif, self.old_dbg, self.old_dmesg
        FT.Card.write = self.old_write
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mclk_levels(self):
        self.assertEqual(self.card.mclk_levels(), ([97, 1259], 1))

    def test_load_query_enable_disable_unload(self):
        st = {}
        self.assertEqual(FT.cmd_load(self.card, st, "auto"), 0)
        self.assertEqual((st["session"], st["image"]), (0x1234, "ppta_a.bin"))
        self.assertEqual(self.fake.loads[0][0x10:0x14], b"$PS1")            # the real image, header included
        self.assertEqual(len(self.fake.loads[0]), 0xa100)                   # header + body + signature
        self.assertEqual(FT.cmd_query(self.card, st, 100), 0)
        s, cmd, data = self.fake.invokes[-1]
        self.assertEqual((s, cmd), (0x1234, FT.CMD_QUERY))
        self.assertEqual(struct.unpack_from("<III", data, 0), (125900, 0xFFFFFFFF, 0xFFFFFFFF))
        self.assertEqual(FT.cmd_set(self.card, st, 1, 100, False), 0)
        s, cmd, data = self.fake.invokes[-1]
        self.assertEqual(cmd, FT.CMD_ENABLE)
        self.assertEqual(struct.unpack_from("<8I", data, 0), (0, 0, 0, 0, 0, 0, 125900, 1))
        self.assertEqual(st["level"], 1)
        self.assertEqual(self.writes, [("power_dpm_force_performance_level", "manual"), ("pp_dpm_mclk", "0"),
                                       ("power_dpm_force_performance_level", "auto")])   # pinned one level down, released
        self.writes.clear()
        self.assertEqual(FT.cmd_set(self.card, st, 0, 100, True), 0)
        self.assertEqual(self.fake.invokes[-1][1], FT.CMD_DISABLE)
        self.assertEqual(self.writes, [])                                    # --no-pin: clock untouched
        self.assertEqual(FT.cmd_unload(self.card, st), 0)
        self.assertEqual(self.fake.unloads, [0x1234])
        self.assertNotIn("session", st)

    def test_ta_error_and_load_fallback(self):
        self.fake.status = 1
        st = {"session": 0x1234, "image": "ppta_a.bin"}
        self.assertEqual(FT.cmd_set(self.card, st, 1, 100, False), 1)
        self.assertIsNone(st.get("level"))
        self.fake.fail_load = True
        self.assertEqual(FT.cmd_load(self.card, {}, "auto"), 1)             # every image refused -> no state
        self.assertEqual(self.fake.loads, [])

    def test_taif_packing_against_real_fs(self):
        # TaIf writes to real files: use a temp dir to check the byte layout the kernel will see
        d = os.path.join(self.tmp, "ta_if"); os.makedirs(d)
        for n in ("ta_load", "ta_invoke", "ta_unload"):
            open(os.path.join(d, n), "wb").close()
        ta = self.old_taif(d)
        ta.invoke(7, FT.CMD_ENABLE, struct.pack("<8I", 0, 0, 0, 0, 0, 0, 125900, 1))
        with open(os.path.join(d, "ta_invoke"), "rb") as f:
            raw = f.read()
        self.assertEqual(struct.unpack_from("<IIII", raw, 0), (FT.TA_TYPE_RAS, 7, FT.CMD_ENABLE, FT.BUF_LEN))
        shared = raw[16:]
        self.assertEqual(len(shared), FT.BUF_LEN)
        self.assertEqual(struct.unpack_from("<I", shared, 0)[0], FT.CMD_ENABLE)
        self.assertEqual(struct.unpack_from("<8I", shared, FT.IN_OFF)[6:], (125900, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
