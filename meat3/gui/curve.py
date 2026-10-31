"""V/F curve editor widget (Afterburner-style): voltage on X, clock on Y, draggable points.

Used by the Overclock tab for controls of kind "curve". The control's value is
    {"points": [{"i": idx, "mv": volt, "base": stock MHz, "mhz": current MHz, "delta": MHz, ...}],
     "lock": {"min": .., "max": ..}        optional per-point delta bounds (MHz)
     "mode": "delta" | "absolute"}         delta: edits are MHz offsets per point (NVIDIA);
                                           absolute: points carry editable (mhz, mv) pairs (AMD OD_VDDC_CURVE)
The widget's value() returns {"deltas": {idx: MHz}} in delta mode or {"points": [{"i","mhz","mv"}]} in
absolute mode, containing every point (the daemon applies what differs).
"""
from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSpinBox, QVBoxLayout,
                               QWidget)

from meat3.gui.common import mono_font, theme


def _snap(v: float, step: float) -> float:
    return round(v / step) * step if step else v


class CurvePlot(QWidget):
    changed = Signal()

    PAD_L, PAD_R, PAD_T, PAD_B = 54, 34, 12, 30
    HIT = 9

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(260)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.points: list[dict] = []        # as given by the control
        self.edit: list[float] = []         # edited Y (MHz) per point
        self.edit_mv: list[float] = []      # edited X (mV) per point (absolute mode only)
        self.mode = "delta"
        self.lo = self.hi = None            # delta bounds
        self.step = 5.0
        self.sel: Optional[int] = None
        self.hover: Optional[int] = None
        self._drag: Optional[tuple[int, float, float]] = None   # (index, y0, mhz0)
        self.touched = False                # user edited since the last load/reset

    # ---- data ----
    def load(self, c: dict) -> None:
        """Take the control's current points. Untouched: the edit curve follows the hardware; touched:
        the user's edits are kept (unless the point set changed shape) and compared against the new points."""
        v = c.get("value") or {}
        pts = list(v.get("points") or [])
        same_shape = len(pts) == len(self.points) and all(p["i"] == q["i"] for p, q in zip(pts, self.points))
        self.points = pts
        self.mode = v.get("mode") or "delta"
        lk = v.get("lock") or {}
        self.lo, self.hi = lk.get("min"), lk.get("max")
        self.step = float(c.get("step") or 5)
        if not (self.touched and same_shape):
            self.edit = [float(p.get("mhz") or 0) for p in self.points]
            self.edit_mv = [float(p.get("mv") or 0) for p in self.points]
            self.touched = False
        if self.sel is not None and self.sel >= len(self.points):
            self.sel = None
        self.update()

    def dirty(self) -> bool:
        if not self.touched:
            return False
        for p, y, x in zip(self.points, self.edit, self.edit_mv):
            if abs(y - float(p.get("mhz") or 0)) > 1e-6 or (self.mode == "absolute" and abs(x - float(p.get("mv") or 0)) > 1e-6):
                return True
        return False

    def value(self) -> dict:
        if self.mode == "absolute":
            return {"points": [{"i": p["i"], "mhz": round(y), "mv": round(x)}
                               for p, y, x in zip(self.points, self.edit, self.edit_mv)]}
        return {"deltas": {str(p["i"]): round(y - float(p.get("base") or 0), 3) for p, y in zip(self.points, self.edit)}}

    def set_from_value(self, v: Any) -> None:
        """Load an edited value (profile / pending edit) onto the current points."""
        if not isinstance(v, dict):
            return
        self.touched = True
        if "deltas" in v and self.mode == "delta":
            d = {int(k): float(x) for k, x in (v.get("deltas") or {}).items()}
            self.edit = [float(p.get("base") or 0) + d.get(p["i"], float(p.get("delta") or 0)) for p in self.points]
        elif "points" in v and self.mode == "absolute":
            by = {int(q["i"]): q for q in v.get("points") or []}
            self.edit = [float(by.get(p["i"], p).get("mhz") or 0) for p in self.points]
            self.edit_mv = [float(by.get(p["i"], p).get("mv") or 0) for p in self.points]
        self.update()
        self.changed.emit()

    def reset(self) -> None:
        self.edit = [float(p.get("mhz") or 0) for p in self.points]
        self.edit_mv = [float(p.get("mv") or 0) for p in self.points]
        self.touched = False
        self.update()
        self.changed.emit()

    def stock(self) -> None:
        """Delta mode: every delta to 0 (stock curve)."""
        if self.mode == "delta":
            self.edit = [float(p.get("base") or 0) for p in self.points]
            self.touched = True
            self.update()
            self.changed.emit()

    def _clamp(self, i: int, y: float) -> float:
        p = self.points[i]
        if self.mode == "delta":
            base = float(p.get("base") or 0)
            d = y - base
            if self.lo is not None:
                d = max(d, float(self.lo))
            if self.hi is not None:
                d = min(d, float(self.hi))
            return base + d
        r = p.get("mhz_range")
        if r:
            y = min(max(y, float(r[0])), float(r[1]))
        return y

    def offset_all(self, mhz: float) -> None:
        self.edit = [self._clamp(i, y + mhz) for i, y in enumerate(self.edit)]
        self.touched = True
        self.update()
        self.changed.emit()

    def flatten_above(self) -> None:
        """Every point right of the selected one takes the selected point's clock (the Afterburner
        'flatten' move: the curve never asks for more clock above that voltage)."""
        if self.sel is None:
            return
        y = self.edit[self.sel]
        for i in range(self.sel + 1, len(self.edit)):
            self.edit[i] = self._clamp(i, y)
        self.touched = True
        self.update()
        self.changed.emit()

    def set_selected(self, mhz: float) -> None:
        if self.sel is None:
            return
        self.edit[self.sel] = self._clamp(self.sel, _snap(mhz, self.step))
        self._monotone_from(self.sel)
        self.touched = True
        self.update()
        self.changed.emit()

    def _monotone_from(self, i: int) -> None:
        # keep the curve non-decreasing: raising a point lifts the ones above it, lowering pushes the ones below
        for k in range(i + 1, len(self.edit)):
            if self.edit[k] < self.edit[i]:
                self.edit[k] = self._clamp(k, self.edit[i])
        for k in range(i - 1, -1, -1):
            if self.edit[k] > self.edit[i]:
                self.edit[k] = self._clamp(k, self.edit[i])

    # ---- geometry ----
    def _ranges(self) -> tuple[float, float, float, float]:
        if not self.points:
            return 0, 1, 0, 1
        xs = self.edit_mv if self.mode == "absolute" else [float(p.get("mv") or 0) for p in self.points]
        ys = [float(p.get("mhz") or 0) for p in self.points] + list(self.edit) + \
             [float(p.get("base") or 0) for p in self.points if p.get("base")]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        if x1 - x0 < 1:
            x1 = x0 + 1
        if y1 - y0 < 1:
            y1 = y0 + 1
        my = (y1 - y0) * 0.08
        return x0, x1, y0 - my, y1 + my

    def _plot_rect(self) -> QRectF:
        return QRectF(self.PAD_L, self.PAD_T, max(10, self.width() - self.PAD_L - self.PAD_R),
                      max(10, self.height() - self.PAD_T - self.PAD_B))

    def _to_px(self, mv: float, mhz: float) -> QPointF:
        x0, x1, y0, y1 = self._ranges()
        r = self._plot_rect()
        return QPointF(r.left() + (mv - x0) / (x1 - x0) * r.width(),
                       r.bottom() - (mhz - y0) / (y1 - y0) * r.height())

    def _from_py(self, py: float) -> float:
        x0, x1, y0, y1 = self._ranges()
        r = self._plot_rect()
        return y0 + (r.bottom() - py) / r.height() * (y1 - y0)

    def _nearest(self, pos: QPointF) -> Optional[int]:
        best, bd = None, self.HIT ** 2
        for i, (p, y, x) in enumerate(zip(self.points, self.edit, self.edit_mv)):
            q = self._to_px(x if self.mode == "absolute" else float(p.get("mv") or 0), y)
            d = (q.x() - pos.x()) ** 2 + (q.y() - pos.y()) ** 2
            if d < bd:
                best, bd = i, d
        return best

    # ---- events ----
    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or not self.points:
            return
        i = self._nearest(e.position())
        if i is None:
            # nearest by X only, so clicking anywhere on a column selects that point
            xs = [self._to_px(x if self.mode == "absolute" else float(p.get("mv") or 0), 0).x()
                  for p, x in zip(self.points, self.edit_mv)]
            i = min(range(len(xs)), key=lambda k: abs(xs[k] - e.position().x()))
        self.sel = i
        self._drag = (i, e.position().y(), self.edit[i])
        self.setCursor(Qt.ClosedHandCursor)
        self.update()
        self.changed.emit()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            i, y0, mhz0 = self._drag
            d = self._from_py(e.position().y()) - self._from_py(y0)
            self.edit[i] = self._clamp(i, _snap(mhz0 + d, self.step))
            self.touched = True
            if not (e.modifiers() & Qt.ShiftModifier):
                self._monotone_from(i)
            self.update()
            self.changed.emit()
        else:
            h = self._nearest(e.position())
            if h != self.hover:
                self.hover = h
                self.setCursor(Qt.OpenHandCursor if h is not None else Qt.ArrowCursor)
                self.update()

    def mouseReleaseEvent(self, e):
        if self._drag is not None:
            self._drag = None
            self.setCursor(Qt.OpenHandCursor)
            self.update()

    def keyPressEvent(self, e):
        if self.sel is None:
            return super().keyPressEvent(e)
        step = self.step * (5 if e.modifiers() & Qt.ShiftModifier else 1)
        if e.key() in (Qt.Key_Up, Qt.Key_Plus, Qt.Key_Equal):
            self.set_selected(self.edit[self.sel] + step)
        elif e.key() in (Qt.Key_Down, Qt.Key_Minus):
            self.set_selected(self.edit[self.sel] - step)
        elif e.key() == Qt.Key_Left and self.sel > 0:
            self.sel -= 1; self.update(); self.changed.emit()
        elif e.key() == Qt.Key_Right and self.sel < len(self.points) - 1:
            self.sel += 1; self.update(); self.changed.emit()
        else:
            return super().keyPressEvent(e)

    # ---- painting ----
    def paintEvent(self, ev):
        t = theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self._plot_rect()
        p.fillRect(self.rect(), t.card)
        if not self.points:
            p.setPen(t.dim)
            p.drawText(self.rect(), Qt.AlignCenter, "no curve points")
            return
        x0, x1, y0, y1 = self._ranges()
        fm = QFontMetrics(self.font())
        # grid
        pen = QPen(t.border); pen.setWidthF(1)
        p.setPen(pen)
        for k in range(6):
            yv = y0 + (y1 - y0) * k / 5
            py = self._to_px(x0, yv).y()
            p.drawLine(QPointF(r.left(), py), QPointF(r.right(), py))
            p.setPen(t.dim)
            p.drawText(QRectF(0, py - fm.height() / 2, self.PAD_L - 6, fm.height()), Qt.AlignRight | Qt.AlignVCenter,
                       f"{yv:.0f}")
            p.setPen(pen)
        for k in range(6):
            xv = x0 + (x1 - x0) * k / 5
            px = self._to_px(xv, y0).x()
            p.drawLine(QPointF(px, r.top()), QPointF(px, r.bottom()))
            p.setPen(t.dim)
            p.drawText(QRectF(px - 30, r.bottom() + 4, 60, fm.height()), Qt.AlignCenter, f"{xv:.0f} mV")
            p.setPen(pen)
        # stock (base) curve, dotted
        if any(q.get("base") for q in self.points) and self.mode == "delta":
            path = QPainterPath()
            for k, q in enumerate(self.points):
                pt = self._to_px(float(q.get("mv") or 0), float(q.get("base") or 0))
                path.moveTo(pt) if k == 0 else path.lineTo(pt)
            pen = QPen(t.dim); pen.setStyle(Qt.DotLine); pen.setWidthF(1.2)
            p.setPen(pen); p.drawPath(path)
        # applied (current) curve, thin
        path = QPainterPath()
        for k, q in enumerate(self.points):
            pt = self._to_px(float(q.get("mv") or 0), float(q.get("mhz") or 0))
            path.moveTo(pt) if k == 0 else path.lineTo(pt)
        pen = QPen(t.dim); pen.setWidthF(1.4)
        p.setPen(pen); p.drawPath(path)
        # edited curve, accent
        path = QPainterPath()
        for k, (q, y, x) in enumerate(zip(self.points, self.edit, self.edit_mv)):
            pt = self._to_px(x if self.mode == "absolute" else float(q.get("mv") or 0), y)
            path.moveTo(pt) if k == 0 else path.lineTo(pt)
        pen = QPen(t.accent); pen.setWidthF(2.2)
        p.setPen(pen); p.drawPath(path)
        # points
        n = len(self.points)
        every = max(1, n // 40)
        for k, (q, y, x) in enumerate(zip(self.points, self.edit, self.edit_mv)):
            if k % every and k not in (self.sel, self.hover) and k != n - 1:
                continue
            pt = self._to_px(x if self.mode == "absolute" else float(q.get("mv") or 0), y)
            rad = 5.5 if k == self.sel else 4.0 if k == self.hover else 2.6
            col = t.green if k == self.sel else t.accent
            p.setPen(QPen(col, 1.2)); p.setBrush(col if k in (self.sel, self.hover) else t.card)
            p.drawEllipse(pt, rad, rad)
        # readout
        k = self.sel if self.sel is not None else self.hover
        if k is not None:
            q = self.points[k]
            mv = self.edit_mv[k] if self.mode == "absolute" else float(q.get("mv") or 0)
            txt = f"point {q['i']}: {mv:.0f} mV  {self.edit[k]:.0f} MHz"
            if self.mode == "delta":
                txt += f"  (stock {float(q.get('base') or 0):.0f}, delta {self.edit[k] - float(q.get('base') or 0):+.0f})"
            p.setPen(t.text)
            p.drawText(QRectF(r.left() + 6, r.top() + 2, r.width() - 12, fm.height()), Qt.AlignLeft, txt)


class CurveEditor(QWidget):
    """Plot plus a toolbar; `value()`/`set_value()`/`dirty()` follow the ControlRow protocol."""
    edited = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.plot = CurvePlot()
        self.plot.changed.connect(self._changed)
        lay.addWidget(self.plot, 1)
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.sel_label = QLabel("select a point")
        self.sel_label.setObjectName("dim")
        bar.addWidget(self.sel_label)
        self.sel_spin = QSpinBox()
        self.sel_spin.setRange(0, 10000)
        self.sel_spin.setSuffix(" MHz")
        self.sel_spin.setFont(mono_font())
        self.sel_spin.setKeyboardTracking(False)
        self.sel_spin.valueChanged.connect(self._spin)
        self.sel_spin.setEnabled(False)
        bar.addWidget(self.sel_spin)
        self.btn_flat = QPushButton("Flatten above")
        self.btn_flat.setToolTip("Every point at a higher voltage takes the selected point's clock")
        self.btn_flat.clicked.connect(self.plot.flatten_above)
        bar.addWidget(self.btn_flat)
        self.off_spin = QSpinBox()
        self.off_spin.setRange(-1000, 1000)
        self.off_spin.setSingleStep(5)
        self.off_spin.setSuffix(" MHz")
        self.off_spin.setFont(mono_font())
        self.off_spin.setToolTip("Shift every point by this many MHz")
        bar.addWidget(self.off_spin)
        self.btn_off = QPushButton("Offset all")
        self.btn_off.clicked.connect(lambda: self.plot.offset_all(self.off_spin.value()))
        bar.addWidget(self.btn_off)
        bar.addStretch(1)
        self.btn_stock = QPushButton("Stock curve")
        self.btn_stock.setToolTip("Every delta to 0 (not applied until you press Apply)")
        self.btn_stock.clicked.connect(self.plot.stock)
        bar.addWidget(self.btn_stock)
        self.btn_reset = QPushButton("Revert")
        self.btn_reset.setToolTip("Back to the curve currently applied on the card")
        self.btn_reset.clicked.connect(self.plot.reset)
        bar.addWidget(self.btn_reset)
        lay.addLayout(bar)
        self._sync = False

    def _changed(self):
        k = self.plot.sel
        if k is not None and self.plot.points:
            self._sync = True
            self.sel_spin.setEnabled(True)
            self.sel_spin.setValue(int(round(self.plot.edit[k])))
            self._sync = False
            self.sel_label.setText(f"point {self.plot.points[k]['i']} @ {self.plot.edit_mv[k]:.0f} mV")
        else:
            self.sel_spin.setEnabled(False)
            self.sel_label.setText("select a point")
        self.btn_flat.setEnabled(k is not None)
        self.btn_stock.setVisible(self.plot.mode == "delta")
        self.edited.emit()

    def _spin(self, v):
        if not self._sync:
            self.plot.set_selected(float(v))

    def load(self, c: dict) -> None:
        self.plot.load(c)
        self._changed()

    def value(self) -> dict:
        return self.plot.value()

    def set_value(self, v: Any) -> None:
        self.plot.set_from_value(v)

    def dirty(self) -> bool:
        return self.plot.dirty()

    def setEnabled(self, on: bool) -> None:  # noqa: N802
        super().setEnabled(on)
