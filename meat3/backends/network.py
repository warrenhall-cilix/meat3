"""Network backend: per-interface bandwidth, totals, link state/speed, errors, Wi-Fi signal, TCP.

Per interface (physical NICs, plus VPN tunnels: WireGuard, Tailscale, ZeroTier, tun/tap):
  * download/upload rate from /sys/class/net/<if>/statistics byte counters, in Mbit/s like a speed test
  * utilisation against the negotiated link speed (Ethernet; Wi-Fi has no fixed speed in sysfs)
  * received/sent since boot, packets/s, errors and drops (counts since boot)
  * Wi-Fi link quality and signal from /proc/net/wireless
System-wide: total rates over the physical interfaces, established TCP connections and the TCP
retransmit rate (/proc/net/snmp), a sign of a lossy link.

Containers and VMs (veth, docker, bridges, virbr) are skipped: their traffic also crosses a physical
NIC or never leaves the machine. Read-only, no root needed.
"""
from __future__ import annotations

import os
import re
import time
from typing import Optional

from meat3.core.model import Backend, Sensor
from meat3.backends._cpu_common import _listdir, read_str, slug

RESCAN_S = 10.0
VPN = re.compile(r"^(wg|tun|tap|tailscale|zt|nordlynx|proton|mullvad)")
SKIP = re.compile(r"^(lo|veth|docker|br-|virbr|vnet|fwbr|fwpr|fwln|tap\d+i)")    # tap<vmid>i<n> = Proxmox VM


def _int(path: str) -> Optional[int]:
    v = read_str(path)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def parse_wireless(text: str) -> dict:
    """/proc/net/wireless -> {iface: (quality, signal dBm)}."""
    out = {}
    for line in (text or "").splitlines()[2:]:
        m = re.match(r"\s*([^:\s]+):\s+\S+\s+([-\d.]+)\s+([-\d.]+)", line)
        if m:
            q, lvl = float(m.group(2)), float(m.group(3))
            out[m.group(1)] = (q, lvl if lvl < 0 else (lvl - 256 if lvl > 63 else None))   # some drivers report unsigned dBm
    return out


def parse_snmp_tcp(text: str) -> dict:
    """The Tcp: header/value line pair of /proc/net/snmp as a dict."""
    rows = [l.split()[1:] for l in (text or "").splitlines() if l.startswith("Tcp:")]
    if len(rows) < 2:
        return {}
    try:
        return dict(zip(rows[0], (int(x) for x in rows[1])))
    except ValueError:
        return {}


class _Rate:
    """Per-second deltas of monotonically increasing counters (None on the first sample or a reset)."""

    def __init__(self):
        self.prev: dict[str, tuple[float, int]] = {}

    def __call__(self, key: str, value: Optional[int], now: float) -> Optional[float]:
        if value is None:
            self.prev.pop(key, None)
            return None
        prev, self.prev[key] = self.prev.get(key), (now, value)
        if not prev or now <= prev[0] or value < prev[1]:    # counter reset (driver reload, interface re-created)
            return None
        return (value - prev[1]) / (now - prev[0])


class NetworkBackend(Backend):
    kind = "network"
    name = "Network"

    def __init__(self, sys_root: str = "/sys", proc_root: str = "/proc", clock=time.monotonic):
        self.sys_root, self.proc_root, self.clock = sys_root, proc_root, clock
        self.ifaces: list[dict] = []
        self.rate = _Rate()
        self.last_scan = -1e9
        self._scan()

    @classmethod
    def detect(cls) -> list["NetworkBackend"]:
        try:
            b = cls()
            return [b] if b.ifaces else []
        except Exception:
            return []

    def _scan(self):
        root = os.path.join(self.sys_root, "class/net")
        out = []
        for n in sorted(_listdir(root)):
            d = os.path.join(root, n)
            if SKIP.match(n) or os.path.isdir(os.path.join(d, "bridge")):
                continue
            phys = os.path.exists(os.path.join(d, "device"))
            if not phys and not VPN.match(n):
                continue
            wifi = os.path.isdir(os.path.join(d, "wireless")) or os.path.isdir(os.path.join(d, "phy80211"))
            drv = os.path.basename(os.path.realpath(os.path.join(d, "device/driver"))) if phys else ""
            if not phys:
                kind = "VPN"
            elif wifi:
                kind = "Wi-Fi"
            elif "/usb" in os.path.realpath(os.path.join(d, "device")):
                kind = "USB network"
            else:
                kind = "Ethernet"
            title = f"{n}: {kind}" + (f" ({drv})" if drv else "")
            out.append({"name": n, "id": slug(n), "dir": d, "phys": phys, "wifi": wifi, "kind": kind, "title": title})
        self.ifaces = out
        self.last_scan = self.clock()

    def sensors(self) -> list[Sensor]:
        now = self.clock()
        if now - self.last_scan > RESCAN_S:
            self._scan()
        wl = parse_wireless(read_str(os.path.join(self.proc_root, "net/wireless")) or "")
        out: list[Sensor] = []
        tot_rx = tot_tx = None
        for i in self.ifaces:
            p, g, st = f"net.{i['id']}", f"Network/{i['title']}", os.path.join(i["dir"], "statistics")
            S = lambda key, label, v, unit="", dec=0, hint="": out.append(Sensor(f"{p}.{key}", g, label, v, unit, dec, hint))
            c = lambda k: _int(os.path.join(st, k))
            rxb, txb = c("rx_bytes"), c("tx_bytes")
            rx, tx = self.rate(f"{p}.rx", rxb, now), self.rate(f"{p}.tx", txb, now)
            rx = None if rx is None else rx * 8 / 1e6
            tx = None if tx is None else tx * 8 / 1e6
            if i["phys"] and rx is not None:
                tot_rx, tot_tx = (tot_rx or 0) + rx, (tot_tx or 0) + (tx or 0)
            oper = read_str(os.path.join(i["dir"], "operstate"))
            carrier = _int(os.path.join(i["dir"], "carrier")) if oper != "down" else 0
            up = 1 if oper == "up" or (oper == "unknown" and carrier) else 0
            speed = _int(os.path.join(i["dir"], "speed")) if up else None
            speed = speed if speed and speed > 0 else None        # -1 / absent: Wi-Fi, VPN, link down
            src = f"/sys/class/net/{i['name']}/statistics"
            S("rx", "Download", rx, "Mbit/s", 1, f"{src}; megabits per second, like a speed test (÷ 8 for MB/s)")
            S("tx", "Upload", tx, "Mbit/s", 1, f"{src}; megabits per second")
            if speed:
                busy = max(rx or 0, tx or 0) / speed * 100 if rx is not None else None
                S("util", "Link utilisation", None if busy is None else min(100.0, busy), "%", 0,
                  f"busier direction against the {speed} Mbit/s link (full duplex: each direction has the full speed)")
            S("rx_total", "Downloaded since boot", None if rxb is None else rxb / 1e9, "GB", 2, src)
            S("tx_total", "Uploaded since boot", None if txb is None else txb / 1e9, "GB", 2, src)
            S("rx_pps", "Packets received", self.rate(f"{p}.rxp", c("rx_packets"), now), "/s", 0, src)
            S("tx_pps", "Packets sent", self.rate(f"{p}.txp", c("tx_packets"), now), "/s", 0, src)
            errs = [c(k) for k in ("rx_errors", "tx_errors")]
            drops = [c(k) for k in ("rx_dropped", "tx_dropped")]
            S("errors", "Errors since boot", None if None in errs else sum(errs), "", 0,
              src + "; CRC/frame/FIFO errors. Rising = bad cable, port or driver")
            S("drops", "Dropped since boot", None if None in drops else sum(drops), "", 0,
              src + "; usually harmless (unknown protocols, full queues), unless it climbs fast")
            S("link", "Link up", up, "", 0, f"operstate {oper or '?'}; 1 = connected")
            if i["phys"] and not i["wifi"]:
                S("speed", "Link speed", speed, "Mbit/s", 0,
                  "negotiated with the switch/router. Lower than the port supports = cable or port problem")
            if i["wifi"]:
                q = wl.get(i["name"])
                S("wifi.quality", "Wi-Fi link quality", q[0] if q else None, "/70", 0, "/proc/net/wireless")
                S("wifi.signal", "Wi-Fi signal", q[1] if q else None, "dBm", 0,
                  "/proc/net/wireless; -50 excellent, -67 good for calls/games, below -75 unreliable")
        g = "Network/All interfaces"
        out.append(Sensor("net.total.rx", g, "Download (total)", tot_rx, "Mbit/s", 1, "sum over the physical interfaces"))
        out.append(Sensor("net.total.tx", g, "Upload (total)", tot_tx, "Mbit/s", 1, "sum over the physical interfaces"))
        tcp = parse_snmp_tcp(read_str(os.path.join(self.proc_root, "net/snmp")) or "")
        out.append(Sensor("net.tcp.established", g, "TCP connections", tcp.get("CurrEstab"), "", 0,
                          "established TCP connections (/proc/net/snmp)"))
        segs = self.rate("tcp.out", tcp.get("OutSegs"), now)
        retr = self.rate("tcp.retr", tcp.get("RetransSegs"), now)
        out.append(Sensor("net.tcp.retrans", g, "TCP retransmits", None if segs is None or retr is None
                          else (retr / segs * 100 if segs > 0 else 0.0), "%", 2,
                          "share of sent TCP segments that were resends. Sustained > 1% = packet loss somewhere"))
        return out
