"""Storage backend: NVMe SMART parsing, sysfs discovery, rates, SATA via smartctl JSON."""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from meat3.backends import storage  # noqa: E402
from meat3.backends.storage import StorageBackend, parse_nvme_smart  # noqa: E402


def smart_bytes(**kv) -> bytes:
    """512-byte SMART log built from smartctl-style values (BACONATOR's KC3000)."""
    b = bytearray(512)
    put = lambda off, n, v: b.__setitem__(slice(off, off + n), int(v).to_bytes(n, "little"))
    b[0] = kv.get("critical_warning", 0)
    put(1, 2, kv.get("temperature", 32) + 273)
    b[3], b[4], b[5] = kv.get("available_spare", 100), 10, kv.get("percentage_used", 5)
    for off, k, v in ((32, "dur", 133152235), (48, "duw", 102798108), (64, "hr", 4162166661),
                      (80, "hw", 3008914101), (96, "busy", 4836), (112, "pc", 70), (128, "poh", 28968),
                      (144, "us", 35), (160, "me", 0), (176, "el", 356)):
        put(off, 16, kv.get(k, v))
    put(202, 2, 64 + 273)                      # temperature sensor 2 = 64 °C, sensor 1 absent
    put(224, 4, 90)                            # 90 s in thermal management 1
    put(228, 4, 30)
    return bytes(b)


def w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meat3stor")
        sysr = self.sys = os.path.join(self.tmp, "sys")
        pci = os.path.join(sysr, "devices/pci0000:00/0000:00:01.2/0000:02:00.0")
        ctl = os.path.join(pci, "nvme/nvme0")
        w(f"{ctl}/model", "KINGSTON SKC3000S1024G\n")
        w(f"{ctl}/firmware_rev", "EIFK31.6\n")
        os.symlink(pci, f"{ctl}/device")
        for f, v in (("current_link_speed", "16.0 GT/s PCIe"), ("max_link_speed", "16.0 GT/s PCIe"),
                     ("current_link_width", "4"), ("max_link_width", "4")):
            w(f"{pci}/{f}", v + "\n")
        hw = f"{ctl}/hwmon0"
        for f, v in (("name", "nvme"), ("temp1_input", "30850"), ("temp1_label", "Composite"),
                     ("temp1_max", "84850"), ("temp1_crit", "89850"),
                     ("temp3_input", "63850"), ("temp3_label", "Sensor 2")):
            w(f"{hw}/{f}", v + "\n")
        os.makedirs(f"{sysr}/class/nvme")
        os.symlink(ctl, f"{sysr}/class/nvme/nvme0")
        w(f"{sysr}/block/nvme0n1/size", "2000409264\n")
        self.stat = f"{sysr}/block/nvme0n1/stat"
        w(self.stat, "0 0 1000 0 0 0 2000 0 0 500 0\n")
        # SATA SSD with drivetemp, a sleeping hard drive, and a USB stick
        sda = f"{sysr}/devices/pci0000:00/ata1/host0/target0:0:0/0:0:0:0"
        w(f"{sda}/model", "Samsung SSD 870 EVO 1TB\n"); w(f"{sda}/vendor", "ATA     \n")
        w(f"{sda}/hwmon/hwmon9/name", "drivetemp\n"); w(f"{sda}/hwmon/hwmon9/temp1_input", "35000\n")
        os.makedirs(f"{sysr}/block/sda")
        os.symlink(sda, f"{sysr}/block/sda/device")
        w(f"{sysr}/block/sda/queue/rotational", "0\n"); w(f"{sysr}/block/sda/size", "1953525168\n")
        sdb = f"{sysr}/devices/pci0000:00/ata2/host1/target1:0:0/1:0:0:0"
        w(f"{sdb}/model", "ST4000DM004-2CV1\n"); w(f"{sdb}/vendor", "ATA\n")
        w(f"{sdb}/hwmon/hwmon10/name", "drivetemp\n"); w(f"{sdb}/hwmon/hwmon10/temp1_input", "41000\n")
        os.makedirs(f"{sysr}/block/sdb")
        os.symlink(sdb, f"{sysr}/block/sdb/device")
        w(f"{sysr}/block/sdb/queue/rotational", "1\n")
        sdc = f"{sysr}/devices/pci0000:00/usb2/2-1/2-1:1.0/host2/target2:0:0/2:0:0:0"
        w(f"{sdc}/model", "Flash Drive\n"); w(f"{sdc}/vendor", "SanDisk\n")
        os.makedirs(f"{sysr}/block/sdc")
        os.symlink(sdc, f"{sysr}/block/sdc/device")
        self.smartctl_calls = []
        self.clock = Clock()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_smartctl(self, dev):
        self.smartctl_calls.append(dev)
        if dev.endswith("sdb"):
            return {"smart_status": {"passed": True}, "power_on_time": {"hours": 31000},
                    "temperature": {"current": 38},
                    "ata_smart_attributes": {"table": [
                        {"id": 5, "name": "Reallocated_Sector_Ct", "value": 100, "raw": {"value": 8}},
                        {"id": 197, "name": "Current_Pending_Sector", "value": 100, "raw": {"value": 0}},
                        {"id": 241, "name": "Total_LBAs_Written", "value": 100, "raw": {"value": 2 * 10 ** 10}}]}}
        return {"smart_status": {"passed": True}, "power_on_time": {"hours": 1200},
                "ata_smart_attributes": {"table": [
                    {"id": 177, "name": "Wear_Leveling_Count", "value": 97, "raw": {"value": 31}}]}}

    def backend(self, smart=smart_bytes()):
        return StorageBackend(sys_root=self.sys, dev_root="/dev", clock=self.clock,
                              smart_reader=lambda dev: smart, smartctl=self.fake_smartctl)

    def sens(self, b):
        return {s.id: s for s in b.sensors()}

    def test_parse_matches_smartctl(self):
        h = parse_nvme_smart(smart_bytes())
        self.assertEqual((h["temperature"], h["percentage_used"], h["power_on_hours"], h["unsafe_shutdowns"]),
                         (32, 5, 28968, 35))
        self.assertEqual(h["data_units_written"], 102798108)
        self.assertEqual(h["temp_sensors"][:2], [None, 64])
        self.assertEqual(storage._PASSTHRU.size, 72)

    def test_nvme_sensors(self):
        s = self.sens(self.backend())
        self.assertEqual(s["storage.nvme0.t.temp1"].label, "Composite")
        self.assertAlmostEqual(s["storage.nvme0.t.temp1"].value, 30.85)
        self.assertIn("warning 85 °C", s["storage.nvme0.t.temp1"].hint)
        self.assertEqual(s["storage.nvme0.t.temp3"].label, "Sensor 2")
        self.assertTrue(s["storage.nvme0.t.temp1"].group.startswith("Storage/NVMe 0: KINGSTON"))
        self.assertAlmostEqual(s["storage.nvme0.smart.written"].value, 52.63, places=2)
        self.assertEqual(s["storage.nvme0.smart.left"].value, 95)
        self.assertEqual(s["storage.nvme0.smart.poh"].value, 28968)
        self.assertAlmostEqual(s["storage.nvme0.smart.throttle_time"].value, 2.0)
        self.assertEqual((s["storage.nvme0.pcie.gen"].value, s["storage.nvme0.pcie.width"].value), (4, 4))
        self.assertAlmostEqual(s["storage.nvme0.info.size"].value, 1024.2, places=1)

    def test_nvme_unreadable_keeps_rows(self):
        s = self.sens(self.backend(smart=None))
        self.assertIsNone(s["storage.nvme0.smart.used"].value)
        self.assertIn("not readable", s["storage.nvme0.smart.used"].hint)

    def test_smart_cached(self):
        n = [0]

        def reader(dev):
            n[0] += 1
            return smart_bytes()
        b = StorageBackend(sys_root=self.sys, clock=self.clock, smart_reader=reader, smartctl=self.fake_smartctl)
        b.sensors(); self.clock.t += 5; b.sensors()
        self.assertEqual(n[0], 1)
        self.clock.t += 61; b.sensors()
        self.assertEqual(n[0], 2)

    def test_rates(self):
        b = self.backend()
        s = self.sens(b)
        self.assertIsNone(s["storage.nvme0.io.read"].value)       # needs two samples
        w(self.stat, "0 0 205800 0 0 0 4002000 0 0 1000 0\n")     # +100 MB read, +2 GB written, +500 ms busy
        self.clock.t += 2
        s = self.sens(b)
        self.assertAlmostEqual(s["storage.nvme0.io.read"].value, 52.4288, places=3)
        self.assertAlmostEqual(s["storage.nvme0.io.write"].value, 1024.0, places=1)
        self.assertAlmostEqual(s["storage.nvme0.io.busy"].value, 25.0)

    def test_sata_and_usb(self):
        s = self.sens(self.backend())
        self.assertEqual(s["storage.sda.t.temp1"].value, 35.0)                # SSD: drivetemp
        self.assertEqual(s["storage.sda.smart.a177"].value, 97)               # normalised wear
        self.assertTrue(s["storage.sda.t.temp1"].group.endswith("SATA sda: Samsung SSD 870 EVO 1TB"))
        self.assertNotIn("storage.sdb.t.temp1", s)                            # HDD: no drivetemp read
        self.assertEqual(s["storage.sdb.t.smart"].value, 38)
        self.assertEqual(s["storage.sdb.smart.a5"].value, 8)
        self.assertAlmostEqual(s["storage.sdb.smart.written"].value, 10.24)
        self.assertNotIn("storage.sdc.smart.health", s)                       # USB: no SMART
        self.assertIn("storage.sdc.io.read", s)
        self.assertNotIn("/dev/sdc", self.smartctl_calls)


if __name__ == "__main__":
    unittest.main()
