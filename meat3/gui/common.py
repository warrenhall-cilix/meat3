"""Shared GUI state: config paths, settings, theme, colour rules, sensor store, icons."""
from __future__ import annotations

import configparser
import csv
import sys
import math
import os
import time
from collections import deque
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QFileSystemWatcher, QObject, QPointF, QRectF, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QIcon, QPainter, QPainterPath,
                           QPalette, QPen, QPixmap, QPolygonF)
from PySide6.QtWidgets import QApplication, QStyleFactory

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

THEMES = {"dark": "Dark", "light": "Light", "liquid": "Liquid Ass", "system": "System (follow KDE)"}


def theme_name(v) -> str:
    """Accepts a theme name or the legacy dark bool."""
    if isinstance(v, bool):
        return "dark" if v else "light"
    v = str(v or "dark").lower().replace(" ", "")
    if v in ("liquidass", "liquidglass", "glass"):
        return "liquid"
    if v in ("kde", "auto", "native", "plasma"):
        return "system"
    return v if v in THEMES else "dark"


# ----------------------------------------------------------------------------- KDE / system colours
def kdeglobals_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "kdeglobals"


def read_kde_scheme(path: Optional[Path] = None) -> Optional[dict]:
    """Plasma's colour scheme and widget style from kdeglobals, or None outside KDE.
    Plasma writes the active scheme (with the accent colour already applied) into this file."""
    path = path or kdeglobals_path()
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    try:
        if not cp.read(path, encoding="utf-8"):
            return None
    except (configparser.Error, OSError, UnicodeDecodeError):
        return None

    def col(sec: str, key: str) -> Optional[QColor]:
        try:
            parts = [int(x) for x in cp.get(sec, key).split(",")]
        except (configparser.Error, ValueError):
            return None
        if len(parts) not in (3, 4) or not all(0 <= x <= 255 for x in parts):
            return None
        return QColor(*parts)

    if col("Colors:Window", "BackgroundNormal") is None:
        return None
    out = {"style": cp.get("KDE", "widgetStyle", fallback="") or ""}
    for name, sec, key in (
            ("window", "Colors:Window", "BackgroundNormal"), ("window_text", "Colors:Window", "ForegroundNormal"),
            ("base", "Colors:View", "BackgroundNormal"), ("alt", "Colors:View", "BackgroundAlternate"),
            ("text", "Colors:View", "ForegroundNormal"), ("dim", "Colors:View", "ForegroundInactive"),
            ("button", "Colors:Button", "BackgroundNormal"), ("button_text", "Colors:Button", "ForegroundNormal"),
            ("selection", "Colors:Selection", "BackgroundNormal"),
            ("selection_text", "Colors:Selection", "ForegroundNormal"),
            ("tooltip", "Colors:Tooltip", "BackgroundNormal"), ("tooltip_text", "Colors:Tooltip", "ForegroundNormal"),
            ("link", "Colors:View", "ForegroundLink"), ("negative", "Colors:View", "ForegroundNegative"),
            ("neutral", "Colors:View", "ForegroundNeutral"), ("positive", "Colors:View", "ForegroundPositive"),
            ("accent", "General", "AccentColor")):
        out[name] = col(sec, key)
    return out


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    """a blended towards b by t (0..1)."""
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t))


# the platform's own style and palette, captured before the first theme replaces them
_NATIVE: dict = {}


def _capture_native(app) -> None:
    if not _NATIVE:
        _NATIVE["style"] = app.style().name()
        _NATIVE["palette"] = QPalette(app.palette())


def _rgba(c: QColor) -> str:
    return f"rgba({c.red()},{c.green()},{c.blue()},{c.alpha()})"


class Theme:
    def __init__(self, name="dark"):
        self.name = theme_name(name)
        self.glass = self.name == "liquid"
        self.native = self.name == "system"
        dark = self.name != "light"
        self.dark = dark
        self.style = "Fusion"
        if self.native:
            self._from_system()
            return
        if self.glass:
            # "Liquid Ass": translucent glass over a painted liquid backdrop (paint_backdrop)
            self.window = QColor("#16131f")          # opaque: popups / fallbacks
            self.base = QColor(255, 255, 255, 18)
            self.alt = QColor(255, 255, 255, 9)
            self.text = QColor("#f5f3fb")
            self.dim = QColor("#b9b4cc")
            self.button = QColor(255, 255, 255, 30)
            self.border = QColor(255, 255, 255, 46)
            self.accent = QColor("#ff8a65")
            self.select = QColor(255, 255, 255, 52)
            self.device_bg = QColor(255, 255, 255, 26)
            self.card = QColor(255, 255, 255, 20)
            self.amber = QColor("#ffc857")
            self.red = QColor("#ff6b6b")
            self.green = QColor("#6ee7a0")
            self.throttle_bg = QColor(255, 90, 90, 60)
            self.modified_bg = QColor(255, 138, 101, 40)
        elif dark:
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

    def _from_system(self):
        """Colours from the KDE colour scheme (kdeglobals), else from the platform palette; the
        widget style is the desktop's own (Breeze on Plasma) so meat3 looks like every other app."""
        k = read_kde_scheme() or {}
        nat = _NATIVE.get("palette") or QApplication.palette()
        role = lambda r: QColor(nat.color(QPalette.Active, r))
        pick = lambda key, r: k.get(key) or role(r)
        # a partial scheme: derive what's missing from the scheme's own window/text colours, not
        # from the platform palette (which may be the opposite brightness)
        self.window = pick("window", QPalette.Window)
        self.window_text = k.get("window_text") or (k.get("text") if k else None) or role(QPalette.WindowText)
        self.base = k.get("base") or (self.window if k else role(QPalette.Base))
        self.text = k.get("text") or (self.window_text if k else role(QPalette.Text))
        self.alt = k.get("alt") or (_mix(self.base, self.text, 0.04) if k else role(QPalette.AlternateBase))
        self.dim = k.get("dim") or _mix(self.text, self.base, 0.45)
        self.button = k.get("button") or (self.window if k else role(QPalette.Button))
        self.button_text = k.get("button_text") or (self.window_text if k else role(QPalette.ButtonText))
        self.highlight = pick("selection", QPalette.Highlight)
        self.highlight_text = pick("selection_text", QPalette.HighlightedText)
        self.tooltip = k.get("tooltip") or (self.window if k else role(QPalette.ToolTipBase))
        self.tooltip_text = k.get("tooltip_text") or (self.window_text if k else role(QPalette.ToolTipText))
        self.link = pick("link", QPalette.Link)
        self.accent = k.get("accent") or self.highlight
        self.dark = self.window.lightness() < 128
        self.border = _mix(self.window, self.text, 0.18)
        self.select = _mix(self.base, self.highlight, 0.35)       # selection tint behind normal text
        self.device_bg = _mix(self.base, self.text, 0.06)
        self.card = self.base
        self.red = k.get("negative") or QColor("#da4453" if self.dark else "#c62828")
        self.amber = k.get("neutral") or QColor("#f67400" if self.dark else "#b26b00")
        self.green = k.get("positive") or QColor("#27ae60" if self.dark else "#23853f")
        self.throttle_bg = QColor(self.red.red(), self.red.green(), self.red.blue(), 46)
        self.modified_bg = QColor(self.accent.red(), self.accent.green(), self.accent.blue(), 30)
        want = k.get("style") or _NATIVE.get("style") or ""
        keys = {x.lower(): x for x in QStyleFactory.keys()}
        self.style = keys.get(want.lower()) or keys.get((_NATIVE.get("style") or "").lower()) or "Fusion"

    def palette(self) -> QPalette:
        if self.native:
            return self._system_palette()
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

    def _system_palette(self) -> QPalette:
        p = QPalette()
        for grp in (QPalette.Active, QPalette.Inactive):
            for r, c in ((QPalette.Window, self.window), (QPalette.WindowText, self.window_text),
                         (QPalette.Base, self.base), (QPalette.AlternateBase, self.alt), (QPalette.Text, self.text),
                         (QPalette.Button, self.button), (QPalette.ButtonText, self.button_text),
                         (QPalette.Highlight, self.highlight), (QPalette.HighlightedText, self.highlight_text),
                         (QPalette.ToolTipBase, self.tooltip), (QPalette.ToolTipText, self.tooltip_text),
                         (QPalette.PlaceholderText, self.dim), (QPalette.Link, self.link),
                         (QPalette.Light, self.button.lighter(130)), (QPalette.Midlight, self.button.lighter(115)),
                         (QPalette.Mid, self.border), (QPalette.Dark, self.button.darker(150)),
                         (QPalette.Shadow, QColor(0, 0, 0)), (QPalette.BrightText, self.red)):
                p.setColor(grp, r, c)
            if hasattr(QPalette, "Accent"):                      # Qt 6.6+
                p.setColor(grp, QPalette.Accent, self.accent)
        for r in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
            p.setColor(QPalette.Disabled, r, self.dim)
        for r in (QPalette.Window, QPalette.Base, QPalette.Button, QPalette.AlternateBase):
            p.setColor(QPalette.Disabled, r, p.color(QPalette.Active, r))
        p.setColor(QPalette.Disabled, QPalette.Highlight, self.border)
        return p

    def system_stylesheet(self) -> str:
        """Only meat3's own widgets (cards, banners, dim labels): everything else is drawn by the
        desktop's style, which a stylesheet on standard widgets would switch off."""
        c = {k: v.name() for k, v in vars(self).items() if isinstance(v, QColor)}
        return f"""
QFrame#card {{ border: 1px solid {c['border']}; border-radius: 6px; background: {c['card']}; }}
QLabel#cardTitle {{ font-weight: bold; }}
QFrame#banner {{ background: {QColor(self.red.red(), self.red.green(), self.red.blue(), 40).name(QColor.HexArgb)};
    border: 1px solid {c['red']}; border-radius: 5px; }}
QLabel#bannerText {{ color: {c['text']}; }}
QLabel#dim {{ color: {c['dim']}; }}
QStatusBar {{ color: {c['dim']}; }}
"""

    def stylesheet(self) -> str:
        if self.native:
            return self.system_stylesheet()
        if self.glass:
            return self.glass_stylesheet()
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


    def glass_stylesheet(self) -> str:
        """Liquid Glass-style material: translucent fill with a top-lit sheen gradient, a bright
        specular rim on the upper edge, softer rim elsewhere, big radii and capsule controls.
        Everything sits over the backdrop that MainWindow paints (paint_backdrop)."""
        a = ASSETS.as_posix()
        txt, dim, acc = self.text.name(), self.dim.name(), self.accent.name()
        glass = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 rgba(255,255,255,40), "
                 "stop:0.45 rgba(255,255,255,16), stop:1 rgba(255,255,255,10))")
        glass_hi = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 rgba(255,255,255,70), "
                    "stop:1 rgba(255,255,255,26))")
        rim = ("border: 1px solid rgba(255,255,255,40); border-top: 1px solid rgba(255,255,255,120); "
               "border-left: 1px solid rgba(255,255,255,64);")
        popup = "#221d30"
        return f"""
QMainWindow, QMenuBar, QStatusBar, QToolBar, QTabWidget, QStackedWidget, QScrollArea,
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QMenuBar {{ color: {txt}; padding: 2px 6px; }}
QMenuBar::item {{ padding: 4px 10px; border-radius: 10px; background: transparent; }}
QMenuBar::item:selected {{ background: rgba(255,255,255,40); }}
QMenu {{ background: {popup}; color: {txt}; border: 1px solid rgba(255,255,255,60); border-radius: 12px; padding: 6px; }}
QMenu::item {{ padding: 5px 22px 5px 14px; border-radius: 8px; }}
QMenu::item:selected {{ background: rgba(255,255,255,40); }}
QMenu::separator {{ height: 1px; background: rgba(255,255,255,40); margin: 4px 8px; }}
QToolTip {{ color: {txt}; background: {popup}; border: 1px solid rgba(255,255,255,70); border-radius: 8px; padding: 6px; }}
QTabWidget::pane {{ border: none; background: transparent; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{ padding: 6px 20px; margin: 6px 3px 6px 3px; border-radius: 15px; color: {dim};
    background: rgba(255,255,255,14); border: 1px solid rgba(255,255,255,24); }}
QTabBar::tab:selected {{ color: {txt}; background: {glass_hi}; {rim} }}
QTabBar::tab:hover:!selected {{ color: {txt}; background: rgba(255,255,255,30); }}
QToolBar {{ border: none; spacing: 6px; padding: 4px 6px; }}
QToolButton {{ padding: 4px 12px; border-radius: 13px; color: {txt}; background: rgba(255,255,255,16);
    border: 1px solid rgba(255,255,255,30); border-top-color: rgba(255,255,255,70); }}
QToolButton:hover {{ background: rgba(255,255,255,36); }}
QToolButton:checked {{ background: rgba(255,138,101,90); border-color: rgba(255,190,170,160); }}
QTreeView, QTableWidget, QTableView, QListWidget, QPlainTextEdit {{ background: {glass}; color: {txt};
    alternate-background-color: rgba(255,255,255,10); {rim} border-radius: 16px;
    selection-background-color: rgba(255,255,255,56); selection-color: {txt}; gridline-color: rgba(255,255,255,20); }}
QTreeView::item, QTableView::item {{ padding: 1px 0px; }}
QTreeView::item:hover, QTableView::item:hover {{ background: rgba(255,255,255,20); }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{ background: transparent; color: {dim}; border: none;
    border-bottom: 1px solid rgba(255,255,255,40); padding: 6px 8px; }}
QTableCornerButton::section {{ background: transparent; border: none; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{ padding: 4px 10px; color: {txt}; border-radius: 12px;
    background: rgba(255,255,255,22); border: 1px solid rgba(255,255,255,40); border-top-color: rgba(255,255,255,90); }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {acc}; }}
QPushButton {{ padding: 5px 16px; color: {txt}; border-radius: 14px; background: {glass_hi};
    border: 1px solid rgba(255,255,255,50); border-top-color: rgba(255,255,255,140); }}
QPushButton:hover {{ background: rgba(255,255,255,70); }}
QPushButton:pressed {{ background: rgba(255,255,255,30); }}
QPushButton:disabled {{ color: {dim}; background: rgba(255,255,255,10); border-color: rgba(255,255,255,20); }}
QPushButton#primary {{ color: white; border-color: rgba(255,200,180,170);
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ffab8a, stop:1 #ff6f4d); }}
QPushButton#primary:disabled {{ background: rgba(255,255,255,14); color: {dim}; border-color: rgba(255,255,255,20); }}
QGroupBox {{ background: {glass}; {rim} border-radius: 18px; margin-top: 16px; padding: 12px 10px 10px 10px; color: {txt}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 14px; padding: 0 6px; color: {txt}; font-weight: bold; }}
QFrame#card {{ background: {glass}; {rim} border-radius: 18px; }}
QSlider::groove:horizontal {{ height: 6px; background: rgba(255,255,255,30); border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #ff8a65, stop:1 #ff5fa2); border-radius: 3px; }}
QSlider::sub-page:horizontal:disabled {{ background: rgba(255,255,255,30); }}
QSlider::handle:horizontal {{ width: 18px; margin: -7px 0; border-radius: 9px;
    background: qradialgradient(cx:0.35, cy:0.3, radius:0.9, fx:0.35, fy:0.3, stop:0 white, stop:1 #d9d4e8);
    border: 1px solid rgba(255,255,255,160); }}
QSlider::handle:horizontal:disabled {{ background: rgba(255,255,255,60); }}
QFrame#banner {{ background: rgba(255,107,107,50); border: 1px solid rgba(255,140,140,160); border-radius: 14px; }}
QAbstractSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 18px;
    border: none; border-top-right-radius: 12px; background: transparent; }}
QAbstractSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; width: 18px;
    border: none; border-bottom-right-radius: 12px; background: transparent; }}
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{ background: rgba(255,255,255,40); }}
QAbstractSpinBox::up-arrow {{ image: url({a}/arrow-up.svg); width: 9px; height: 9px; }}
QAbstractSpinBox::down-arrow {{ image: url({a}/arrow-down.svg); width: 9px; height: 9px; }}
QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::down-arrow:disabled {{ image: none; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: url({a}/arrow-down.svg); width: 10px; height: 10px; }}
QComboBox QAbstractItemView {{ background: {popup}; color: {txt}; border: 1px solid rgba(255,255,255,60);
    selection-background-color: rgba(255,255,255,50); }}
QCheckBox {{ color: {txt}; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 8px; background: rgba(255,255,255,24);
    border: 1px solid rgba(255,255,255,90); }}
QCheckBox::indicator:checked {{ background: {acc}; border-color: rgba(255,210,195,200); image: url({a}/check.svg); }}
QCheckBox::indicator:disabled {{ background: rgba(255,255,255,8); border-color: rgba(255,255,255,30); }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}
QScrollBar::handle:vertical {{ background: rgba(255,255,255,70); border-radius: 3px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px 4px; }}
QScrollBar::handle:horizontal {{ background: rgba(255,255,255,70); border-radius: 3px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: none; width: 0; height: 0; }}
QListWidget::item {{ padding: 3px 4px; }}
QLabel {{ color: {txt}; background: transparent; }}
QLabel#cardTitle {{ font-weight: bold; }}
QLabel#bannerText {{ color: {txt}; }}
QLabel#dim {{ color: {dim}; }}
QStatusBar {{ color: {dim}; }}
QStatusBar QLabel {{ color: {dim}; }}
QScrollArea {{ border: none; }}
"""


_backdrop_cache: dict = {}


def paint_backdrop(p: QPainter, rect) -> None:
    """The 'liquid' behind the glass: deep base + large soft colour blobs + a diagonal sheen.
    Rendered once per size into a cached pixmap (cheap to blit on every repaint)."""
    w, h = max(1, rect.width()), max(1, rect.height())
    pm = _backdrop_cache.get((w, h))
    if pm is None:
        _backdrop_cache.clear()
        pm = QPixmap(w, h)
        q = QPainter(pm)
        q.setRenderHint(QPainter.Antialiasing)
        from PySide6.QtGui import QLinearGradient, QRadialGradient
        base = QLinearGradient(0, 0, w, h)
        base.setColorAt(0.0, QColor("#120c22"))
        base.setColorAt(0.55, QColor("#1a0f2e"))
        base.setColorAt(1.0, QColor("#071a2a"))
        q.fillRect(0, 0, w, h, base)
        blobs = [  # (x, y, radius as fraction of the larger side, colour, alpha)
            (0.12, 0.18, 0.55, "#ff6a3d", 150),   # meat orange
            (0.85, 0.12, 0.45, "#c13cff", 120),   # magenta
            (0.70, 0.85, 0.60, "#1fb6ff", 115),   # cyan
            (0.25, 0.92, 0.40, "#3b5bff", 110),   # blue
            (0.52, 0.45, 0.35, "#ff4f9a", 70),    # pink core
        ]
        big = max(w, h)
        for x, y, r, col, al in blobs:
            g = QRadialGradient(QPointF(x * w, y * h), r * big)
            c0 = QColor(col)
            c0.setAlpha(al)
            c1 = QColor(col)
            c1.setAlpha(0)
            g.setColorAt(0.0, c0)
            g.setColorAt(0.55, QColor(c0.red(), c0.green(), c0.blue(), al // 3))
            g.setColorAt(1.0, c1)
            q.fillRect(0, 0, w, h, g)
        sheen = QLinearGradient(0, 0, w * 0.6, h)
        sheen.setColorAt(0.0, QColor(255, 255, 255, 26))
        sheen.setColorAt(0.35, QColor(255, 255, 255, 0))
        q.fillRect(0, 0, w, h, sheen)
        q.end()
        _backdrop_cache[(w, h)] = pm
    p.drawPixmap(rect.topLeft(), pm)


_theme = Theme("dark")


def theme() -> Theme:
    return _theme


def apply_theme(name="dark") -> Theme:
    global _theme
    app = QApplication.instance()
    _capture_native(app)
    _theme = Theme(name)
    app.setStyle(_theme.style)
    app.setPalette(_theme.palette())
    app.setStyleSheet(_theme.stylesheet())
    return _theme


class KdeSchemeWatcher(QObject):
    """Emits changed() when the Plasma colour scheme / accent / widget style changes, so the
    "System" theme follows System Settings live. Plasma replaces kdeglobals by rename, which
    drops the watch, so the file is re-added after every change; a slow re-check covers a
    config dir that didn't exist yet when meat3 started."""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.path = str(kdeglobals_path())
        self.w = QFileSystemWatcher(self)
        self.w.fileChanged.connect(self._poke)
        self.w.directoryChanged.connect(self._poke)       # catches the file appearing later
        self.debounce = QTimer(self, singleShot=True, interval=400)
        self.debounce.timeout.connect(self._fire)
        self._last = self._sig()
        self._arm()
        self.slow = QTimer(self, interval=3000)
        self.slow.timeout.connect(self._fire)
        self.slow.start()

    def _arm(self):
        if os.path.exists(self.path) and self.path not in self.w.files():
            self.w.addPath(self.path)
        d = os.path.dirname(self.path)
        if os.path.isdir(d) and d not in self.w.directories():
            self.w.addPath(d)

    def _sig(self):
        try:
            with open(self.path, "rb") as f:
                return hash(f.read())
        except OSError:
            return None

    def _poke(self, *_):
        self.debounce.start()

    def _fire(self):
        self._arm()
        sig = self._sig()
        if sig != self._last:          # the config dir changes all the time; only react to kdeglobals
            self._last = sig
            self.changed.emit()


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
