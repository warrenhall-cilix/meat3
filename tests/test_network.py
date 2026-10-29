"""Network backend: interface discovery, rates, link state, Wi-Fi, TCP, overview rows, Prometheus."""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from meat3.backends.network import NetworkBackend, parse_snmp_tcp, parse_wireless  # noqa: E402
from meat3 import tui  # noqa: E402
from meat3.web.server import prometheus_text  # noqa: E402

WIRELESS = """Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE
 face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22
wlp9s0: 0000   54.  -56.  -256        0      0      0      0     12        0
"""
SNMP = """Tcp: RtoAlgorithm RtoMin RtoMax MaxConn ActiveOpens PassiveOpens AttemptFails EstabResets CurrEstab InSegs OutSegs RetransSegs InErrs OutRsts InCsumErrors
Tcp: 1 200 120000 -1 100 5 0 0 {est} 1000 {out} {retr} 0 0 0
"""


def w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(text))


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


class NetworkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.sys, self.proc = os.path.join(self.tmp, "sys"), os.path.join(self.tmp, "proc")
        net = os.path.join(self.sys, "class/net")
        # BACONATOR's 2.5 GbE port, a Wi-Fi card, WireGuard, plus things that must be skipped
        self.iface("enp14s0", "devices/pci0000:00/0000:0e:00.0", "r8169", speed=2500)
        self.iface("wlp9s0", "devices/pci0000:00/0000:09:00.0", "iwlwifi", speed=-1, wifi=True)
        self.iface("wg0", None, None, oper="unknown", carrier=1)
        self.iface("lo", None, None, oper="unknown")
        self.iface("docker0", None, None)
        self.iface("veth1234", None, None)
        self.iface("vmbr0", None, None)
        os.makedirs(os.path.join(net, "vmbr0/bridge"))
        self.iface("eno2", "devices/pci0000:00/0000:0f:00.0", "igb", oper="down", carrier=0, speed=-1)
        w(os.path.join(self.proc, "net/wireless"), WIRELESS)
        self.snmp(3, 10000, 0)
        self.clock = Clock()

    def iface(self, n, dev, drv, speed=None, oper="up", carrier=1, wifi=False):
        d = os.path.join(self.sys, "class/net", n)
        os.makedirs(d)
        if dev:
            ddir = os.path.join(self.sys, dev)
            os.makedirs(os.path.join(self.sys, "bus/pci/drivers", drv), exist_ok=True)
            os.makedirs(ddir)
            os.symlink(os.path.join(self.sys, "bus/pci/drivers", drv), os.path.join(ddir, "driver"))
            os.symlink(ddir, os.path.join(d, "device"))
        if wifi:
            os.makedirs(os.path.join(d, "wireless"))
        w(os.path.join(d, "operstate"), oper)
        w(os.path.join(d, "carrier"), carrier)
        if speed is not None:
            w(os.path.join(d, "speed"), speed)
        self.count(n, 0, 0)

    def count(self, n, rx, tx, rx_err=0):
        st = os.path.join(self.sys, "class/net", n, "statistics")
        for k, v in (("rx_bytes", rx), ("tx_bytes", tx), ("rx_packets", rx // 1000), ("tx_packets", tx // 1000),
                     ("rx_errors", rx_err), ("tx_errors", 0), ("rx_dropped", 7), ("tx_dropped", 0)):
            w(os.path.join(st, k), v)

    def snmp(self, est, out, retr):
        w(os.path.join(self.proc, "net/snmp"), SNMP.format(est=est, out=out, retr=retr))

    def backend(self):
        return NetworkBackend(self.sys, self.proc, self.clock)

    def vals(self, b):
        return {s.id: s for s in b.sensors()}

    def test_discovery(self):
        b = self.backend()
        self.assertEqual([i["name"] for i in b.ifaces], ["eno2", "enp14s0", "wg0", "wlp9s0"])
        t = {i["name"]: i["title"] for i in b.ifaces}
        self.assertEqual(t["enp14s0"], "enp14s0: Ethernet (r8169)")
        self.assertEqual(t["wlp9s0"], "wlp9s0: Wi-Fi (iwlwifi)")
        self.assertEqual(t["wg0"], "wg0: VPN")

    def test_rates_link_and_totals(self):
        b = self.backend()
        v = self.vals(b)
        self.assertIsNone(v["net.enp14s0.rx"].value)                 # first sample: no rate yet
        self.assertEqual(v["net.enp14s0.speed"].value, 2500)
        self.assertEqual(v["net.enp14s0.link"].value, 1)
        self.assertEqual(v["net.eno2.link"].value, 0)
        self.assertIsNone(v["net.eno2.speed"].value)
        self.assertEqual(v["net.wg0.link"].value, 1)                  # tunnels: operstate unknown + carrier
        self.assertNotIn("net.wlp9s0.speed", v)
        self.clock.t += 2
        self.count("enp14s0", 250_000_000, 25_000_000)                # 1000 / 100 Mbit/s over 2 s
        self.count("wlp9s0", 2_500_000, 0)
        self.count("wg0", 10_000_000, 0)
        v = self.vals(b)
        self.assertAlmostEqual(v["net.enp14s0.rx"].value, 1000.0)
        self.assertAlmostEqual(v["net.enp14s0.tx"].value, 100.0)
        self.assertAlmostEqual(v["net.enp14s0.util"].value, 40.0)     # 1000 of 2500 Mbit/s
        self.assertAlmostEqual(v["net.enp14s0.rx_total"].value, 0.25)
        self.assertEqual(v["net.enp14s0.rx_pps"].value, 125000)   # 250k packets / 2 s
        self.assertEqual(v["net.enp14s0.drops"].value, 7)
        # total = physical interfaces only (wg0's traffic also crosses a NIC)
        self.assertAlmostEqual(v["net.total.rx"].value, 1010.0)
        self.assertAlmostEqual(v["net.total.tx"].value, 100.0)
        self.assertEqual(v["net.wlp9s0.wifi.signal"].value, -56)
        self.assertEqual(v["net.wlp9s0.wifi.quality"].value, 54)

    def test_counter_reset_gives_no_spike(self):
        b = self.backend()
        self.count("enp14s0", 10**12, 0)
        self.clock.t += 1
        b.sensors()
        self.count("enp14s0", 5000, 0)                                # interface re-created
        self.clock.t += 1
        self.assertIsNone(self.vals(b)["net.enp14s0.rx"].value)

    def test_tcp(self):
        b = self.backend()
        self.assertEqual(self.vals(b)["net.tcp.established"].value, 3)
        self.clock.t += 1
        self.snmp(4, 12000, 50)                                       # 50 of 2000 segments resent
        self.assertAlmostEqual(self.vals(b)["net.tcp.retrans"].value, 2.5)

    def test_new_interface_appears(self):
        b = self.backend()
        self.iface("usb0", "devices/pci0000:00/0000:00:14.0/usb1/1-2/1-2:1.0", "rndis_host")
        self.clock.t += 11
        v = self.vals(b)
        self.assertIn("net.usb0.rx", v)
        self.assertIn("USB network", v["net.usb0.rx"].group)

    def test_parsers(self):
        self.assertEqual(parse_wireless(WIRELESS), {"wlp9s0": (54.0, -56.0)})
        self.assertEqual(parse_wireless(""), {})
        self.assertEqual(parse_snmp_tcp(SNMP.format(est=1, out=2, retr=3))["RetransSegs"], 3)
        self.assertEqual(parse_snmp_tcp("garbage"), {})

    def test_overview_and_prometheus(self):
        b = self.backend()
        b.sensors()
        self.clock.t += 1
        self.count("enp14s0", 12_500_000, 0)
        sensors = [s.to_dict() for s in b.sensors()]
        by_id = {s["id"]: s for s in sensors}
        order = [s["id"] for s in sensors]
        self.assertEqual(tui.overview_devices(order, by_id), [("net", "Network")])
        rows = tui.overview_rows("net", order, by_id)
        labels = [r[0] for r in rows]
        self.assertIn("enp14s0 ↓", labels)
        self.assertNotIn("eno2 ↓", labels)                            # link down: not in the overview
        self.assertIn("wlp9s0 signal", labels)
        self.assertEqual(dict((r[0], r[2]) for r in rows)["enp14s0 ↓"], 2500)
        meta = {"sensors": sensors}
        values = {"connected": True, "v": {s["id"]: [s["value"]] for s in sensors}, "host": "h", "installed": "x"}
        text = prometheus_text(meta, values)
        self.assertIn('meat3_throughput_bytes_per_second{id="net.enp14s0.rx"', text)
        line = next(l for l in text.splitlines() if 'id="net.enp14s0.rx"' in l)
        self.assertAlmostEqual(float(line.rsplit(" ", 1)[1]), 12_500_000)   # 100 Mbit/s -> bytes/s


if __name__ == "__main__":
    unittest.main()
