"""Graphs tab: pick sensors, get a Task-Manager / netdata style grid of live charts.

Every card is one chart with one or more series (drag a card onto another to combine them, if
the units match). Hovering any chart moves a shared crosshair across all of them and shows each
series' value at that moment. History comes from SensorStore (client-side, last 30 min), so a
newly picked sensor shows its past straight away. The picked cards are kept in QSettings.
"""
from __future__ import annotations

import json
import math
import re
import time
from typing import Optional

from PySide6.QtCore import QMimeData, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDrag, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton,
                               QScrollArea, QSplitter, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QTreeWidgetItemIterator, QVBoxLayout, QWidget)

from meat3.core.fmt import format_value
from meat3.gui.common import SensorStore, mono_font, settings, severity, severity_color, theme

MIME = "application/x-meat3-graph-card"
WINDOWS = [(60, "1 min"), (300, "5 min"), (600, "10 min"), (1800, "30 min")]
COLUMNS = [(0, "Auto"), (1, "1"), (2, "2"), (3, "3"), (4, "4")]
CARD_MIN_W, CARD_H = 360, 190
SERIES = ["#4cc9f0", "#ff8a3d", "#b388ff", "#5fd38d", "#ffd166", "#ff5d8f", "#3d8bff", "#2ec4b6"]
UNIT_COLOR = {"%": "#4c9aff", "MHz": "#4cc9f0", "°C": "#ff7a45", "W": "#ffc53d", "V": "#b388ff",
              "mV": "#b388ff", "A": "#ff5d8f", "RPM": "#5fd38d", "MiB": "#9d7bff", "MB": "#9d7bff",
              "GiB": "#9d7bff", "GB/s": "#2ec4b6", "MB/s": "#2ec4b6", "Gbps": "#4cc9f0"}
# limits/thresholds are flat lines, not worth a chart in the Temperatures/Power presets
_STATIC = re.compile(r"limit|threshold|slowdown|target|crit|emergency|max operating|delta|cap|tdp|"
                     r"rated|default|min |max ", re.I)

# preset -> id regexes, cards in pattern order
PRESETS: dict[str, list[str]] = {
    # (alternatives cover the mock daemon's older ids)
    "Overview": [r"^nv\d+\.clk\.gpc$", r"^nv\d+\.(u|util)\.gpu$", r"^nv\d+\.pwr\.(board_real|real)$",
                 r"^nv\d+\.(t|temp)\.hotspot$", r"^nv\d+\.pins\.max_a$",
                 r"^(amdgpu|amd)\d+\.(gm\.average_gfxclk_frequency|clk\.gfx)$",
                 r"^(amdgpu|amd)\d+\.(gm\.average_socket_power|pwr\.ppt)$",
                 r"^(amdgpu|amd)\d+\.(gm\.temperature_hotspot|temp\.junction)$",
                 r"^cpu\d*\.(util\.total|usage_total)$", r"^cpu\d*\.clk(\.eff_avg|_eff_avg)$",
                 r"^cpu\d*\.(k10temp\.temp1|temp\.tctl)$", r"^cpu\d*\.pmt\.ppt$",
                 r"^cpu\d*\.l3app\.game\.occupancy$"],
    "CPU cores": [r"^cpu\d*\.core\d+\.(util|usage)$"],
    "CPU clocks": [r"^cpu\d*\.core\d+\.clk_eff$"],
    "GPU": [r"^nv\d+\.(clk\.(gpc|mem|xbar)|u\.(gpu|memctl)|pwr\.board_real|t\.(gpu|hotspot|mem)|"
            r"mem\.pct|pins\.(a|max_a)|v\.nvvdd)$",
            r"^(amdgpu|amd)\d+\.(gm\.(average|current)_(gfxclk|uclk)_frequency|gm\.average_gfx_activity|"
            r"gm\.(average|current)_socket_power|gm\.temperature_(edge|hotspot|mem)|hwmon\.in0)$"],
}


def _sensor_ok(s: dict) -> bool:
    v = s.get("value")
    return isinstance(v, (int, float)) and not isinstance(v, bool) or (v is None and bool(s.get("unit")))


def preset_ids(name: str, sensors: dict[str, dict]) -> list[str]:
    if name == "Temperatures":
        return [i for i, s in sensors.items() if s.get("unit") == "°C" and not _STATIC.search(s.get("label", ""))]
    if name == "Power":
        return [i for i, s in sensors.items() if s.get("unit") == "W" and not _STATIC.search(s.get("label", ""))]
    out = []
    for pat in PRESETS.get(name, []):
        rx = re.compile(pat)
        out += [i for i in sensors if rx.search(i) and i not in out
                and not (name == "Overview" and "iGPU" in sensors[i].get("group", ""))]
    # CPU cores/clocks: natural order (core2 before core10)
    if name.startswith("CPU"):
        out.sort(key=lambda i: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", i)])
    return out


def nice_ticks(lo: float, hi: float, n: int = 4) -> tuple[float, float, float]:
    """(lo, hi, step) widened to 1/2/2.5/5 x 10^k steps."""
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    return math.floor(lo / step) * step, math.ceil(hi / step) * step, step


def fmt_ago(sec: float) -> str:
    sec = int(round(sec))
    return "now" if sec <= 0 else (f"-{sec // 60}:{sec % 60:02d}" if sec >= 60 else f"-{sec}s")


class GraphCard(QWidget):
    """One chart. ids: series shown together (same unit)."""

    def __init__(self, page: "GraphsPage", ids: list[str]):
        super().__init__()
        self.page = page
        self.ids = list(ids)
        self.setMinimumSize(220, CARD_H)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.hover = False
        self.drop_hint = False
        self.press_pos: Optional[QPointF] = None
        self.chart = QRectF()

    # ---------------------------------------------------------------- data
    @property
    def store(self) -> SensorStore:
        return self.page.store

    def unit(self) -> str:
        return self.store.sensors.get(self.ids[0], {}).get("unit", "") if self.ids else ""

    def color(self, i: int) -> QColor:
        if len(self.ids) == 1:
            return QColor(UNIT_COLOR.get(self.unit(), theme().accent.name()))
        return QColor(SERIES[i % len(SERIES)])

    def points(self, sid: str, t0: float) -> list[tuple[float, Optional[float]]]:
        h = self.store.history.get(sid)
        if not h:
            return []
        out = []
        for ts, v in reversed(h):
            out.append((ts, v))
            if ts < t0:
                break
        out.reverse()
        return out

    def legend_h(self) -> int:
        return 16 * len(self.ids) + 4 if len(self.ids) > 1 else 0

    def fit_height(self):
        self.setMinimumHeight(CARD_H + self.legend_h())

    def title(self) -> str:
        s = [self.store.sensors.get(i, {}) for i in self.ids]
        if not s:
            return ""
        if len(s) == 1:
            return s[0].get("label", self.ids[0])
        return " · ".join(x.get("label", i) for x, i in zip(s, self.ids))

    def subtitle(self) -> str:
        groups = list(dict.fromkeys(self.store.sensors.get(i, {}).get("group", "") for i in self.ids))
        if len(groups) == 1:
            g = groups[0]
            return g.split("/", 1)[0] + (" · " + g.split("/", 1)[1] if "/" in g else "")
        return " + ".join(dict.fromkeys(g.split("/", 1)[0] for g in groups))

    # ---------------------------------------------------------------- paint
    def paintEvent(self, ev):
        if not self.ids:
            return
        t = theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        bg = QColor(t.card)
        p.setPen(QPen(t.accent if self.drop_hint else t.border, 2 if self.drop_hint else 1))
        p.setBrush(bg)
        p.drawRoundedRect(r, 10, 10)
        if t.glass:     # specular rim, as the rest of the glass theme
            g = QLinearGradient(r.topLeft(), r.bottomLeft())
            g.setColorAt(0, QColor(255, 255, 255, 40))
            g.setColorAt(0.25, QColor(255, 255, 255, 0))
            p.setBrush(g)
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(r, 10, 10)

        now = self.store.t or time.time()
        win = self.page.window_s
        t0 = now - win
        mono = mono_font()
        small = QFont(mono)
        small.setPointSizeF(max(6.5, mono.pointSizeF() - 1.5))
        bold = QFont(self.font())
        bold.setBold(True)
        fm_small = QFontMetrics(small)

        # header: title / subtitle left, current (or hovered) value right
        p.setFont(bold)
        p.setPen(t.text)
        head = QRectF(12, 8, r.width() - 24, 18)
        p.drawText(head, Qt.AlignLeft | Qt.AlignVCenter, QFontMetrics(bold).elidedText(
            self.title(), Qt.ElideRight, int(head.width() * 0.62)))
        p.setFont(small)
        p.setPen(t.dim)
        p.drawText(QRectF(12, 26, r.width() - 24, 14), Qt.AlignLeft | Qt.AlignVCenter,
                   fm_small.elidedText(self.subtitle(), Qt.ElideRight, int(r.width() - 24)))
        if self.hover:     # remove button
            p.setPen(QPen(t.dim, 1.4))
            c = QPointF(r.right() - 14, 15)
            p.drawLine(c + QPointF(-4, -4), c + QPointF(4, 4))
            p.drawLine(c + QPointF(-4, 4), c + QPointF(4, -4))

        series = []
        all_vals = []
        for i, sid in enumerate(self.ids):
            pts = self.points(sid, t0)
            vals = [v for ts, v in pts if v is not None and ts >= t0]
            all_vals += vals
            series.append((sid, pts, vals))
        unit = self.unit()
        s0 = self.store.sensors.get(self.ids[0], {}) if self.ids else {}
        dec = s0.get("decimals", 0)

        legend_h = self.legend_h()
        top = 52 + legend_h
        foot = 18
        ylab_w = fm_small.horizontalAdvance("00000.0") + 4
        chart = QRectF(12 + ylab_w, top, r.width() - 24 - ylab_w, r.height() - top - foot - 14)
        self.chart = chart

        # value(s) at the hover time, else current
        ht = self.page.hover_t
        def value_at(sid, pts):
            if ht is None:
                return self.store.sensors.get(sid, {}).get("value")
            best = min(pts, key=lambda tv: abs(tv[0] - ht), default=None)
            return best[1] if best and abs(best[0] - ht) < 5 else None

        big = QFont(mono)
        big.setPointSizeF(mono.pointSizeF() + 4)
        big.setBold(True)
        if len(self.ids) == 1 and series:
            v = value_at(self.ids[0], series[0][1])
            p.setFont(big)
            col = (severity_color(severity(s0, v)) if ht is None else None) or t.text
            p.setPen(col)
            vr = QRectF(r.width() * 0.4, 6, r.width() * 0.6 - (30 if self.hover else 12), 24)
            p.drawText(vr, Qt.AlignRight | Qt.AlignVCenter, format_value(v, dec, unit))
            if ht is not None:
                p.setFont(small)
                p.setPen(t.dim)
                p.drawText(QRectF(vr.left(), 26, vr.width(), 14), Qt.AlignRight | Qt.AlignVCenter,
                           fmt_ago(now - ht))
        elif series:
            p.setFont(small)
            for i, (sid, pts, _) in enumerate(series):
                y = 44 + 16 * i
                p.setPen(Qt.NoPen)
                p.setBrush(self.color(i))
                p.drawRoundedRect(QRectF(14, y + 3, 10, 10), 2, 2)
                p.setPen(t.text)
                s = self.store.sensors.get(sid, {})
                lab = s.get("label", sid)
                v = format_value(value_at(sid, pts), s.get("decimals", 0), s.get("unit", ""))
                vw = fm_small.horizontalAdvance(v) + 4
                p.drawText(QRectF(30, y, r.width() - 46 - vw, 16), Qt.AlignLeft | Qt.AlignVCenter,
                           fm_small.elidedText(lab, Qt.ElideRight, int(r.width() - 50 - vw)))
                p.drawText(QRectF(r.width() - 12 - vw, y, vw, 16), Qt.AlignRight | Qt.AlignVCenter, v)
            if ht is not None:
                p.setPen(t.dim)
                p.drawText(QRectF(r.width() * 0.5, 26, r.width() * 0.5 - 12, 14), Qt.AlignRight | Qt.AlignVCenter,
                           fmt_ago(now - ht))

        if chart.width() < 20 or chart.height() < 20:
            return
        # y scale: % fixed 0..100 like Task Manager, everything else auto
        if unit == "%" and (not all_vals or (min(all_vals) >= 0 and max(all_vals) <= 100)):
            lo, hi, step = 0.0, 100.0, 25.0
        elif all_vals:
            vmin, vmax = min(all_vals), max(all_vals)
            # don't blow sensor noise up to full height: span at least 10% of the magnitude
            need = max(abs(vmax), abs(vmin)) * 0.1 or 1.0
            if vmax - vmin < need:
                mid = (vmax + vmin) / 2
                vmin, vmax = mid - need / 2, mid + need / 2
            if min(all_vals) >= 0 and vmin < 0:       # never show a negative axis for >= 0 data
                vmin, vmax = 0.0, vmax - vmin
            lo, hi, step = nice_ticks(vmin, vmax)
            lo = max(lo, 0.0) if min(all_vals) >= 0 else lo
        else:
            lo, hi, step = 0.0, 1.0, 0.25
        span_v = max(1e-9, hi - lo)

        def X(ts):
            return chart.left() + (ts - t0) / win * chart.width()

        def Y(v):
            return chart.bottom() - (v - lo) / span_v * chart.height()

        # grid + y labels
        p.setFont(small)
        grid = QColor(t.border)
        y_dec = 0 if float(step).is_integer() else (1 if round(step * 10, 6).is_integer() else 2)
        k = 0
        while lo + k * step <= hi + 1e-9 and k < 12:
            val = lo + k * step
            y = Y(val)
            p.setPen(QPen(grid, 1, Qt.SolidLine if k == 0 else Qt.DotLine))
            p.drawLine(QPointF(chart.left(), y), QPointF(chart.right(), y))
            p.setPen(t.dim)
            p.drawText(QRectF(4, y - 7, ylab_w + 4, 14), Qt.AlignRight | Qt.AlignVCenter, f"{val:.{y_dec}f}")
            k += 1
        # x labels
        xstep = {60: 15, 300: 60, 600: 120, 1800: 300}.get(win, win / 4)
        k = 0
        while k * xstep <= win + 1e-6:
            x = chart.right() - k * xstep / win * chart.width()
            p.setPen(QPen(grid, 1, Qt.DotLine))
            if k:
                p.drawLine(QPointF(x, chart.top()), QPointF(x, chart.bottom()))
            p.setPen(t.dim)
            p.drawText(QRectF(x - 30, chart.bottom() + 2, 60, 14) if k else QRectF(x - 60, chart.bottom() + 2, 60, 14),
                       Qt.AlignCenter if k else Qt.AlignRight, fmt_ago(k * xstep))
            k += 1

        # series: area fill (single series) + line, decimated to ~2 points per pixel column
        p.save()
        p.setClipRect(chart.adjusted(-1, -2, 1, 1))
        for i, (sid, pts, _) in enumerate(series):
            col = self.color(i)
            path, fill = QPainterPath(), QPainterPath()
            started = False
            first = last = None
            buckets: dict[int, list] = {}
            dense = len(pts) > chart.width() * 2
            seq = pts
            if dense:
                order = []
                for ts, v in pts:
                    if v is None:
                        continue
                    bx = int(X(ts))
                    b = buckets.get(bx)
                    if b is None:
                        buckets[bx] = [ts, v, v]
                        order.append(bx)
                    else:
                        b[1], b[2] = min(b[1], v), max(b[2], v)
                seq = []
                for bx in order:
                    ts, mn, mx = buckets[bx]
                    seq += [(ts, mn), (ts, mx)] if mn != mx else [(ts, mn)]
            for ts, v in seq:
                if v is None:
                    started = False
                    continue
                pt = QPointF(X(ts), Y(v))
                if not started:
                    path.moveTo(pt)
                    started = True
                else:
                    path.lineTo(pt)
                if first is None:
                    first = pt
                    fill.moveTo(QPointF(pt.x(), chart.bottom()))
                fill.lineTo(pt)
                last = pt
            if last is None:
                continue
            if len(self.ids) == 1:
                fill.lineTo(QPointF(last.x(), chart.bottom()))
                fill.closeSubpath()
                g = QLinearGradient(chart.topLeft(), chart.bottomLeft())
                c1, c2 = QColor(col), QColor(col)
                c1.setAlpha(110)
                c2.setAlpha(12)
                g.setColorAt(0, c1)
                g.setColorAt(1, c2)
                p.fillPath(fill, g)
            p.setPen(QPen(col, 1.8))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        p.restore()
        if not all_vals:
            p.setPen(t.dim)
            p.drawText(chart, Qt.AlignCenter, "no data yet")

        # shared crosshair
        if ht is not None and t0 <= ht <= now:
            x = X(ht)
            p.setPen(QPen(t.text, 1, Qt.DashLine))
            p.drawLine(QPointF(x, chart.top()), QPointF(x, chart.bottom()))
            for i, (sid, pts, _) in enumerate(series):
                v = value_at(sid, pts)
                if v is not None and lo <= v <= hi:
                    p.setPen(QPen(t.card, 2))
                    p.setBrush(self.color(i))
                    p.drawEllipse(QPointF(x, Y(v)), 3.5, 3.5)

        # footer: min / avg / max over the window
        if all_vals and len(self.ids) == 1:
            p.setFont(small)
            p.setPen(t.dim)
            txt = (f"min {format_value(min(all_vals), dec, unit)}   avg "
                   f"{format_value(sum(all_vals) / len(all_vals), dec, unit)}   max {format_value(max(all_vals), dec, unit)}")
            p.drawText(QRectF(12, r.height() - foot, r.width() - 24, foot - 2), Qt.AlignLeft | Qt.AlignVCenter, txt)

    # ---------------------------------------------------------------- mouse
    def _close_rect(self) -> QRectF:
        return QRectF(self.width() - 26, 4, 22, 22)

    def enterEvent(self, e):
        self.hover = True
        self.update()

    def leaveEvent(self, e):
        self.hover = False
        self.page.set_hover(None)

    def mouseMoveEvent(self, e):
        pos = e.position()
        if self.press_pos is not None and (pos - self.press_pos).manhattanLength() > 12:
            self.press_pos = None
            self._start_drag()
            return
        c = self.chart
        if c.width() > 0 and c.left() <= pos.x() <= c.right() and c.top() - 20 <= pos.y() <= c.bottom() + 16:
            now = self.store.t or time.time()
            self.page.set_hover(now - self.page.window_s + (pos.x() - c.left()) / c.width() * self.page.window_s)
        else:
            self.page.set_hover(None)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            if self._close_rect().contains(e.position()):
                self.page.remove_card(self)
                return
            self.press_pos = e.position()

    def mouseReleaseEvent(self, e):
        self.press_pos = None

    def _start_drag(self):
        d = QDrag(self)
        m = QMimeData()
        m.setData(MIME, str(self.page.cards.index(self)).encode())
        d.setMimeData(m)
        d.setPixmap(self.grab().scaledToWidth(220, Qt.SmoothTransformation))
        d.exec(Qt.MoveAction)

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(MIME) and e.source() is not self:
            e.acceptProposedAction()
            self.drop_hint = True
            self.update()

    def dragLeaveEvent(self, e):
        self.drop_hint = False
        self.update()

    def dropEvent(self, e):
        self.drop_hint = False
        self.update()
        src = e.source()
        if isinstance(src, GraphCard) and src is not self:
            if self.page.merge(src, self):
                e.acceptProposedAction()

    def _menu(self, pos):
        m = QMenu(self)
        if len(self.ids) > 1:
            m.addAction("Split into separate graphs", lambda: self.page.split(self))
        others = [c for c in self.page.cards if c is not self and c.unit() == self.unit()]
        if others:
            sub = m.addMenu("Combine with")
            for c in others:
                sub.addAction(c.title(), lambda c=c: self.page.merge(self, c))
        i = self.page.cards.index(self)
        if i > 0:
            m.addAction("Move left", lambda: self.page.move(self, -1))
        if i < len(self.page.cards) - 1:
            m.addAction("Move right", lambda: self.page.move(self, 1))
        m.addSeparator()
        m.addAction("Remove", lambda: self.page.remove_card(self))
        m.exec(self.mapToGlobal(pos))


class _Grid(QWidget):
    """Card container; re-flows the grid when resized (Auto columns)."""

    def __init__(self, page: "GraphsPage"):
        super().__init__()
        self.page = page
        self.lay = QGridLayout(self)
        self.lay.setContentsMargins(8, 8, 8, 8)
        self.lay.setSpacing(10)
        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.cols = 0

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.page.columns == 0 and self.page.ncols() != self.cols:
            self.page.relayout()


class GraphsPage(QWidget):
    status = Signal(str)

    def __init__(self, store: SensorStore, parent=None):
        super().__init__(parent)
        self.store = store
        self.cards: list[GraphCard] = []
        self.hover_t: Optional[float] = None
        st = settings()
        self.window_s = int(st.value("graphs/window", 300) or 300)
        self.columns = int(st.value("graphs/columns", 0) or 0)
        self.sig = None
        self.syncing = False

        v = QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        bar = QHBoxLayout()
        self.btn_pick = QToolButton()
        self.btn_pick.setText("☰ Sensors")
        self.btn_pick.setCheckable(True)
        self.btn_pick.setChecked(st.value("graphs/picker", "true") in (True, "true"))
        self.btn_pick.toggled.connect(self._toggle_picker)
        bar.addWidget(self.btn_pick)
        self.btn_presets = QToolButton()
        self.btn_presets.setText("Presets")
        self.btn_presets.setPopupMode(QToolButton.InstantPopup)
        pm = QMenu(self)
        for name in list(PRESETS) + ["Temperatures", "Power"]:
            pm.addAction(name, lambda n=name: self.add_preset(n))
        self.btn_presets.setMenu(pm)
        bar.addWidget(self.btn_presets)
        bar.addSpacing(12)
        bar.addWidget(QLabel("Window"))
        self.cmb_window = QComboBox()
        for s, lab in WINDOWS:
            self.cmb_window.addItem(lab, s)
        self.cmb_window.setCurrentIndex(max(0, [s for s, _ in WINDOWS].index(self.window_s)
                                             if self.window_s in [s for s, _ in WINDOWS] else 1))
        self.cmb_window.currentIndexChanged.connect(self._set_window)
        bar.addWidget(self.cmb_window)
        bar.addWidget(QLabel("Columns"))
        self.cmb_cols = QComboBox()
        for n, lab in COLUMNS:
            self.cmb_cols.addItem(lab, n)
        self.cmb_cols.setCurrentIndex(max(0, min(len(COLUMNS) - 1, self.columns)))
        self.cmb_cols.currentIndexChanged.connect(self._set_columns)
        bar.addWidget(self.cmb_cols)
        bar.addStretch(1)
        self.hint = QLabel("Drag a graph onto another to combine · hover for values · right-click for more")
        self.hint.setStyleSheet(f"color:{theme().dim.name()}")
        bar.addWidget(self.hint)
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self.clear)
        bar.addWidget(self.btn_clear)
        v.addLayout(bar)

        self.splitter = QSplitter(Qt.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter sensors…")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._filter)
        lv.addWidget(self.filter)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemChanged.connect(self._item_changed)
        lv.addWidget(self.tree, 1)
        self.left = left
        self.splitter.addWidget(left)
        self.grid = _Grid(self)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.grid)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.viewport().setAutoFillBackground(False)
        self.grid.setAutoFillBackground(False)
        self.splitter.addWidget(self.scroll)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([280, 900])
        v.addWidget(self.splitter, 1)
        self.left.setVisible(self.btn_pick.isChecked())

        saved = st.value("graphs/cards")
        self.pending: Optional[list[list[str]]] = None
        if saved is None:
            self.pending = [["__preset__", "Overview"]]
        else:
            try:
                self.pending = [list(map(str, c)) for c in json.loads(saved)]
            except (TypeError, ValueError):
                self.pending = []
        self._repaint = QTimer(self)
        self._repaint.setSingleShot(True)
        self._repaint.timeout.connect(self._update_cards)
        store.updated.connect(self.refresh)
        self.relayout()

    # ---------------------------------------------------------------- state
    def save(self):
        settings().setValue("graphs/cards", json.dumps([c.ids for c in self.cards]))

    def ids(self) -> set[str]:
        return {i for c in self.cards for i in c.ids}

    def add_sensor(self, sid: str, quiet: bool = False) -> bool:
        if sid in self.ids():
            return False
        self.cards.append(GraphCard(self, [sid]))
        self._after_change()
        if not quiet:
            self.status.emit(f"Graphing {self.store.sensors.get(sid, {}).get('label', sid)}")
        return True

    def remove_sensor(self, sid: str):
        for c in list(self.cards):
            if sid in c.ids:
                c.ids.remove(sid)
                if not c.ids:
                    self._drop(c)
        self._after_change()

    def add_preset(self, name: str):
        ids = [i for i in preset_ids(name, self.store.sensors) if i not in self.ids()]
        for i in ids:
            self.cards.append(GraphCard(self, [i]))
        self._after_change()
        self.status.emit(f"{name}: added {len(ids)} graph(s)" if ids else f"{name}: nothing new to add")

    def remove_card(self, card: GraphCard):
        self._drop(card)
        self._after_change()

    def _drop(self, card: GraphCard):
        if card in self.cards:
            self.cards.remove(card)
        card.ids = []
        card.hide()
        self.grid.lay.removeWidget(card)
        card.deleteLater()        # stays parented to the grid: Qt owns it until the event loop deletes it

    def clear(self):
        for c in list(self.cards):
            self._drop(c)
        self._after_change()

    def merge(self, src: GraphCard, dst: GraphCard) -> bool:
        if src.unit() != dst.unit():
            self.status.emit(f"Can't combine {src.unit() or 'unitless'} with {dst.unit() or 'unitless'} — "
                             "only graphs with the same unit share an axis")
            return False
        dst.ids += [i for i in src.ids if i not in dst.ids]
        self._drop(src)
        self._after_change()
        return True

    def split(self, card: GraphCard):
        i = self.cards.index(card)
        rest = card.ids[1:]
        card.ids = card.ids[:1]
        for k, sid in enumerate(rest):
            self.cards.insert(i + 1 + k, GraphCard(self, [sid]))
        self._after_change()

    def move(self, card: GraphCard, d: int):
        i = self.cards.index(card)
        j = max(0, min(len(self.cards) - 1, i + d))
        self.cards.insert(j, self.cards.pop(i))
        self._after_change()

    def _after_change(self):
        self.relayout()
        self.save()
        self._sync_checks()

    def set_hover(self, t: Optional[float]):
        if t != self.hover_t:
            self.hover_t = t
            for c in self.cards:
                c.update()

    # ---------------------------------------------------------------- layout
    def ncols(self) -> int:
        if self.columns:
            return self.columns
        w = self.scroll.viewport().width() or 900
        return max(1, min(4, w // CARD_MIN_W))

    def relayout(self):
        lay = self.grid.lay
        while lay.count():
            it = lay.takeAt(0)
            if it.widget() is not None and it.widget() is not self.grid.empty:
                it.widget().setParent(self.grid)
        n = self.ncols()
        self.grid.cols = n
        if not self.cards:
            self.grid.empty.setText("<h3>No graphs yet</h3>Tick sensors on the left, pick a preset, or "
                                    "right-click a sensor on the Sensors tab → Add to Graphs.")
            self.grid.empty.show()
            lay.addWidget(self.grid.empty, 0, 0)
            lay.setRowStretch(0, 1)
            return
        self.grid.empty.hide()
        for r in range(lay.rowCount()):
            lay.setRowStretch(r, 0)
        for c in range(4):
            lay.setColumnStretch(c, 1 if c < n else 0)
        for k, card in enumerate(self.cards):
            card.fit_height()
            lay.addWidget(card, k // n, k % n)
            card.show()
        lay.setRowStretch(len(self.cards) // n + 1, 1)

    def _toggle_picker(self, on: bool):
        self.left.setVisible(on)
        settings().setValue("graphs/picker", "true" if on else "false")

    def _set_window(self, _=None):
        self.window_s = int(self.cmb_window.currentData())
        settings().setValue("graphs/window", self.window_s)
        self._update_cards()

    def _set_columns(self, _=None):
        self.columns = int(self.cmb_cols.currentData())
        settings().setValue("graphs/columns", self.columns)
        self.relayout()

    # ---------------------------------------------------------------- picker
    def _build_tree(self):
        self.syncing = True
        self.tree.clear()
        nodes: dict[str, QTreeWidgetItem] = {}
        picked = self.ids()
        for sid, s in self.store.sensors.items():
            if not _sensor_ok(s):
                continue
            path = s.get("group", "Other").split("/")
            parent = None
            key = ""
            for part in path:
                key = f"{key}/{part}"
                it = nodes.get(key)
                if it is None:
                    it = QTreeWidgetItem([part])
                    it.setFlags(it.flags() & ~Qt.ItemIsUserCheckable)
                    if parent is None:
                        self.tree.addTopLevelItem(it)
                        f = it.font(0)
                        f.setBold(True)
                        it.setFont(0, f)
                    else:
                        parent.addChild(it)
                    nodes[key] = it
                parent = it
            unit = s.get("unit", "")
            leaf = QTreeWidgetItem([s.get("label", sid) + (f"  [{unit}]" if unit else "")])
            leaf.setData(0, Qt.UserRole, sid)
            leaf.setToolTip(0, f"{sid}\n{s.get('hint', '')}".strip())
            leaf.setFlags(leaf.flags() | Qt.ItemIsUserCheckable)
            leaf.setCheckState(0, Qt.Checked if sid in picked else Qt.Unchecked)
            parent.addChild(leaf)
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setExpanded(True)
        self.syncing = False
        self._filter(self.filter.text())

    def _sync_checks(self):
        self.syncing = True
        picked = self.ids()
        it = QTreeWidgetItemIterator(self.tree)
        while it.value():
            item = it.value()
            sid = item.data(0, Qt.UserRole)
            if sid:
                item.setCheckState(0, Qt.Checked if sid in picked else Qt.Unchecked)
            it += 1
        self.syncing = False

    def _item_changed(self, item: QTreeWidgetItem, col: int):
        if self.syncing:
            return
        sid = item.data(0, Qt.UserRole)
        if not sid:
            return
        if item.checkState(0) == Qt.Checked:
            self.add_sensor(sid)
        else:
            self.remove_sensor(sid)

    def _filter(self, text: str):
        words = text.lower().split()

        def visit(item: QTreeWidgetItem, inherited: bool) -> bool:
            own = " ".join([item.text(0), item.data(0, Qt.UserRole) or ""]).lower()
            hit = inherited or all(w in own for w in words)
            any_child = False
            for k in range(item.childCount()):
                any_child |= visit(item.child(k), hit)
            show = hit or any_child or not words
            item.setHidden(not show)
            if words and any_child:
                item.setExpanded(True)
            return show

        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i), False)

    # ---------------------------------------------------------------- updates
    def refresh(self):
        sig = len(self.store.sensors)
        if sig != self.sig:
            self.sig = sig
            if self.pending is not None and self.store.sensors:
                pend, self.pending = self.pending, None
                for ids in pend:
                    if ids[:1] == ["__preset__"]:
                        for i in preset_ids(ids[1], self.store.sensors):
                            if i not in self.ids():
                                self.cards.append(GraphCard(self, [i]))
                    else:
                        self.cards.append(GraphCard(self, ids))
                self.relayout()
                self.save()
            self._build_tree()
        if self.isVisible() and not self._repaint.isActive():
            self._repaint.start(0)

    def _update_cards(self):
        for c in self.cards:
            c.update()

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()
        self.relayout()
