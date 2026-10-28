"""Shared GUI state: config paths, settings, theme, colour rules, sensor store, icons."""
from __future__ import annotations

import csv
import sys
import math
import os
import time
from collections import deque
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QPointF, QRectF, QSettings, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QIcon, QPainter, QPainterPath,
                           QPalette, QPen, QPixmap, QPolygonF)
from PySide6.QtWidgets import QApplication

ASSETS = Path(__file__).resolve().parent / "assets"


# ----------------------------------------------------------------------------- paths/settings

def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "meat3"


def settings() -> QSettings:
    # ~/.config/meat3/meat3-gui.conf (QSettings honours XDG_CONFIG_HOME on Linux)
    return QSettings(str(config_dir() / "meat3-gui.conf"), QSettings.IniFormat)


def setting_list(s: QSettings, key: str) -> list[str]:
    v = s.value(key, [])
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v else []
    return [str(x) for x in v]


def setting_bool(s: QSettings, key: str, default: bool) -> bool:
    v = s.value(key, default)
    if isinstance(v, str):
        return v.lower() in ("1", "true", "yes", "on")
    return bool(v)


# ----------------------------------------------------------------------------- theme

class Theme:
    def __init__(self, dark: bool):
        self.dark = dark
        if dark:
            self.window = QColor("#1c1e23")
            self.base = QColor("#16181c")
            self.alt = QColor("#1c1f24")
            self.text = QColor("#d9dde4")
            self.dim = QColor("#8a919e")
            self.button = QColor("#272a31")
            self.border = QColor("#30343c")
            self.accent = QColor("#e8674f")
            self.select = QColor("#4a2a24")
            self.device_bg = QColor("#23262d")
            self.card = QColor("#212429")
            self.amber = QColor("#f2b33d")
            self.red = QColor("#ff5d5d")
            self.green = QColor("#5fcf80")
            self.throttle_bg = QColor(255, 80, 80, 46)
            self.modified_bg = QColor(232, 103, 79, 30)
        else:
            self.window = QColor("#f3f4f6")
            self.base = QColor("#ffffff")
            self.alt = QColor("#f5f6f8")
            self.text = QColor("#1d2128")
            self.dim = QColor("#69707c")
            self.button = QColor("#e9ebef")
            self.border = QColor("#d3d7de")
            self.accent = QColor("#c9472f")
            self.select = QColor("#f6d5cc")
            self.device_bg = QColor("#e9ecf1")
            self.card = QColor("#ffffff")
            self.amber = QColor("#b26b00")
            self.red = QColor("#c62828")
            self.green = QColor("#23853f")
            self.throttle_bg = QColor(229, 57, 53, 38)
            self.modified_bg = QColor(201, 71, 47, 26)

    def palette(self) -> QPalette:
        p = QPalette()
        for grp in (QPalette.Active, QPalette.Inactive):
            p.setColor(grp, QPalette.Window, self.window)
            p.setColor(grp, QPalette.WindowText, self.text)
            p.setColor(grp, QPalette.Base, self.base)
            p.setColor(grp, QPalette.AlternateBase, self.alt)
            p.setColor(grp, QPalette.Text, self.text)
            p.setColor(grp, QPalette.Button, self.button)
            p.setColor(grp, QPalette.ButtonText, self.text)
            p.setColor(grp, QPalette.Highlight, self.select)
            p.setColor(grp, QPalette.HighlightedText, self.text)
            p.setColor(grp, QPalette.ToolTipBase, self.button)
            p.setColor(grp, QPalette.ToolTipText, self.text)
            p.setColor(grp, QPalette.PlaceholderText, self.dim)
            p.setColor(grp, QPalette.Link, self.accent)
            p.setColor(grp, QPalette.Mid, self.border)
            p.setColor(grp, QPalette.Dark, self.border.darker(130))
            p.setColor(grp, QPalette.Light, self.button.lighter(120))
            p.setColor(grp, QPalette.BrightText, self.red)
        p.setColor(QPalette.Disabled, QPalette.Text, self.dim)
        p.setColor(QPalette.Disabled, QPalette.WindowText, self.dim)
        p.setColor(QPalette.Disabled, QPalette.ButtonText, self.dim)
        p.setColor(QPalette.Disabled, QPalette.Base, self.window)
        p.setColor(QPalette.Disabled, QPalette.Button, self.window)
        p.setColor(QPalette.Disabled, QPalette.Highlight, self.border)
        return p

    def stylesheet(self) -> str:
        c = {k: v.name() for k, v in vars(self).items() if isinstance(v, QColor)}
        a = ASSETS.as_posix()
        return f"""
QToolTip {{ color: {c['text']}; background: {c['button']}; border: 1px solid {c['border']}; padding: 4px; }}
QTabWidget::pane {{ border: none; border-top: 1px solid {c['border']}; }}
QTabBar::tab {{ padding: 7px 18px; border: none; color: {c['dim']}; background: transparent; }}
QTabBar::tab:selected {{ color: {c['text']}; border-bottom: 2px solid {c['accent']}; }}
QTabBar::tab:hover {{ color: {c['text']}; }}
QToolBar {{ border: none; spacing: 4px; padding: 4px 6px; }}
QToolButton {{ padding: 4px 9px; border-radius: 4px; border: 1px solid transparent; }}
QToolButton:hover {{ background: {c['button']}; border-color: {c['border']}; }}
QToolButton:checked {{ background: {c['select']}; border-color: {c['accent']}; }}
QTreeView, QListWidget, QPlainTextEdit {{ border: 1px solid {c['border']}; border-radius: 4px; }}
QTreeView::item {{ padding: 1px 0px; }}
QHeaderView::section {{ background: {c['window']}; color: {c['dim']}; border: none;
    border-bottom: 1px solid {c['border']}; border-right: 1px solid {c['border']}; padding: 5px 8px; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{ padding: 3px 6px; border: 1px solid {c['border']};
    border-radius: 4px; background: {c['base']}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {c['accent']}; }}
QPushButton {{ padding: 4px 12px; border: 1px solid {c['border']}; border-radius: 4px; background: {c['button']}; }}
QPushButton:hover {{ border-color: {c['accent']}; }}
QPushButton:disabled {{ color: {c['dim']}; background: {c['window']}; }}
QPushButton#primary {{ background: {c['accent']}; color: white; border-color: {c['accent']}; }}
QPushButton#primary:disabled {{ background: {c['button']}; color: {c['dim']}; border-color: {c['border']}; }}
QGroupBox {{ border: 1px solid {c['border']}; border-radius: 6px; margin-top: 14px; padding: 10px 8px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {c['text']}; font-weight: bold; }}
QSlider::groove:horizontal {{ height: 4px; background: {c['border']}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {c['accent']}; border-radius: 2px; }}
QSlider::sub-page:horizontal:disabled {{ background: {c['border']}; }}
QSlider::handle:horizontal {{ width: 12px; margin: -5px 0; border-radius: 6px; background: {c['text']}; }}
QSlider::handle:horizontal:disabled {{ background: {c['dim']}; }}
QFrame#banner {{ background: {QColor(self.red.red(), self.red.green(), self.red.blue(), 40).name(QColor.HexArgb)};
    border: 1px solid {c['red']}; border-radius: 5px; }}
QAbstractSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 18px;
    border-left: 1px solid {c['border']}; border-top-right-radius: 4px; background: {c['button']}; }}
QAbstractSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; width: 18px;
    border-left: 1px solid {c['border']}; border-bottom-right-radius: 4px; background: {c['button']}; }}
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{ background: {c['select']}; }}
QAbstractSpinBox::up-arrow {{ image: url({a}/arrow-up.svg); width: 9px; height: 9px; }}
QAbstractSpinBox::down-arrow {{ image: url({a}/arrow-down.svg); width: 9px; height: 9px; }}
QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::down-arrow:disabled {{ image: none; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ image: url({a}/arrow-down.svg); width: 10px; height: 10px; }}
QComboBox QAbstractItemView {{ border: 1px solid {c['border']}; selection-background-color: {c['select']}; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid {c['dim']}; border-radius: 3px; background: {c['base']}; }}
QCheckBox::indicator:hover {{ border-color: {c['accent']}; }}
QCheckBox::indicator:checked {{ background: {c['accent']}; border-color: {c['accent']}; image: url({a}/check.svg); }}
QCheckBox::indicator:disabled {{ background: {c['window']}; border-color: {c['border']}; }}
QListWidget::item {{ padding: 3px 4px; }}
QFrame#card {{ border: 1px solid {c['border']}; border-radius: 6px; background: {c['card']}; }}
QLabel#cardTitle {{ font-weight: bold; }}
QLabel#bannerText {{ color: {c['text']}; }}
QLabel#dim {{ color: {c['dim']}; }}
QStatusBar {{ color: {c['dim']}; }}
QScrollArea {{ border: none; }}
"""


_theme = Theme(True)


def theme() -> Theme:
    return _theme


def apply_theme(dark: bool) -> Theme:
    global _theme
    _theme = Theme(dark)
    app = QApplication.instance()
    app.setStyle("Fusion")
    app.setPalette(_theme.palette())
    app.setStyleSheet(_theme.stylesheet())
    return _theme


def mono_font(size_delta: float = 0) -> QFont:
    f = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    base = QApplication.font().pointSizeF()
    if base > 0:
        f.setPointSizeF(base + size_delta)
    return f


# ----------------------------------------------------------------------------- colour rules

NORMAL, WARN, CRIT, THROTTLE = 0, 1, 2, 3

# (keyword in label/id, amber threshold, red threshold) - first match wins
TEMP_RULES = [
    ("memory junction", 90, 100), ("memj", 90, 100),
    ("hot spot", 90, 100), ("hotspot", 90, 100), ("junction", 90, 100),
    ("vrm", 90, 105),
    ("tctl", 85, 95), ("tdie", 85, 95), ("ccd", 85, 95), ("cpu.core", 85, 95), ("l3", 85, 95),
]
TEMP_DEFAULT = (80, 90)


def is_pin_current(s: dict) -> bool:
    return s.get("unit") == "A" and (".pin" in s.get("id", "") or s.get("label", "").lower().startswith("pin "))


def severity(s: dict, value: Optional[float]) -> int:
    if value is None:
        return NORMAL
    sid = s.get("id", "").lower()
    label = s.get("label", "").lower()
    if ("throttle" in sid or "throttle" in label or "throttle" in s.get("group", "").lower()) \
            and value != 0 and "idle" not in label:
        return THROTTLE
    unit = s.get("unit", "")
    if is_pin_current(s):
        return CRIT if value > 9 else WARN if value > 8 else NORMAL
    if unit == "°C" and not any(w in label for w in ("limit", "threshold", "target", "delta", "max operating")):
        lo, hi = TEMP_DEFAULT
        key = f"{label} {sid}"
        for kw, a, r in TEMP_RULES:
            if kw in key:
                lo, hi = a, r
                break
        return CRIT if value >= hi else WARN if value >= lo else NORMAL
    return NORMAL


def severity_color(level: int) -> Optional[QColor]:
    t = theme()
    return {WARN: t.amber, CRIT: t.red, THROTTLE: t.red}.get(level)


# ----------------------------------------------------------------------------- store

class SensorStore(QObject):
    """Latest snapshot + client-side history ring buffers + CSV logging + favourites."""

    updated = Signal()
    favourites_changed = Signal()
    logging_changed = Signal(bool, str)

    HISTORY_SECONDS = 30 * 60

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sensors: dict[str, dict] = {}       # insertion order = first-seen order
        self.controls: list[dict] = []
        self.t = 0.0
        self.history: dict[str, deque] = {}
        self.polls = 0
        self.log_file = None
        self.log_writer = None
        self.log_ids: list[str] = []
        self.log_path = ""
        st = settings()
        self.favourites: set[str] = set(setting_list(st, "favourites"))

    def ingest(self, snap: dict) -> None:
        t = float(snap.get("t") or time.time())
        self.t = t
        seen = set()
        for s in snap.get("sensors", []):
            sid = s.get("id")
            if not sid:
                continue
            seen.add(sid)
            self.sensors[sid] = s
            v = s.get("value")
            h = self.history.get(sid)
            if h is None:
                h = self.history[sid] = deque(maxlen=self.HISTORY_SECONDS * 2)
            h.append((t, v if isinstance(v, (int, float)) and math.isfinite(v) else None))
        for sid, s in self.sensors.items():   # sensors that vanished: keep the row, show '—'
            if sid not in seen and s.get("value") is not None:
                self.sensors[sid] = dict(s, value=None)
        self.controls = snap.get("controls", []) or []
        self.polls += 1
        self._log_row(t)
        self.updated.emit()

    def mark_stale(self) -> None:
        """Daemon gone: keep rows and min/max/avg but show current values as '—'."""
        if not self.sensors:
            return
        for sid, s in self.sensors.items():
            if s.get("value") is not None:
                self.sensors[sid] = dict(s, value=None)
        self.updated.emit()

    # favourites ------------------------------------------------------
    def set_favourite(self, sid: str, fav: bool) -> None:
        (self.favourites.add if fav else self.favourites.discard)(sid)
        settings().setValue("favourites", sorted(self.favourites))
        self.favourites_changed.emit()

    # CSV logging -----------------------------------------------------
    def start_log(self, path: str) -> None:
        self.stop_log()
        self.log_file = open(path, "w", newline="", encoding="utf-8")
        self.log_writer = csv.writer(self.log_file)
        self.log_ids = list(self.sensors.keys())
        self.log_writer.writerow(["time_unix", "time"] + self.log_ids)
        units = [self.sensors[i].get("unit", "") for i in self.log_ids]
        labels = [f"{self.sensors[i].get('group', '')}/{self.sensors[i].get('label', '')}" for i in self.log_ids]
        # second and third header rows (comment-prefixed so tools can skip them)
        self.log_writer.writerow(["#label", ""] + labels)
        self.log_writer.writerow(["#unit", ""] + units)
        self.log_file.flush()
        self.log_path = path
        self.logging_changed.emit(True, path)

    def stop_log(self) -> None:
        if self.log_file is not None:
            try:
                self.log_file.close()
            except OSError:
                pass
            self.log_file = None
            self.log_writer = None
            self.logging_changed.emit(False, self.log_path)

    @property
    def logging(self) -> bool:
        return self.log_file is not None

    def _log_row(self, t: float) -> None:
        if self.log_writer is None:
            return
        row = [f"{t:.3f}", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))]
        for sid in self.log_ids:
            v = self.sensors.get(sid, {}).get("value")
            row.append("" if v is None else repr(round(float(v), 6)))
        try:
            self.log_writer.writerow(row)
            self.log_file.flush()
        except OSError:
            self.stop_log()


# ----------------------------------------------------------------------------- icons

def app_icon() -> QIcon:
    svg = ASSETS / "meat3.svg"
    icon = QIcon(str(svg)) if svg.exists() else QIcon()
    if icon.isNull() or icon.pixmap(32, 32).isNull():
        icon = QIcon()
        for size in (16, 22, 32, 48, 64, 128, 256):
            icon.addPixmap(draw_cube_icon(size))
    return icon


def draw_cube_icon(size: int, osd_on: Optional[bool] = None) -> QPixmap:
    """Isometric cube with a superscript ³ - fallback if the SVG plugin is missing."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 64.0
    top = QPolygonF([QPointF(30 * s, 8 * s), QPointF(54 * s, 20 * s), QPointF(30 * s, 32 * s), QPointF(6 * s, 20 * s)])
    left = QPolygonF([QPointF(6 * s, 20 * s), QPointF(30 * s, 32 * s), QPointF(30 * s, 60 * s), QPointF(6 * s, 48 * s)])
    right = QPolygonF([QPointF(30 * s, 32 * s), QPointF(54 * s, 20 * s), QPointF(54 * s, 48 * s), QPointF(30 * s, 60 * s)])
    p.setPen(Qt.NoPen)
    for poly, col in ((top, "#f28b72"), (left, "#d9573f"), (right, "#a83a29")):
        p.setBrush(QColor(col))
        p.drawPolygon(poly)
    p.setPen(QPen(QColor("#ffffff"), max(1.0, 1.5 * s)))
    f = QFont()
    f.setBold(True)
    f.setPixelSize(max(6, int(20 * s)))
    p.setFont(f)
    p.drawText(QRectF(40 * s, 0, 24 * s, 24 * s), Qt.AlignCenter, "³")
    if osd_on is not None:
        p.setBrush(QColor("#5fcf80") if osd_on else QColor("#8a919e"))
        p.setPen(QPen(QColor("#000000"), max(1.0, s)))
        p.drawEllipse(QRectF(44 * s, 44 * s, 18 * s, 18 * s))
    p.end()
    return pm


def _star(p: QPainter, cx: float, cy: float, r: float) -> None:
    pts = []
    for i in range(10):
        a = -math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * 0.45
        pts.append(QPointF(cx + rr * math.cos(a), cy + rr * math.sin(a)))
    p.drawPolygon(QPolygonF(pts))


_marker_cache: dict = {}


def sensor_marker(fav: bool, osd: bool, h: int = 14) -> QIcon:
    """Two-slot decoration (favourite star, OSD tag) so labels stay aligned."""
    key = (fav, osd, h, theme().dark)
    if key in _marker_cache:
        return _marker_cache[key]
    w = h * 2 + 2
    pm = QPixmap(w * 2, h * 2)
    pm.setDevicePixelRatio(2.0)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    if fav:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#f2b33d"))
        _star(p, h / 2, h / 2 + 0.5, h * 0.45)
    if osd:
        r = QRectF(h + 2.5, 2.5, h - 4, h - 5)
        p.setPen(QPen(theme().accent, 1.4))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, 2, 2)
        p.setPen(QPen(theme().accent, 1.2))
        p.drawLine(QPointF(r.left() + 2.5, r.center().y()), QPointF(r.right() - 2.5, r.center().y()))
    p.end()
    icon = QIcon(pm)
    _marker_cache[key] = icon
    return icon


def device_badge(name: str, kind: str = "", h: int = 14) -> QIcon:
    n = name.lower()
    if "nvidia" in n or "geforce" in n or "rtx" in n:
        col = "#76b900"
    elif "radeon" in n or ("amd" in n and kind == "gpu"):
        col = "#e8453c"
    elif kind == "cpu" or "ryzen" in n or "cpu" in n:
        col = "#f08a24"
    else:
        col = "#4f8fe8"
    key = ("badge", col, h)
    if key in _marker_cache:
        return _marker_cache[key]
    pm = QPixmap(h * 2, h * 2)
    pm.setDevicePixelRatio(2.0)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(col))
    p.drawRoundedRect(QRectF(1.5, 1.5, h - 3, h - 3), 3, 3)
    p.end()
    icon = QIcon(pm)
    _marker_cache[key] = icon
    return icon


def outlined_path(text: str, font: QFont, x: float, y: float) -> QPainterPath:
    path = QPainterPath()
    path.addText(QPointF(x, y), font, text)
    return path


# ----------------------------------------------------------------------------- PySide6 workaround

_none_guard = None


def install_none_refcount_guard() -> bool:
    """Work around PySide6 builds (seen with 6.12.0 on CPython 3.10) whose void-returning
    methods DECREF None without a matching INCREF. On CPython >= 3.12 None is immortal and
    this is harmless; on older interpreters None's refcount reaches 0 after a few minutes of
    UI updates and the process aborts with 'none_dealloc: deallocating None'.
    If the leak is detected we pad None's refcount and top it up from a timer."""
    global _none_guard
    if sys.version_info >= (3, 12) or _none_guard is not None:
        return False
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QLabel
    probe = QLabel()
    before = sys.getrefcount(None)
    for _ in range(200):
        probe.setEnabled(True)
    leak = before - sys.getrefcount(None)
    probe.deleteLater()
    if leak < 100:
        return False
    import ctypes
    incref = ctypes.pythonapi.Py_IncRef
    incref.argtypes = [ctypes.py_object]
    incref.restype = None
    pad = [None] * 4_000_000            # headroom between timer ticks
    baseline = sys.getrefcount(None)

    def top_up():
        deficit = baseline - sys.getrefcount(None)
        for _ in range(deficit):
            incref(None)

    timer = QTimer()
    timer.timeout.connect(top_up)
    timer.start(250)
    _none_guard = (pad, timer, top_up)
    print(f"meat3: WARNING: this PySide6 build is broken on Python {sys.version_info[0]}.{sys.version_info[1]} "
          f"(None refcount leak, {leak}/200 calls). A guard is installed, but PySide6 6.12.0 on 3.10 also "
          "crashes in other ways - use Python >= 3.12, or `pip install 'pyside6==6.9.*'`.", file=sys.stderr)
    return True
