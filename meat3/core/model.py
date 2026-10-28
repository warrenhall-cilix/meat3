"""meat³ shared data model and backend contract.

Every hardware backend (NVIDIA GPU, AMD GPU, AMD CPU, board/hwmon, ...) implements `Backend`.
The root daemon (meat3d) loads all backends whose `detect()` returns instances, polls
`sensors()` about once per second, and serves the results to the GUI/OSD over a Unix socket.
`controls()`/`apply()` expose overclocking knobs; the daemon only calls `apply()` for a
request from an authorised client, and backends must validate every value themselves.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class Sensor:
    id: str                 # stable, unique across the system, e.g. "nv0.clk.gpc"
    group: str              # tree path, '/'-separated: "GPU 0: RTX 5080/Clocks"
    label: str              # row label, e.g. "GPC (core) clock"
    value: Optional[float]  # None = not available this poll
    unit: str = ""          # "MHz", "mV", "V", "W", "A", "°C", "%", "GB/s", "RPM", ...
    decimals: int = 0       # display precision
    hint: str = ""          # tooltip: source / caveats (e.g. "x1.5 shunt corrected")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Control:
    id: str                 # unique, e.g. "nv0.oc.core_offset"
    group: str              # "GPU 0: RTX 5080"
    label: str
    kind: str               # "int" | "float" | "bool" | "choice" | "action"
    value: Any = None       # current value as read back from hardware (None if unknown)
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    unit: str = ""
    choices: list = field(default_factory=list)   # for kind == "choice"
    default: Any = None     # stock value, used by "reset to stock"
    danger: int = 0         # 0 safe, 1 caution, 2 dangerous (GUI shows warning colours)
    hint: str = ""
    available: bool = True  # False => shown greyed out with `hint` explaining why

    def to_dict(self) -> dict:
        return asdict(self)


class Backend(ABC):
    kind: str = "misc"      # "gpu" | "cpu" | "board" | "misc"
    name: str = "?"         # human readable device name, used as top-level tree node

    @classmethod
    @abstractmethod
    def detect(cls) -> list["Backend"]:
        """Return one instance per supported device present. Must never raise and must
        never write to hardware. Missing permissions/modules => return [] or degrade."""

    @abstractmethod
    def sensors(self) -> list[Sensor]:
        """Current readings. Called ~1 Hz from the daemon thread. Must be read-only and
        should finish in < 200 ms. Unavailable values => value=None (keep the row)."""

    def controls(self) -> list[Control]:
        """Overclocking controls with current values. Read-only call."""
        return []

    def apply(self, control_id: str, value: Any) -> tuple[bool, str]:
        """Apply one control. MUST validate type/range itself and refuse anything outside
        the advertised min/max. Returns (ok, message)."""
        return False, "not supported"

    def close(self) -> None:
        pass
