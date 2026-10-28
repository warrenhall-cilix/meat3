"""Minimal NVIDIA Resource Manager client (open kernel modules, Linux).

Allocates root client -> device -> subdevice through /dev/nvidiactl and issues
NV_ESC_RM_CONTROL. Only controls with layouts known from the open SDK headers or
decoded from NVIDIA's own callers (NVAPI/NVML) are used by meat3 - see the
helpers below. Never call controls with guessed inputs (it crashed the PMU once).
"""
from __future__ import annotations

import ctypes
import fcntl
import os
import struct

NV_ESC_RM_FREE, NV_ESC_RM_CONTROL, NV_ESC_RM_ALLOC = 0x29, 0x2A, 0x2B
NV_ESC_REGISTER_FD = 200 + 1
NV01_ROOT_CLIENT, NV01_DEVICE_0, NV20_SUBDEVICE_0 = 0x41, 0x80, 0x2080

# Known controls (subdevice class 0x2080)
VOLT_RAILS_STATUS = 0x2080b202       # 3360 B, non-privileged (patched module v2+)
VOLT_RAILS_CTRL_GET = 0x2080b213     # 1036 B
VOLT_RAILS_CTRL_SET = 0x2080f214     # 1036 B, privileged
CLK_DOMAINS_INFO = 0x20809019        # 12336 B
CLK_OFFSETS_GET = 0x2080901b         # 2108 B
CLK_OFFSETS_SET = 0x2080d01c         # 2108 B, privileged
CLK_READ = 0x20809062                # 648 B
THERMAL_EXECUTE_V2 = 0x20800513      # 1432 B (stock)
GPU_EXEC_REG_OPS = 0x20800122        # 48 B + regOps array (stock, documented)
# Blackwell GB20x thermal registers (read-only status); nothing else may be read through read_regs().
BW_THERM_REGS = {0xAD0AA0: "hotspot", 0xAD0A90: "die_a0", 0xAD0A94: "die_a1", 0xAD0A98: "die_a2",
                 0xAD0A9C: "die_a3", 0xAD0AA4: "die_a4", 0xAD0AE0: "bankb_max", 0xAD0400: "edge",
                 0xAD0718: "slowdown"}
REG_WHITELIST = set(BW_THERM_REGS)
CLK_RATIO_INFO = 0x20809081          # 5400 B (patched module v3)
CLK_RATIO_GET = 0x20809083           # 3096 B
CLK_RATIO_SET = 0x2080d084           # 3096 B, privileged
THERM_CHANNEL_INFO = 0x2080853a      # 15352 B (served; NVML uses it)
THERM_CHANNEL_STATUS = 0x2080853b    # 14320 B

DOMAIN_NAMES = {0x1: "GPC (core)", 0x2: "XBAR", 0x4: "SYS", 0x8: "HUB", 0x10: "Memory (MCLK)",
                0x40: "UTILS", 0x80000: "PWR", 0x100000: "NVD (video)", 0x200000: "DISP"}


def _iowr(nr: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (ord("F") << 8) | nr


class RMError(OSError):
    pass


class RMClient:
    def __init__(self, gpu: int = 0):
        self.ctl = os.open("/dev/nvidiactl", os.O_RDWR)
        try:
            self.hClient = self._alloc(0, 0, NV01_ROOT_CLIENT, None)
            self.dev = os.open(f"/dev/nvidia{gpu}", os.O_RDWR)
            fcntl.ioctl(self.dev, _iowr(NV_ESC_REGISTER_FD, 4), struct.pack("i", self.ctl))
            p = bytearray(56)
            struct.pack_into("<I", p, 0, gpu)
            self.hDevice = self._alloc(self.hClient, self.hClient, NV01_DEVICE_0, p, 0xCAF00001)
            self.hSub = self._alloc(self.hClient, self.hDevice, NV20_SUBDEVICE_0, bytearray(4), 0xCAF00002)
        except Exception:
            os.close(self.ctl)
            raise

    def _alloc(self, hRoot, hParent, hClass, params, hNew=0):
        buf = ctypes.create_string_buffer(bytes(params)) if params is not None else None
        a = bytearray(struct.pack("<IIIIQII", hRoot, hParent, hNew, hClass,
                                  ctypes.addressof(buf) if buf else 0, len(params) if params else 0, 0))
        fcntl.ioctl(self.ctl, _iowr(NV_ESC_RM_ALLOC, 32), a)
        st = struct.unpack_from("<I", a, 28)[0]
        if st:
            raise RMError(f"RM_ALLOC class {hClass:#x} status {st:#x}")
        return struct.unpack_from("<I", a, 8)[0]

    def control(self, cmd: int, params: bytes) -> tuple[int, bytearray]:
        buf = ctypes.create_string_buffer(bytes(params), len(params))
        a = bytearray(struct.pack("<IIIIQII", self.hClient, self.hSub, cmd, 0,
                                  ctypes.addressof(buf), len(params), 0))
        fcntl.ioctl(self.ctl, _iowr(NV_ESC_RM_CONTROL, 32), a)
        return struct.unpack_from("<I", a, 28)[0], bytearray(buf.raw)

    def close(self):
        a = bytearray(struct.pack("<IIII", self.hClient, self.hClient, self.hClient, 0))
        try:
            fcntl.ioctl(self.ctl, _iowr(NV_ESC_RM_FREE, 16), a)
        except OSError:
            pass
        for fd in (getattr(self, "dev", None), self.ctl):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    # ---- typed helpers (layouts from RE notes: ~/nv-volt-re, ~/nv-work/xbar-re) ----

    def volt_rails_status(self) -> dict | None:
        """NVVDD (rail 0) / MSVDD (rail 1): live voltage and REL/ALT/OV limits, in mV."""
        p = bytearray(3360); p[0] = 0xFF; p[4] = 3
        st, b = self.control(VOLT_RAILS_STATUS, p)
        if st:
            return None
        u = lambda o: struct.unpack_from("<I", b, o)[0] / 1000.0
        return {"nvvdd": u(0x24), "nvvdd_rel": u(0x2C), "nvvdd_alt": u(0x30), "nvvdd_ov": u(0x34),
                "nvvdd_vmin": u(0x3C), "msvdd": u(0x8C), "msvdd_rel": u(0x94), "msvdd_alt": u(0x98),
                "msvdd_ov": u(0x9C), "msvdd_vmin": u(0xA4)}

    def vrel_offsets(self) -> tuple[float, float] | None:
        """(NVVDD, MSVDD) Vrel offsets in mV (rail v[0], uV in firmware)."""
        p = bytearray(1036); p[0] = 0xFF; struct.pack_into("<I", p, 4, 3)
        st, b = self.control(VOLT_RAILS_CTRL_GET, p)
        if st:
            return None
        return (struct.unpack_from("<i", b, 0x0C + 4)[0] / 1000.0,
                struct.unpack_from("<i", b, 0x2C + 4)[0] / 1000.0)

    def set_vrel(self, rail: int, mv: float) -> None:
        """Read-modify-write rail `rail` v[0] only. Caller validates the range."""
        p = bytearray(1036); p[0] = 0xFF; struct.pack_into("<I", p, 4, 3)
        st, b = self.control(VOLT_RAILS_CTRL_GET, p)
        if st:
            raise RMError(f"rail get status {st:#x}")
        e = 0x0C + 0x20 * rail
        if struct.unpack_from("<I", b, e)[0] != 5 or any(struct.unpack_from("<5i", b, e + 8)):
            raise RMError("unexpected rail entry layout; refusing")
        w = bytearray(b)
        struct.pack_into("<I", w, 4, 1 << rail)
        struct.pack_into("<i", w, e + 4, int(round(mv * 1000)))
        st, _ = self.control(VOLT_RAILS_CTRL_SET, w)
        if st:
            raise RMError(f"rail set status {st:#x}")

    def clock_domains(self) -> list[dict]:
        """Per-domain objects: index, domain bit, offset range (MHz)."""
        p = bytearray(12336); p[0] = 0xFF; struct.pack_into("<I", p, 4, 0x1FF)
        st, b = self.control(CLK_DOMAINS_INFO, p)
        if st:
            return []
        out = []
        for i in range(9):
            e = 0x30 + 0x180 * i
            dom = struct.unpack_from("<I", b, e + 4)[0]
            lo, hi = struct.unpack_from("<hh", b, e + 0x26)
            out.append({"index": i, "domain": dom, "min": lo, "max": hi})
        return out

    def clock_read(self, domain: int) -> tuple[float, float] | None:
        """(actual, target) MHz for one clock domain bit."""
        p = bytearray(648)
        struct.pack_into("<I", p, 4, 1)
        struct.pack_into("<II", p, 8, 0, domain)
        st, b = self.control(CLK_READ, p)
        if st:
            return None
        a, t = struct.unpack_from("<II", b, 0x10)
        return a / 1000.0, t / 1000.0

    def domain_offsets(self) -> dict[int, float] | None:
        """{object index: frequency offset MHz} from the per-domain offset group."""
        p = bytearray(2108); p[0] = 0xFF; struct.pack_into("<I", p, 4, 0x1FF)
        st, b = self.control(CLK_OFFSETS_GET, p)
        if st:
            return None
        return {i: struct.unpack_from("<i", b, 0x3C + 0x40 * i + 0x0C)[0] / 1000.0 for i in range(9)}

    def set_domain_offset(self, index: int, mhz: int) -> None:
        p = bytearray(2108); p[0] = 0xFF; struct.pack_into("<I", p, 4, 0x1FF)
        st, b = self.control(CLK_OFFSETS_GET, p)
        if st:
            raise RMError(f"offset get status {st:#x}")
        e = 0x3C + 0x40 * index
        if b[e] != 0x10 or b[e + 0x08] != 0 or any(struct.unpack_from("<8i", b, e + 0x10)):
            raise RMError("unexpected domain entry layout; refusing")
        w = bytearray(b)
        struct.pack_into("<I", w, 4, 1 << index)
        struct.pack_into("<i", w, e + 0x0C, int(mhz) * 1000)
        st, _ = self.control(CLK_OFFSETS_SET, w)
        if st:
            raise RMError(f"offset set status {st:#x}")

    def clock_ratios(self) -> list[dict]:
        """Clock-propagation ratio relationships (RE: ~/nv-work/ratio-re; patched module v3).
        Info: zeroed 5400 B (as NVAPI); get: 3096 B with the info mask. Ratio = u32 16.16."""
        st, info = self.control(CLK_RATIO_INFO, bytearray(5400))
        if st:
            return []
        maskb = bytes(info[4:36])
        mask = int.from_bytes(maskb, "little")
        g = bytearray(3096); g[4:36] = maskb
        st, cur = self.control(CLK_RATIO_GET, g)
        if st:
            return []
        out = []
        for i in range(255):
            if not mask >> i & 1:
                continue
            e = 0x128 + 0x14 * i
            if info[e] != 3:                       # 3 = RATIO type
                continue
            ce = 0x24 + 0x0C * i
            out.append({"index": i, "src": info[e + 2], "dst": info[e + 3], "bidir": bool(info[e + 4]),
                        "default": struct.unpack_from("<I", info, e + 0x0C)[0] / 65536.0,
                        "current": struct.unpack_from("<I", cur, ce + 8)[0] / 65536.0 if cur[ce] == 3 else None})
        return out

    def set_clock_ratio(self, index: int, ratio: float) -> None:
        """Write one RATIO entry (NVAPI builds the SET buffer from scratch). Caller validates range."""
        if not 0.25 <= ratio <= 2.0:
            raise RMError("ratio outside sane bounds")
        w = bytearray(3096)
        w[4 + index // 8] |= 1 << (index % 8)
        ce = 0x24 + 0x0C * index
        w[ce] = 3
        struct.pack_into("<I", w, ce + 8, int(round(ratio * 65536)))
        st, _ = self.control(CLK_RATIO_SET, w)
        if st:
            raise RMError(f"ratio set status {st:#x}")

    def read_regs(self, offsets: list[int]) -> dict[int, int | None]:
        """Read whitelisted BAR0 registers through RM GPU_EXEC_REG_OPS (documented in ctrl2080gpu.h;
        same path LACT uses). Non-transactional: each op gets its own status. Needs root
        (osIsAdministrator) for most offsets. Never writes."""
        bad = [o for o in offsets if o not in REG_WHITELIST]
        if bad:
            raise RMError(f"offsets not whitelisted: {[hex(o) for o in bad]}")
        ops = (ctypes.c_uint8 * (32 * len(offsets)))()
        for i, off in enumerate(offsets):            # regOp READ_32=0, regType GLOBAL=0
            struct.pack_into("<BBBBIII", ops, 32 * i, 0, 0, 0, 0, 0, 0, off)
        p = bytearray(48)
        struct.pack_into("<IIIIIIQ", p, 0, 0, 0, 1, 0, 0, len(offsets), ctypes.addressof(ops))
        st, _ = self.control(GPU_EXEC_REG_OPS, p)
        if st:
            return {o: None for o in offsets}
        out = {}
        for i, off in enumerate(offsets):
            ok = ops[32 * i + 2] == 0
            v = struct.unpack_from("<I", ops, 32 * i + 20)[0]
            out[off] = v if ok and v != 0xFFFFFFFF and (v >> 20) != 0xBAD else None
        return out

    def blackwell_temps(self) -> dict | None:
        """GB20x THERM block (0x00AD0000) - hotspot aggregate, die sensors, edge, slowdown limit.
        Offsets/format: LACT #1112/#1122, igor'sLAB; format confirmed in GSP-RM (Q9.5 in [16:3], valid bit 30)."""
        regs = self.read_regs(list(BW_THERM_REGS))
        if all(v is None for v in regs.values()):
            return None

        def q95(v):
            if v is None or not v >> 30 & 1:
                return None
            q = (v >> 3) & 0x3FFF
            return (q - 0x4000 if q & 0x2000 else q) / 32.0

        out = {name: q95(regs[off]) for off, name in BW_THERM_REGS.items() if name.startswith(("hot", "die", "bankb"))}
        e = regs.get(0xAD0400)
        out["edge"] = float(e & 0xFF) if e is not None else None
        s = regs.get(0xAD0718)
        out["slowdown"] = (s & 0x3FFF) / 32.0 if s is not None else None
        return out

    def thermal_channels(self) -> list[dict]:
        """Thermal channels (layout copied from NVML/NVAPI callers, see ~/nv-work/hotspot-re).
        Returns [{'index','type','enabled','temp'}]; type 0 = GPU, 1 = GPU max (hotspot), 3 = memory.
        temp is None when the firmware reports the 0xFF00 'invalid' marker."""
        st, info = self.control(THERM_CHANNEL_INFO, bytearray(15352))
        if st:
            return []
        maskb = bytes(info[4:36])
        mask = int.from_bytes(maskb, "little")
        s = bytearray(14320)
        s[4:36] = maskb
        st, r = self.control(THERM_CHANNEL_STATUS, s)
        if st:
            return []
        out = []
        for i in range(256):
            if not mask >> i & 1:
                continue
            e = 0x34 + i * 0x3C
            raw = struct.unpack_from("<i", r, 0x2C + i * 0x38)[0]
            out.append({"index": i, "type": info[e + 1], "enabled": bool(info[e + 0x21] & 0x40),
                        "temp": None if raw in (0xFF00, -1) or raw / 256 >= 254 else raw / 256.0})
        return out

    def thermal_sensors(self) -> list[int]:
        """Readings (degC) of every sensor the documented thermal-system interface exposes."""
        ISZ, N = 44, 32

        def run(instrs):
            p = bytearray(24 + N * ISZ)
            struct.pack_into("<IIIIII", p, 0, 1, 0, ISZ, 0, 0, len(instrs))
            for i, (op, a) in enumerate(instrs):
                struct.pack_into("<IIII", p, 24 + i * ISZ, 0, 0, op, a)
            st, r = self.control(THERMAL_EXECUTE_V2, p)
            return st, [struct.unpack_from("<IIIIi", r, 24 + i * ISZ) for i in range(len(instrs))]

        st, o = run([(0x500, 0)])
        if st or not o[0][1]:
            return []
        n = min(o[0][3], N)
        st, o = run([(0x1500, s) for s in range(n)])
        return [] if st else [x[4] for x in o if x[1]]
