"""Value/label formatting shared by the daemon, the mock daemon, the GUI and the OSD.

Keeping this in one place guarantees that the GUI tree, the desktop overlay, the CSV log
and the in-game OSD (osd.txt -> MangoHud) all render a sensor identically.
"""
from __future__ import annotations

from typing import Optional

NONE_TEXT = "—"   # em dash, shown when a backend reports value=None


def format_number(value: Optional[float], decimals: int = 0) -> str:
    if value is None:
        return NONE_TEXT
    try:
        d = max(0, int(decimals))
        return f"{float(value):.{d}f}"
    except (TypeError, ValueError):
        return NONE_TEXT


def format_value(value: Optional[float], decimals: int = 0, unit: str = "") -> str:
    """'2850 MHz', '1.045 V', '67 °C', '—' (no unit for missing values)."""
    if value is None:
        return NONE_TEXT
    s = format_number(value, decimals)
    if not unit:
        return s
    if unit in ("%",):
        return s + unit
    return f"{s} {unit}"


def device_of(group: str) -> str:
    """Top level tree node of a Sensor/Control group path."""
    return (group or "?").split("/", 1)[0].strip() or "?"


def device_tag(group: str) -> str:
    """Short device tag for the OSD: 'GPU 0: NVIDIA GeForce RTX 5080/Clocks' -> 'GPU 0'."""
    dev = device_of(group)
    tag = dev.split(":", 1)[0].strip()
    return tag or dev


def osd_label(sensor: dict) -> str:
    """Single-line label used by the desktop overlay, osd.txt and MangoHud.

    Guaranteed free of ':' and '#' (osd.txt uses ': ' as the label/value separator and
    MangoHud's config parser treats '#' as a comment start)."""
    label = f"{device_tag(sensor.get('group', ''))} {sensor.get('label', '')}".strip()
    return label.replace(":", " ").replace("#", "No.").replace("\n", " ").strip()


def osd_line(sensor: dict) -> str:
    """One line of osd.txt (/run/meat3 and /tmp/meat3): '<label>: <value unit>'."""
    return f"{osd_label(sensor)}: {format_value(sensor.get('value'), sensor.get('decimals', 0), sensor.get('unit', ''))}"
