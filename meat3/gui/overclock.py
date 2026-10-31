"""Overclock tab: per-device forms built from the daemon's Control list, plus profiles."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QGridLayout,
                               QHBoxLayout, QInputDialog, QLabel, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QSlider, QSpinBox, QVBoxLayout,
                               QWidget)

from meat3.core.fmt import NONE_TEXT
from meat3.gui.common import config_dir, device_badge, mono_font, theme
from meat3.gui.curve import CurveEditor


def profiles_dir() -> Path:
    return config_dir() / "profiles"


def _decimals(step: Optional[float]) -> int:
    if not step:
        return 3
    s = f"{step:.6f}".rstrip("0")
    return max(0, len(s.split(".")[1]) if "." in s else 0)


def fmt_control_value(c: dict, v: Any) -> str:
    if v is None:
        return NONE_TEXT
    k = c.get("kind")
    if k == "bool":
        return "on" if v else "off"
    if k == "curve":
        if isinstance(v, dict) and v.get("points"):
            pts = v["points"]
            top = max(float(p.get("mhz") or 0) for p in pts)
            return f"{len(pts)} pts, top {top:.0f} {c.get('unit') or ''}".strip()
        if isinstance(v, dict) and v.get("deltas"):
            ds = [float(x) for x in v["deltas"].values()]
            return f"{len(ds)} deltas {min(ds):+.0f}..{max(ds):+.0f} {c.get('unit') or ''}".strip()
        return "stock" if isinstance(v, dict) else NONE_TEXT
    if k == "float":
        v = f"{float(v):.{_decimals(c.get('step'))}f}"
    elif k == "int":
        v = str(int(round(float(v))))
    unit = c.get("unit") or ""
    return f"{v} {unit}".strip()


def values_equal(c: dict, a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is b
    if c.get("kind") in ("int", "float"):
        try:
            tol = (c.get("step") or 1e-6) / 2 if c.get("kind") == "float" else 0.5
            return abs(float(a) - float(b)) < tol
        except (TypeError, ValueError):
            return False
    return a == b


class ControlRow:
    """One Control: label | editor | current | modified marker | apply."""

    def __init__(self, page: "OverclockPage", c: dict, grid: QGridLayout, row: int):
        self.page = page
        self.c = c
        self.kind = c.get("kind")
        self._sync = False
        self.label = QLabel()
        self.label.setTextFormat(Qt.RichText)
        self.label.setMinimumWidth(230)
        self.label.setToolTip(c.get("hint") or "")
        self.hint = QLabel(c.get("hint") or "")
        self.hint.setObjectName("dim")
        self.hint.setWordWrap(True)
        f = self.hint.font()
        f.setItalic(True)
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1))
        self.hint.setFont(f)
        lab = QWidget()
        lv = QVBoxLayout(lab)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)
        lv.addWidget(self.label)
        lv.addWidget(self.hint)

        self.editor = QWidget()
        h = QHBoxLayout(self.editor)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        self.slider = self.spin = self.combo = self.check = self.button = self.curve = None
        if self.kind in ("int", "float"):
            lo, hi, step = c.get("min"), c.get("max"), c.get("step") or (1 if self.kind == "int" else 0.01)
            self.step = float(step)
            if self.kind == "int":
                self.spin = QSpinBox()
                self.spin.setRange(int(lo) if lo is not None else -2**31, int(hi) if hi is not None else 2**31 - 1)
                self.spin.setSingleStep(max(1, int(step)))
            else:
                self.spin = QDoubleSpinBox()
                self.spin.setDecimals(_decimals(step))
                self.spin.setRange(lo if lo is not None else -1e9, hi if hi is not None else 1e9)
                self.spin.setSingleStep(step)
            self.spin.setAlignment(Qt.AlignRight)
            self.spin.setFont(mono_font())
            if c.get("unit"):
                self.spin.setSuffix(" " + c["unit"])
            self.spin.setMinimumWidth(QFontMetrics(mono_font()).horizontalAdvance("-8888.888 counts") + 30)
            self.spin.setKeyboardTracking(False)
            if lo is not None and hi is not None:
                self.slider = QSlider(Qt.Horizontal)
                self.slider.setRange(0, max(1, int(round((hi - lo) / self.step))))
                self.slider.setMinimumWidth(160)
                self.slider.valueChanged.connect(self._slider_moved)
                h.addWidget(self.slider, 1)
            self.spin.valueChanged.connect(self._spin_changed)
            h.addWidget(self.spin)
        elif self.kind == "choice":
            self.combo = QComboBox()
            for ch in c.get("choices") or []:
                self.combo.addItem(str(ch), ch)
            self.combo.currentIndexChanged.connect(lambda _: self._edited())
            self.combo.setMinimumWidth(200)
            h.addWidget(self.combo)
            h.addStretch(1)
        elif self.kind == "bool":
            self.check = QCheckBox("Enabled")
            self.check.toggled.connect(lambda _: self._edited())
            h.addWidget(self.check)
            h.addStretch(1)
        elif self.kind == "action":
            self.button = QPushButton(c.get("label") or "Run")
            self.button.clicked.connect(lambda: self.page.apply_rows([self]))
            h.addWidget(self.button)
            h.addStretch(1)
        elif self.kind == "curve":
            self.curve = CurveEditor()
            self.curve.edited.connect(self._edited)
            h.addWidget(self.curve, 1)

        self.current = QLabel()
        self.current.setFont(mono_font())
        self.current.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.current.setMinimumWidth(QFontMetrics(mono_font()).horizontalAdvance("balance_performance") + 8)
        self.current.setToolTip("Current value read back from the hardware")
        self.mark = QLabel()
        self.mark.setFixedWidth(14)
        self.mark.setToolTip("Modified, not applied yet")
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.clicked.connect(lambda: self.page.apply_rows([self]))
        if self.kind == "action":
            self.apply_btn.setVisible(False)

        if self.kind == "curve":
            # the editor takes the whole width under its label row
            grid.addWidget(lab, row, 0, Qt.AlignTop)
            side = QWidget()
            sv = QVBoxLayout(side)
            sv.setContentsMargins(0, 0, 0, 0)
            sv.addWidget(self.current)
            sv.addWidget(self.mark)
            sv.addWidget(self.apply_btn)
            sv.addStretch(1)
            grid.addWidget(self.editor, row, 1, 1, 3)
            grid.addWidget(side, row, 4, Qt.AlignTop)
        else:
            grid.addWidget(lab, row, 0, Qt.AlignVCenter)
            grid.addWidget(self.editor, row, 1)
            grid.addWidget(self.current, row, 2)
            grid.addWidget(self.mark, row, 3)
            grid.addWidget(self.apply_btn, row, 4)
        self.update_control(c, force=True)

    # helpers -----------------------------------------------------------
    @property
    def id(self) -> str:
        return self.c["id"]

    @property
    def danger(self) -> int:
        return int(self.c.get("danger") or 0)

    def _render_label(self):
        t = theme()
        name = self.c.get("label") or self.id
        d = self.danger
        if d >= 2:
            badge = f"<span style='color:{t.red.name()}; font-weight:bold'>⚠ </span>"
            name = f"<span style='color:{t.red.name()}'>{name}</span>"
        elif d == 1:
            badge = f"<span style='color:{t.amber.name()}'>⚠ </span>"
            name = f"<span style='color:{t.amber.name()}'>{name}</span>"
        else:
            badge = "<span style='color:transparent'>⚠ </span>"
        self.label.setText(badge + name)

    def _slider_moved(self, pos: int):
        if self._sync:
            return
        lo = self.c.get("min") or 0
        self._sync = True
        self.spin.setValue(lo + pos * self.step)
        self._sync = False
        self._edited()

    def _spin_changed(self, v):
        if self._sync:
            return
        if self.slider is not None:
            self._sync = True
            self.slider.setValue(int(round((v - (self.c.get("min") or 0)) / self.step)))
            self._sync = False
        self._edited()

    def value(self) -> Any:
        if self.spin is not None:
            v = self.spin.value()
            if self.kind == "int":
                return int(v)
            return round(float(v), _decimals(self.step) + 1)
        if self.combo is not None:
            return self.combo.currentData()
        if self.check is not None:
            return self.check.isChecked()
        if self.curve is not None:
            return self.curve.value()
        return True

    def set_value(self, v: Any) -> None:
        if v is None:
            return
        self._sync = True
        try:
            if self.curve is not None:
                if isinstance(v, dict) and "points" in v and "mode" in v:
                    self.curve.load({**self.c, "value": v})     # a full control value: reload the plot
                else:
                    self.curve.set_value(v)                       # an edit / profile value
            elif self.spin is not None:
                self.spin.setValue(float(v) if self.kind == "float" else int(round(float(v))))
                if self.slider is not None:
                    self.slider.setValue(int(round((float(v) - (self.c.get("min") or 0)) / self.step)))
            elif self.combo is not None:
                i = self.combo.findData(v)
                if i >= 0:
                    self.combo.setCurrentIndex(i)
            elif self.check is not None:
                self.check.setChecked(bool(v))
        finally:
            self._sync = False
        self._edited()

    def dirty(self) -> bool:
        if self.kind == "action" or not self.c.get("available", True):
            return False
        if self.curve is not None:
            return self.curve.dirty()
        if self.c.get("value") is None and not getattr(self, "_touched", False):
            return False
        return not values_equal(self.c, self.value(), self.c.get("value"))

    def _edited(self):
        if self._sync:
            return
        self._touched = True
        d = self.dirty()
        self.mark.setText("●" if d else "")
        self.mark.setStyleSheet(f"color:{theme().accent.name()}")
        self.apply_btn.setEnabled(d and self.c.get("available", True))
        self.page.on_row_edited()

    def update_control(self, c: dict, force: bool = False) -> None:
        was_dirty = (not force) and self.dirty()
        self.c = c
        avail = bool(c.get("available", True))
        self._render_label()
        self.label.setToolTip(c.get("hint") or "")
        self.hint.setText(c.get("hint") or "")
        self.hint.setVisible(not avail and bool(c.get("hint")))
        self.editor.setEnabled(avail)
        self.label.setEnabled(avail)
        if self.kind == "action":
            self.current.setText("")
        else:
            self.current.setText(fmt_control_value(c, c.get("value")))
        if self.curve is not None:
            self._sync = True
            self.curve.load(c)            # keeps the user's edits while touched, follows the card otherwise
            self._sync = False
        elif not was_dirty and self.kind != "action":
            self._sync = True
            self.set_value(c.get("value") if c.get("value") is not None else c.get("default"))
            self._sync = False
        self._edited()


class OverclockPage(QWidget):
    status = Signal(str)

    def __init__(self, store, link, parent=None):
        super().__init__(parent)
        self.store = store
        self.link = link
        self.rows: dict[str, ControlRow] = {}
        self.boxes: dict[str, QFrame] = {}
        self.infos: dict[str, QLabel] = {}
        self.signature: tuple = ()
        self.confirm: Callable[[str, str], bool] = self._confirm_dialog
        self.ask_name: Callable[[str], Optional[str]] = self._ask_name

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        bar = QHBoxLayout()
        bar.setSpacing(6)
        bar.addWidget(QLabel("Profile"))
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(180)
        bar.addWidget(self.profile_combo)
        self.btn_load = QPushButton("Load")
        self.btn_load.setToolTip("Fill the form with the profile's values (does not apply)")
        self.btn_load.clicked.connect(lambda: self.load_profile(self.profile_combo.currentText()))
        self.btn_apply_profile = QPushButton("Apply profile")
        self.btn_apply_profile.clicked.connect(lambda: self.apply_profile(self.profile_combo.currentText()))
        self.btn_save = QPushButton("Save as…")
        self.btn_save.clicked.connect(lambda: self.save_profile())
        self.btn_delete = QPushButton("Delete")
        self.btn_delete.clicked.connect(self.delete_profile)
        for b in (self.btn_load, self.btn_apply_profile, self.btn_save, self.btn_delete):
            bar.addWidget(b)
        bar.addStretch(1)
        self.btn_revert = QPushButton("Revert edits")
        self.btn_revert.clicked.connect(self.revert_all)
        self.btn_apply_all = QPushButton("Apply all changes")
        self.btn_apply_all.setObjectName("primary")
        self.btn_apply_all.clicked.connect(self.apply_all)
        bar.addWidget(self.btn_revert)
        bar.addWidget(self.btn_apply_all)
        lay.addLayout(bar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.vbox = QVBoxLayout(self.container)
        self.vbox.setContentsMargins(0, 0, 6, 0)
        self.vbox.setSpacing(10)
        self.empty = QLabel("No controls reported by the daemon.")
        self.empty.setObjectName("dim")
        self.empty.setAlignment(Qt.AlignCenter)
        self.vbox.addWidget(self.empty)
        self.vbox.addStretch(1)
        self.scroll.setWidget(self.container)
        lay.addWidget(self.scroll, 1)

        lay.addWidget(QLabel("Results"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setFixedHeight(QFontMetrics(mono_font()).lineSpacing() * 5 + 14)
        self.log.setFont(mono_font())
        lay.addWidget(self.log)

        store.updated.connect(self.refresh)
        self.refresh_profiles()
        self.on_row_edited()

    # building --------------------------------------------------------
    def refresh(self):
        ctrls = self.store.controls
        sig = tuple((c.get("id"), c.get("kind"), c.get("group"), tuple(c.get("choices") or ()),
                     c.get("min"), c.get("max"), c.get("step")) for c in ctrls)
        if sig != self.signature:
            self.signature = sig
            self._rebuild(ctrls)
        else:
            by_dev: dict[str, list[dict]] = {}
            for c in ctrls:
                r = self.rows.get(c["id"])
                if r is not None:
                    r.update_control(c)
                by_dev.setdefault((c.get("group") or "Other").split("/")[0], []).append(c)
            for dev, cs in by_dev.items():
                if dev in self.infos:
                    self.infos[dev].setText(self._device_info(cs))

    def _rebuild(self, ctrls: list[dict]):
        pending = {cid: r.value() for cid, r in self.rows.items() if r.dirty()}
        for box in self.boxes.values():
            box.setParent(None)
            box.deleteLater()
        self.rows.clear()
        self.boxes.clear()
        self.infos.clear()
        by_dev: dict[str, list[dict]] = {}
        for c in ctrls:
            by_dev.setdefault((c.get("group") or "Other").split("/")[0], []).append(c)
        self.empty.setVisible(not by_dev)
        for i, (dev, cs) in enumerate(by_dev.items()):
            box = QFrame()
            box.setObjectName("card")
            outer = QVBoxLayout(box)
            outer.setContentsMargins(14, 10, 14, 12)
            outer.setSpacing(8)
            head = QHBoxLayout()
            head.setSpacing(8)
            kind = "cpu" if dev.lower().startswith("cpu") else "gpu" if dev.lower().startswith("gpu") else ""
            badge = QLabel()
            badge.setPixmap(device_badge(dev, kind).pixmap(14, 14))
            head.addWidget(badge)
            title = QLabel(dev)
            title.setObjectName("cardTitle")
            head.addWidget(title)
            info = QLabel(self._device_info(cs))
            info.setObjectName("dim")
            self.infos[dev] = info
            head.addSpacing(6)
            head.addWidget(info)
            head.addStretch(1)
            rb = QPushButton("Reset to stock")
            rb.setToolTip("Set every control of this device to its stock (default) value and apply")
            rb.clicked.connect(lambda _=False, d=dev: self.reset_device(d))
            head.addWidget(rb)
            outer.addLayout(head)
            line = QFrame()
            line.setFrameShape(QFrame.HLine)
            line.setStyleSheet(f"color:{theme().border.name()}")
            outer.addWidget(line)
            grid = QGridLayout()
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(6)
            grid.setColumnStretch(1, 1)
            for r, c in enumerate(cs):
                row = ControlRow(self, c, grid, r)
                self.rows[c["id"]] = row
                if c["id"] in pending:
                    row.set_value(pending[c["id"]])
            outer.addLayout(grid)
            box.setProperty("device", dev)
            self.boxes[dev] = box
            self.vbox.insertWidget(self.vbox.count() - 1, box)
        self.on_row_edited()

    @staticmethod
    def _device_info(cs: list[dict]) -> str:
        n_danger = sum(1 for c in cs if (c.get("danger") or 0) >= 2)
        n_na = sum(1 for c in cs if not c.get("available", True))
        bits = [f"{len(cs)} controls"]
        if n_danger:
            bits.append(f"{n_danger} dangerous")
        if n_na:
            bits.append(f"{n_na} unavailable")
        return " · ".join(bits)

    def on_row_edited(self):
        n = sum(1 for r in self.rows.values() if r.dirty())
        self.btn_apply_all.setEnabled(n > 0)
        self.btn_revert.setEnabled(n > 0)
        self.btn_apply_all.setText(f"Apply all changes ({n})" if n else "Apply all changes")

    # applying --------------------------------------------------------
    def _confirm_dialog(self, title: str, text: str) -> bool:
        mb = QMessageBox(QMessageBox.Warning, title, text, QMessageBox.Yes | QMessageBox.Cancel, self)
        mb.setDefaultButton(QMessageBox.Cancel)
        return mb.exec() == QMessageBox.Yes

    def _log(self, ok: Optional[bool], text: str):
        t = theme()
        fmt = QTextCharFormat()
        fmt.setForeground(t.text if ok is None else (t.green if ok else t.red))
        cur = self.log.textCursor()
        cur.movePosition(QTextCursor.End)
        tag = "    " if ok is None else (" OK " if ok else "FAIL")
        cur.insertText(f"{time.strftime('%H:%M:%S')} {tag}  {text}\n", fmt)
        self.log.setTextCursor(cur)
        self.log.ensureCursorVisible()
        self.status.emit(text)

    def apply_values(self, items: list[tuple[dict, Any]], what: str = "") -> bool:
        """Apply (control, value) pairs in order. Danger-2 items need one confirmation."""
        items = [(c, v) for c, v in items if c.get("available", True)]
        if not items:
            return False
        risky = [c for c, _ in items if (c.get("danger") or 0) >= 2]
        if risky:
            lines = "\n".join(f"  • {c.get('label')} → {fmt_control_value(c, v)}"
                              for c, v in items if (c.get('danger') or 0) >= 2)
            if not self.confirm("Dangerous setting",
                                f"You are about to apply settings marked DANGEROUS:\n\n{lines}\n\n"
                                "These can damage hardware or cause instability. Continue?"):
                self._log(None, f"cancelled {what or 'apply'}")
                return False
        for c, v in items:
            cid = c["id"]
            label = c.get("label") or cid
            shown = "run" if c.get("kind") == "action" else fmt_control_value(c, v)

            def done(resp, label=label, shown=shown):
                ok = bool(resp.get("ok"))
                msg = resp.get("msg") or ("applied" if ok else "failed")
                self._log(ok, f"{label} = {shown}: {msg}")
            self.link.request({"cmd": "apply", "id": cid, "value": v}, done)
        return True

    def apply_rows(self, rows: list[ControlRow]) -> bool:
        return self.apply_values([(r.c, r.value()) for r in rows])

    def apply_all(self) -> bool:
        return self.apply_rows([r for r in self.rows.values() if r.dirty()])

    def revert_all(self):
        for r in self.rows.values():
            if r.dirty():
                r.set_value(r.c.get("value"))

    def reset_device(self, dev: str) -> bool:
        rows = [r for r in self.rows.values()
                if (r.c.get("group") or "").split("/")[0] == dev and r.kind != "action"
                and r.c.get("available", True) and r.c.get("default") is not None]
        changes = [(r.c, r.c["default"]) for r in rows if not values_equal(r.c, r.c.get("value"), r.c["default"])]
        for r in rows:
            r.set_value(r.c["default"])
        if not changes:
            self._log(None, f"{dev}: already at stock")
            return False
        if not any((c.get("danger") or 0) >= 2 for c, _ in changes):
            lines = "\n".join(f"  • {c.get('label')} → {fmt_control_value(c, v)}" for c, v in changes)
            if not self.confirm("Reset to stock", f"Reset {len(changes)} settings of {dev} to stock?\n\n{lines}"):
                return False
        return self.apply_values(changes, f"reset {dev}")

    # profiles --------------------------------------------------------
    def refresh_profiles(self, select: str = ""):
        d = profiles_dir()
        names = sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []
        cur = select or self.profile_combo.currentText()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(names)
        if cur in names:
            self.profile_combo.setCurrentText(cur)
        self.profile_combo.blockSignals(False)
        has = bool(names)
        for b in (self.btn_load, self.btn_apply_profile, self.btn_delete):
            b.setEnabled(has)

    def _ask_name(self, default: str) -> Optional[str]:
        name, ok = QInputDialog.getText(self, "Save profile", "Profile name:", text=default)
        return name if ok else None

    @staticmethod
    def safe_name(name: str) -> str:
        return re.sub(r"[^A-Za-z0-9 _.-]+", "_", name).strip(" .")[:64]

    def save_profile(self, name: Optional[str] = None) -> Optional[Path]:
        if name is None:
            name = self.ask_name(self.profile_combo.currentText() or "my-oc")
        if not name:
            return None
        name = self.safe_name(name)
        if not name:
            return None
        data = {cid: r.value() for cid, r in self.rows.items()
                if r.kind != "action" and r.c.get("available", True)}
        d = profiles_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{name}.json"
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.refresh_profiles(select=name)
        self._log(True, f"saved profile '{name}' ({len(data)} controls) to {path}")
        return path

    def read_profile(self, name: str) -> dict:
        path = profiles_dir() / f"{self.safe_name(name)}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("profile must be a JSON object")
            return data
        except (OSError, ValueError) as e:
            self._log(False, f"cannot read profile '{name}': {e}")
            return {}

    def load_profile(self, name: str) -> int:
        """Fill editors from a profile; returns number of rows changed (not applied)."""
        data = self.read_profile(name)
        n = 0
        missing = []
        for cid, v in data.items():
            r = self.rows.get(cid)
            if r is None or r.kind == "action":
                missing.append(cid)
                continue
            if not r.c.get("available", True):
                continue
            r.set_value(v)
            n += r.dirty()
        if data:
            extra = f"; {len(missing)} unknown controls ignored" if missing else ""
            self._log(None, f"loaded profile '{name}': {n} changes pending{extra}")
        return n

    def apply_profile(self, name: str) -> bool:
        if not name:
            return False
        self.load_profile(name)
        rows = [r for r in self.rows.values() if r.dirty()]
        if not rows:
            self._log(None, f"profile '{name}': nothing to change")
            return False
        return self.apply_rows(rows)

    def delete_profile(self):
        name = self.profile_combo.currentText()
        if not name:
            return
        if not self.confirm("Delete profile", f"Delete profile '{name}'?"):
            return
        try:
            (profiles_dir() / f"{name}.json").unlink()
            self._log(None, f"deleted profile '{name}'")
        except OSError as e:
            self._log(False, f"cannot delete '{name}': {e}")
        self.refresh_profiles()
