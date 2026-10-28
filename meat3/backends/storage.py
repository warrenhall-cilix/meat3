"""Storage backend: NVMe and SATA drives (temperatures, health/SMART, PCIe link, throughput).

Per drive:
  * temperatures from hwmon (nvme driver: Composite + Sensor N; SATA: the `drivetemp` driver)
  * NVMe health from the SMART / Health Information log (Get Log Page 0x02, the read-only admin
    command `smartctl -a` and `nvme smart-log` send). Layout: NVMe Base Specification, Figure
    "SMART / Health Information Log Page". Read every SMART_EVERY_S seconds, root only.
  * SATA SMART via `smartctl -j -A -H -i -n standby` if smartctl is installed; `-n standby` never
    spins up a sleeping hard drive. USB drives are skipped (bridges often mangle SMART).
  * Health reads run in a worker thread: a slow or failing drive never stalls the daemon's poll.
  * PCIe link speed/width (NVMe) against the maximum, to spot a drive in a slower slot
  * read/write rate and busy % from /sys/block/<dev>/stat

Read-only.
"""
from __future__ import annotations

import ctypes
import fcntl
import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time
from typing import Callable, Optional

from meat3.core.model import Backend, Sensor
from meat3.backends._cpu_common import _listdir, hwmon_channels, read_hwmon_value, read_str

SMART_EVERY_S = 60.0
SATA_SMART_EVERY_S = 300.0
RESCAN_S = 30.0

NVME_IOCTL_ADMIN_CMD = 0xC0484E41          # _IOWR('N', 0x41, struct nvme_passthru_cmd), 72 bytes
_PASSTHRU = struct.Struct("<BBHIIIQQII6III")
NVME_ADMIN_GET_LOG_PAGE = 0x02
NVME_LOG_SMART = 0x02

CRIT_WARN_BITS = ["spare below threshold", "temperature out of range", "reliability degraded",
                  "read-only mode", "volatile backup failed", "persistent memory read-only"]


def nvme_get_smart_log(dev_path: str) -> Optional[bytes]:
    """512-byte SMART/Health log of an NVMe controller (/dev/nvmeN), or None."""
    try:
        fd = os.open(dev_path, os.O_RDONLY)
    except OSError:
        return None
    try:
        buf = ctypes.create_string_buffer(512)
        numd = 512 // 4 - 1
        # timeout_ms 0 = the driver's admin_timeout (60 s), like smartctl/nvme-cli: an admin command
        # that times out makes nvme-pci reset the controller, so never pass a short one
        cmd = bytearray(_PASSTHRU.pack(NVME_ADMIN_GET_LOG_PAGE, 0, 0, 0xFFFFFFFF, 0, 0, 0,
                                       ctypes.addressof(buf), 0, 512,
                                       (numd << 16) | NVME_LOG_SMART, 0, 0, 0, 0, 0, 0, 0))
        if fcntl.ioctl(fd, NVME_IOCTL_ADMIN_CMD, cmd) != 0:
            return None                  # >0 = NVMe status (e.g. log page unsupported): no data
        return buf.raw
    except OSError:
        return None
    finally:
        os.close(fd)


def parse_nvme_smart(b: bytes) -> dict:
    """Decode the SMART/Health log (NVMe Base Spec 2.0, log identifier 02h)."""
    u = lambda lo, hi: int.from_bytes(b[lo:hi + 1], "little")
    k2c = lambda k: (k - 273) if k else None
    return {
        "critical_warning": b[0],
        "temperature": k2c(u(1, 2)),
        "available_spare": b[3],
        "available_spare_threshold": b[4],
        "percentage_used": b[5],
        "data_units_read": u(32, 47),          # x 1000 x 512 bytes
        "data_units_written": u(48, 63),
        "host_reads": u(64, 79),
        "host_writes": u(80, 95),
        "controller_busy_time": u(96, 111),    # minutes
        "power_cycles": u(112, 127),
        "power_on_hours": u(128, 143),
        "unsafe_shutdowns": u(144, 159),
        "media_errors": u(160, 175),
        "num_err_log_entries": u(176, 191),
        "warning_temp_time": u(192, 195),      # minutes
        "critical_comp_time": u(196, 199),
        "temp_sensors": [k2c(u(200 + 2 * i, 201 + 2 * i)) for i in range(8)],
        "thm_temp1_trans_count": u(216, 219),
        "thm_temp2_trans_count": u(220, 223),
        "thm_temp1_total_time": u(224, 227),   # seconds
        "thm_temp2_total_time": u(228, 231),
    }


_SMARTCTL: list = []


def smartctl_path() -> Optional[str]:
    if not _SMARTCTL:
        _SMARTCTL.append(shutil.which("smartctl"))
    return _SMARTCTL[0]


def run_smartctl(dev: str) -> Optional[dict]:
    exe = smartctl_path()
    if not exe:
        return None
    try:
        p = subprocess.run([exe, "-j", "-A", "-H", "-i", "-n", "standby", dev], capture_output=True,
                           text=True, timeout=15)
        return json.loads(p.stdout or "null")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


# SATA SMART attributes worth showing: id -> (label, unit, use "value" (normalised) instead of raw)
ATA_ATTRS = {
    5: ("Reallocated sectors", "", False),
    197: ("Pending sectors", "", False),
    198: ("Offline uncorrectable", "", False),
    199: ("Interface CRC errors", "", False),
    12: ("Power cycles", "", False),
    192: ("Unsafe shutdowns", "", False),        # SSDs; on hard drives it's the power-off retract count
    177: ("Wear levelling (normalised)", "%", True),
    202: ("Life left (normalised)", "%", True),
    231: ("Life left (normalised)", "%", True),
    233: ("Media wearout (normalised)", "%", True),
}


class _Rates:
    """Read/write rate and busy % from /sys/block/<dev>/stat deltas."""

    def __init__(self):
        self.prev: dict[str, tuple] = {}

    def sample(self, block: str, stat_path: str, now: float):
        txt = read_str(stat_path)
        try:
            f = [int(x) for x in txt.split()]
            cur = (now, f[2], f[6], f[9])       # sectors read, sectors written, io_ticks (ms)
        except (AttributeError, ValueError, IndexError):
            return None, None, None
        prev, self.prev[block] = self.prev.get(block), cur
        if not prev or cur[0] <= prev[0]:
            return None, None, None
        dt = cur[0] - prev[0]
        rd = max(0, cur[1] - prev[1]) * 512 / dt / 1e6
        wr = max(0, cur[2] - prev[2]) * 512 / dt / 1e6
        busy = min(100.0, max(0, cur[3] - prev[3]) / (dt * 10.0))
        return rd, wr, busy


class StorageBackend(Backend):
    kind = "storage"
    name = "Storage"

    def __init__(self, sys_root: str = "/sys", dev_root: str = "/dev", clock=time.monotonic,
                 smart_reader: Callable[[str], Optional[bytes]] = nvme_get_smart_log,
                 smartctl: Callable[[str], Optional[dict]] = run_smartctl, background: bool = True):
        self.sys_root, self.dev_root, self.clock = sys_root, dev_root, clock
        self.smart_reader, self.smartctl = smart_reader, smartctl
        self.background = background
        self.drives: list[dict] = []
        self.smart: dict[str, tuple[float, object]] = {}      # drive id -> (read time, data)
        self.smart_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self.rates = _Rates()
        self.last_scan = -1e9
        self._scan()

    @classmethod
    def detect(cls) -> list["StorageBackend"]:
        try:
            b = cls()
            return [b] if b.drives else []
        except Exception:
            return []

    # -- discovery
    def _hwmon_of(self, dev_dir: str) -> Optional[str]:
        for d in _listdir(os.path.join(dev_dir, "hwmon")):
            return os.path.join(dev_dir, "hwmon", d)
        for d in _listdir(dev_dir):                     # older kernels: hwmonN directly under the device
            if re.fullmatch(r"hwmon\d+", d):
                return os.path.join(dev_dir, d)
        return None

    def _scan(self):
        drives = []
        nvme_cls = os.path.join(self.sys_root, "class/nvme")
        for c in sorted(_listdir(nvme_cls), key=lambda n: int(re.sub(r"\D", "", n) or 0)):
            if not re.fullmatch(r"nvme\d+", c):
                continue
            cdir = os.path.join(nvme_cls, c)
            model = (read_str(os.path.join(cdir, "model")) or "").strip()
            pci = os.path.realpath(os.path.join(cdir, "device"))
            # namespaces as the controller lists them (with native multipath the head disk is named
            # after the subsystem, not the controller); hidden per-path disks only as a fallback
            ents = _listdir(cdir)
            blocks = sorted(b for b in ents if re.fullmatch(r"nvme\d+n\d+", b)) or \
                sorted(b for b in ents if re.fullmatch(r"nvme\d+c\d+n\d+", b))
            hw = None
            for d in ents:                             # nvme puts hwmonN in the controller dir
                if re.fullmatch(r"hwmon\d+", d):
                    hw = os.path.join(cdir, d)
            hw = hw or self._hwmon_of(cdir)
            drives.append({"id": c, "type": "nvme", "title": f"NVMe {c[4:]}: {model or c}", "model": model,
                           "dev": os.path.join(self.dev_root, c), "pci": pci, "blocks": blocks,
                           "hwmon": hw, "fw": (read_str(os.path.join(cdir, "firmware_rev")) or "").strip()})
        block_root = os.path.join(self.sys_root, "block")
        for b in sorted(_listdir(block_root)):
            if not re.fullmatch(r"sd[a-z]+", b):
                continue
            ddir = os.path.join(block_root, b, "device")
            path = os.path.realpath(ddir)
            usb = "/usb" in path
            vendor = (read_str(os.path.join(ddir, "vendor")) or "").strip()
            model = (read_str(os.path.join(ddir, "model")) or "").strip()
            if vendor and vendor.upper() not in ("ATA",) and not model.startswith(vendor):
                model = f"{vendor} {model}"
            # id from the ATA port, not sdX: sdX letters move when another disk or a USB stick appears
            port = re.search(r"/(ata\d+)/", path)
            did = port.group(1) if port and not usb else f"usb_{b}" if usb else b
            drives.append({"id": did, "type": "usb" if usb else "sata",
                           "title": f"{'USB' if usb else 'SATA'} {b}: {model or b}", "model": model,
                           "dev": os.path.join(self.dev_root, b), "blocks": [b],
                           "hwmon": self._hwmon_of(ddir), "rotational":
                               read_str(os.path.join(block_root, b, "queue/rotational")) == "1"})
        for d in drives:
            size = 0
            for blk in d["blocks"]:
                try:
                    size += int(read_str(os.path.join(block_root, blk, "size")) or 0) * 512
                except ValueError:
                    pass
            d["size_gb"] = size / 1e9 if size else None
            d["temps"] = self._temp_channels(d)
        self.drives = drives
        self.last_scan = self.clock()

    def _temp_channels(self, d: dict) -> list[tuple]:
        """(channel, label, hint) per temperature, resolved once per scan: on NVMe every read of
        temp*_max is an admin command (Get Features), so limits aren't re-read each poll.
        Hard drives: none (temperature comes from smartctl, as drivetemp reads can keep them awake)."""
        hw = d.get("hwmon")
        if not hw or d.get("rotational"):
            return []
        out = []
        for ch in hwmon_channels(hw):
            if ch["kind"] != "temp":
                continue
            lab = ch["label"]
            if lab == ch["key"]:
                lab = "Temperature" if ch["idx"] == 1 else f"Temperature {ch['idx']}"
            hint = f"hwmon {os.path.basename(hw)}"
            lim = []
            for suf, nm in (("max", "warning"), ("crit", "critical")):
                v = read_str(os.path.join(hw, f"temp{ch['idx']}_{suf}"))
                if v and v.lstrip("-").isdigit() and -40000 < int(v) < 200000:
                    lim.append(f"{nm} {int(v) / 1000:.0f} °C")
            if lim:
                hint += "; drive's own limits: " + ", ".join(lim)
            if lab == "Composite":
                hint += "; Composite is the drive's overall temperature (what it throttles on)"
            out.append((ch, lab, hint))
        return out

    # -- health reads (worker thread)
    def _due(self) -> list[tuple]:
        now = self.clock()
        jobs = []
        for d in self.drives:
            if d["type"] == "nvme":
                every, fn = SMART_EVERY_S, (lambda dev=d["dev"]: self.smart_reader(dev))
            elif d["type"] == "sata":
                every, fn = SATA_SMART_EVERY_S, (lambda dev=d["dev"]: self.smartctl(dev))
            else:
                continue
            with self.smart_lock:
                t = self.smart.get(d["id"], (-1e9, None))[0]
            if now - t >= every:
                jobs.append((d["id"], fn))
        return jobs

    def _run_jobs(self, jobs: list[tuple]):
        for key, fn in jobs:
            try:
                val = fn()
            except Exception:
                val = None
            with self.smart_lock:
                self.smart[key] = (self.clock(), val)    # failures wait a full interval too

    def _refresh_health(self):
        if self._worker is not None and self._worker.is_alive():
            return                                     # previous round still running (slow drive)
        jobs = self._due()
        if not jobs:
            return
        if not self.background:
            self._run_jobs(jobs)
            return
        with self.smart_lock:                          # mark as started so they aren't queued twice
            for key, _ in jobs:
                old = self.smart.get(key, (-1e9, None))[1]
                self.smart[key] = (self.clock(), old)
        self._worker = threading.Thread(target=self._run_jobs, args=(jobs,), name="meat3-storage-smart",
                                        daemon=True)
        self._worker.start()

    def _health(self, key: str):
        with self.smart_lock:
            return self.smart.get(key, (0, None))[1]

    # -- polling

    def sensors(self) -> list[Sensor]:
        now = self.clock()
        if now - self.last_scan > RESCAN_S:
            self._scan()
        self._refresh_health()
        out: list[Sensor] = []
        for d in self.drives:
            p = f"storage.{d['id']}"
            g = f"Storage/{d['title']}"
            S = lambda key, label, v, unit="", dec=0, hint="", grp=g: out.append(
                Sensor(f"{p}.{key}", grp, label, v, unit, dec, hint))
            for ch, lab, hint in d["temps"]:
                S(f"t.{ch['key']}", lab, read_hwmon_value(ch), "°C", 0, hint)
            # throughput
            rd = wr = busy = None
            for blk in d["blocks"]:
                r = self.rates.sample(blk, os.path.join(self.sys_root, "block", blk, "stat"), now)
                if r[0] is not None:
                    rd, wr = (rd or 0) + r[0], (wr or 0) + r[1]
                    busy = max(busy or 0, r[2])
            S("io.read", "Read rate", rd, "MB/s", 1, "from /sys/block/*/stat")
            S("io.write", "Write rate", wr, "MB/s", 1, "from /sys/block/*/stat")
            S("io.busy", "Active time", busy, "%", 0, "share of time with I/O in flight (like Task Manager's 'Active time')")
            if d["type"] == "nvme":
                self._nvme(d, S)
            elif d["type"] == "sata":
                self._sata(d, S)
            S("info.size", "Capacity", d["size_gb"], "GB", 0, f"{d['model']}" + (f", firmware {d['fw']}" if d.get("fw") else ""))
        return out

    def _nvme(self, d: dict, S):
        pci = d["pci"]

        def gts(f):
            m = re.match(r"([\d.]+)\s*GT/s", read_str(os.path.join(pci, f)) or "")
            return float(m.group(1)) if m else None

        def width(f):
            try:
                return float(read_str(os.path.join(pci, f)) or "")
            except ValueError:
                return None
        gen = {2.5: 1, 5.0: 2, 8.0: 3, 16.0: 4, 32.0: 5, 64.0: 6}
        cs, ms, cw, mw = gts("current_link_speed"), gts("max_link_speed"), width("current_link_width"), width("max_link_width")
        hint = f"PCIe {pci.rsplit('/', 1)[-1]}"
        if cs and ms and cw and mw:
            hint += f"; the drive supports Gen{gen.get(ms, '?')} x{mw:.0f}"
            if cs < ms or cw < mw:
                hint += ". Running below that: slot/chipset lane limit, or ASPM/link power saving at idle"
        S("pcie.gen", "PCIe link generation", gen.get(cs) if cs else None, "", 0, hint)
        S("pcie.width", "PCIe link width", cw, "lanes", 0, hint)

        raw = self._health(d["id"])
        h = parse_nvme_smart(raw) if raw and len(raw) >= 232 else None
        src = "NVMe SMART/Health log (log page 02h), read every 60 s"
        get = (lambda k: h.get(k)) if h else (lambda k: None)
        if not h:
            src += ": not readable (daemon not root?)"
        cw_ = get("critical_warning")
        bits = [n for i, n in enumerate(CRIT_WARN_BITS) if cw_ and cw_ & (1 << i)]
        S("smart.crit", "Critical warnings", cw_, "", 0, src + (": " + ", ".join(bits) if bits else "; 0 = healthy"))
        used = get("percentage_used")
        S("smart.used", "Life used", used, "%", 0, src + "; the vendor's wear estimate (can exceed 100%)")
        S("smart.left", "Life remaining", None if used is None else max(0, 100 - used), "%", 0, src)
        S("smart.spare", "Available spare", get("available_spare"), "%", 0,
          src + (f"; warns below {get('available_spare_threshold')}%" if h else ""))
        tb = lambda k: None if get(k) is None else get(k) * 512000 / 1e12
        S("smart.written", "Total written", tb("data_units_written"), "TB", 2, src)
        S("smart.read", "Total read", tb("data_units_read"), "TB", 2, src)
        S("smart.poh", "Power-on hours", get("power_on_hours"), "h", 0, src)
        S("smart.cycles", "Power cycles", get("power_cycles"), "", 0, src)
        S("smart.unsafe", "Unsafe shutdowns", get("unsafe_shutdowns"), "", 0, src + "; power lost without a clean shutdown")
        S("smart.media_err", "Media errors", get("media_errors"), "", 0, src + "; unrecovered data integrity errors, should stay 0")
        S("smart.err_log", "Error log entries", get("num_err_log_entries"), "", 0,
          src + "; mostly harmless (e.g. unsupported commands from tools); media errors are the one to watch")
        S("smart.warn_time", "Time over warning temp", get("warning_temp_time"), "min", 0, src)
        S("smart.crit_time", "Time over critical temp", get("critical_comp_time"), "min", 0, src)
        thr = None
        if h:
            thr = (get("thm_temp1_total_time") + get("thm_temp2_total_time")) / 60.0
        S("smart.throttle_time", "Host thermal management time", thr, "min", 1,
          src + "; time in host-controlled thermal management 1 + 2. Stays 0 unless the OS set HCTM "
                "thresholds; the drive's own throttling shows as time over warning temp")

    def _sata(self, d: dict, S):
        j = self._health(d["id"])
        src = "smartctl -A (every 5 min, skipped while the drive sleeps)"
        if not j or "ata_smart_attributes" not in j:
            if j is None and not smartctl_path():
                src = "install smartctl (smartmontools) for SATA health"
            S("smart.health", "SMART overall", None, "", 0, src)
            return
        if not d["temps"]:                       # hard drive, or SSD without the drivetemp module
            S("t.smart", "Temperature", (j.get("temperature") or {}).get("current"), "°C", 0, src)
        ok = (j.get("smart_status") or {}).get("passed")
        S("smart.health", "SMART overall (1 = passed)", None if ok is None else (1 if ok else 0), "", 0, src)
        S("smart.poh", "Power-on hours", (j.get("power_on_time") or {}).get("hours"), "h", 0, src)
        seen = set()
        for a in (j.get("ata_smart_attributes") or {}).get("table", []):
            spec = ATA_ATTRS.get(a.get("id"))
            if not spec or spec[0] in seen:
                continue
            seen.add(spec[0])
            label = spec[0]
            if a.get("id") == 192 and d.get("rotational"):
                label = "Power-off retracts"           # counts normal power-offs too on hard drives
            v = a.get("value") if spec[2] else (a.get("raw") or {}).get("value")
            S(f"smart.a{a['id']}", label, v, spec[1], 0, f"{src}; attribute {a['id']} {a.get('name', '')}")
        for a in (j.get("ata_smart_attributes") or {}).get("table", []):
            if a.get("id") == 241 and "LBA" in a.get("name", ""):
                lbas = (a.get("raw") or {}).get("value")
                S("smart.written", "Total written", None if lbas is None else lbas * 512 / 1e12, "TB", 2,
                  f"{src}; attribute 241 (assumes 512-byte LBAs)")


BACKENDS = [StorageBackend]
