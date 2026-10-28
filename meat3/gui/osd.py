"""OSD tab (item selection, appearance, MangoHud generator) and the desktop overlay window."""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QGuiApplication, QIcon, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog,
                               QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSlider, QSpinBox,
                               QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTreeView, QVBoxLayout,
                               QWidget)

from meat3.core.fmt import format_value, osd_label
from meat3.gui.common import (config_dir, mono_font, setting_bool, settings, severity,
                              severity_color, theme)
from meat3.gui.sensors import SENSOR_ID_ROLE, SensorFilterProxy
from meat3.osd import mangohud

CORNERS = ["top-left", "top-right", "bottom-left", "bottom-right"]


class OsdStyle:
    """Client-side appearance settings (the daemon only stores enabled + items)."""

    def __init__(self):
        s = settings()
        self.corner = str(s.value("osd/corner", "top-left"))
        if self.corner not in CORNERS:
            self.corner = "top-left"
        self.font_size = int(s.value("osd/font_size", 13))
        self.opacity = int(s.value("osd/opacity", 55))          # background alpha, %
        self.text_color = str(s.value("osd/text_color", "#ffffff"))
        self.label_color = str(s.value("osd/label_color", "#aeb6c4"))
        self.bg_color = str(s.value("osd/bg_color", "#0b0c0f"))
        self.rate_ms = int(s.value("osd/rate_ms", 1000))
        self.desktop = setting_bool(s, "osd/desktop", True)
        self.margin = 18

    def save(self):
        s = settings()
        for k in ("corner", "font_size", "opacity", "text_color", "label_color", "bg_color", "rate_ms", "desktop"):
            s.setValue(f"osd/{k}", getattr(self, k))


class OverlayWindow(QWidget):
    """Frameless, translucent, always-on-top, click-through desktop overlay."""

    def __init__(self, store, style: OsdStyle):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setWindowTitle("meat³ OSD")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.store = store
        self.style_ = style
        self.items: list[str] = []
        self.rows: list[tuple[str, str, Optional[QColor]]] = []
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.apply_style()

    def apply_style(self):
        self.timer.start(max(100, self.style_.rate_ms))
        self.font_ = QFont()
        self.font_.setPointSizeF(self.style_.font_size)
        self.font_.setBold(True)
        self.vfont = QFont(mono_font())
        self.vfont.setPointSizeF(self.style_.font_size)
        self.vfont.setBold(True)
        self.refresh()

    def set_items(self, items: list[str]):
        if items != self.items:
            self.items = list(items)
            self.refresh()

    def refresh(self):
        rows = []
        for sid in self.items:
            s = self.store.sensors.get(sid)
            if s is None:
                continue
            v = s.get("value")
            rows.append((osd_label(s), format_value(v, s.get("decimals", 0), s.get("unit", "")),
                         severity_color(severity(s, v))))
        self.rows = rows
        self._layout()
        self.update()

    def _layout(self):
        fm, vm = QFontMetricsF(self.font_), QFontMetricsF(self.vfont)
        pad = self.style_.font_size * 0.8
        lw = max([fm.horizontalAdvance(r[0]) for r in self.rows] + [fm.horizontalAdvance("meat³ OSD")])
        vw = max([vm.horizontalAdvance(r[1]) for r in self.rows] + [vm.horizontalAdvance("0000 MHz")])
        self.line_h = max(fm.height(), vm.height()) * 1.12
        self.pad = pad
        self.label_w = lw
        w = int(pad * 2 + lw + pad * 1.6 + vw + 2)
        h = int(pad * 2 + self.line_h * max(1, len(self.rows)) + 1)
        if QSize(w, h) != self.size():
            self.resize(w, h)
        self.reposition()

    def reposition(self):
        scr = QGuiApplication.primaryScreen()
        if scr is None:
            return
        g = scr.availableGeometry()
        m = self.style_.margin
        c = self.style_.corner
        x = g.left() + m if "left" in c else g.right() - self.width() - m + 1
        y = g.top() + m if "top" in c else g.bottom() - self.height() - m + 1
        if self.pos().x() != x or self.pos().y() != y:
            self.move(x, y)

    def _text(self, p: QPainter, font: QFont, x: float, y: float, text: str, color: QColor):
        path = QPainterPath()
        path.addText(QPointF(x, y), font, text)
        p.setPen(QPen(QColor(0, 0, 0, 220), max(2.0, self.style_.font_size / 5), Qt.SolidLine,
                      Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)                                   # outline
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawPath(path)                                   # fill

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.transparent)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        bg = QColor(self.style_.bg_color)
        bg.setAlpha(int(255 * self.style_.opacity / 100))
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
        fm, vm = QFontMetricsF(self.font_), QFontMetricsF(self.vfont)
        y = self.pad
        lab_c, txt_c = QColor(self.style_.label_color), QColor(self.style_.text_color)
        if not self.rows:
            self._text(p, self.font_, self.pad, y + fm.ascent(), "meat³ OSD: no items", lab_c)
            return
        for label, value, col in self.rows:
            base = y + (self.line_h + fm.ascent() - fm.descent()) / 2
            self._text(p, self.font_, self.pad, base, label, lab_c)
            vx = self.width() - self.pad - vm.horizontalAdvance(value)
            self._text(p, self.vfont, vx, base, value, col or txt_c)
            y += self.line_h


VALUE_ROLE = Qt.UserRole + 1
COLOR_ROLE = Qt.UserRole + 2


class OsdListDelegate(QStyledItemDelegate):
    """Label left, live value right (monospace, severity-coloured), like the overlay."""

    def paint(self, p: QPainter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        value = index.data(VALUE_ROLE) or ""
        opt.text = ""
        opt.widget.style().drawControl(QStyle.CE_ItemViewItem, opt, p, opt.widget)
        r = option.rect.adjusted(8, 0, -10, 0)
        p.save()
        t = theme()
        p.setPen(t.text)
        n = index.row() + 1
        p.setPen(t.dim)
        p.drawText(r, Qt.AlignLeft | Qt.AlignVCenter, f"{n:>2}")
        p.setPen(t.text)
        p.drawText(r.adjusted(26, 0, -120, 0), Qt.AlignLeft | Qt.AlignVCenter,
                   p.fontMetrics().elidedText(index.data(Qt.DisplayRole) or "", Qt.ElideRight, r.width() - 146))
        p.setFont(mono_font())
        p.setPen(index.data(COLOR_ROLE) or t.text)
        p.drawText(r, Qt.AlignRight | Qt.AlignVCenter, value)
        p.restore()

    def sizeHint(self, option, index):
        sz = super().sizeHint(option, index)
        return QSize(sz.width(), max(sz.height(), option.fontMetrics.height() + 10))


def _swatch(color: str) -> QIcon:
    pm = QPixmap(28, 28)
    pm.setDevicePixelRatio(2.0)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(theme().border, 1))
    p.setBrush(QColor(color))
    p.drawRoundedRect(QRectF(0.5, 0.5, 13, 13), 3, 3)
    p.end()
    return QIcon(pm)


class OsdPage(QWidget):
    status = Signal(str)
    style_changed = Signal()

    def __init__(self, store, link, tree_model, overlay: OverlayWindow, style: OsdStyle, parent=None):
        super().__init__(parent)
        self.store = store
        self.link = link
        self.overlay = overlay
        self.style_ = style
        self.items: list[str] = []
        self._inflight = 0
        st = settings()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(10)

        # --- status row
        top = QHBoxLayout()
        self.chk_enabled = QCheckBox("OSD enabled")
        self.chk_enabled.setToolTip("Daemon-side OSD state (also flipped by the hotkey)")
        self.chk_enabled.clicked.connect(self._set_enabled)
        top.addWidget(self.chk_enabled)
        top.addSpacing(16)
        self.hotkey_lbl = QLabel("Hotkey: —")
        top.addWidget(self.hotkey_lbl)
        self.btn_hotkey = QPushButton("Change…")
        self.btn_hotkey.setToolTip("Pick a new OSD hotkey. It also becomes MangoHud's in-game toggle.")
        self.btn_hotkey.clicked.connect(self.change_hotkey)
        top.addWidget(self.btn_hotkey)
        top.addStretch(1)
        self.items_lbl = QLabel()
        self.items_lbl.setObjectName("dim")
        top.addWidget(self.items_lbl)
        lay.addLayout(top)

        # --- selection row
        sel = QHBoxLayout()
        sel.setSpacing(8)
        left = QVBoxLayout()
        left.addWidget(QLabel("<b>Available sensors</b>"))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter…")
        self.filter.setClearButtonEnabled(True)
        left.addWidget(self.filter)
        self.proxy = SensorFilterProxy(self)
        self.proxy.setSourceModel(tree_model)
        self.avail = QTreeView()
        self.avail.setModel(self.proxy)
        self.avail.setAlternatingRowColors(True)
        self.avail.setUniformRowHeights(True)
        self.avail.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.avail.setIconSize(QSize(30, 14))
        self.avail.setIndentation(14)
        for c in (2, 3, 4):
            self.avail.setColumnHidden(c, True)
        h = self.avail.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        h.setSectionResizeMode(1, QHeaderView.Interactive)
        h.resizeSection(1, 110)
        self.avail.doubleClicked.connect(lambda _: self.add_selected())
        self.filter.textChanged.connect(lambda t: (self.proxy.set_text(t), self.avail.expandAll()))
        tree_model.rowsInserted.connect(lambda *a: QTimer.singleShot(0, self.avail.expandAll))
        left.addWidget(self.avail, 1)
        sel.addLayout(left, 5)

        mid = QVBoxLayout()
        mid.addStretch(1)
        self.btn_add = QPushButton("Add  →")
        self.btn_add.clicked.connect(self.add_selected)
        self.btn_rm = QPushButton("←  Remove")
        self.btn_rm.clicked.connect(self.remove_selected)
        self.btn_up = QPushButton("Move up")
        self.btn_up.clicked.connect(lambda: self.move_selected(-1))
        self.btn_down = QPushButton("Move down")
        self.btn_down.clicked.connect(lambda: self.move_selected(1))
        for b in (self.btn_add, self.btn_rm, self.btn_up, self.btn_down):
            b.setMinimumWidth(110)
            mid.addWidget(b)
        mid.addStretch(2)
        sel.addLayout(mid)

        right = QVBoxLayout()
        right.addWidget(QLabel("<b>On-screen items</b>  <span style='color:%s'>(drag to reorder)</span>"
                               % theme().dim.name()))
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setAlternatingRowColors(True)
        self.list.setItemDelegate(OsdListDelegate(self.list))
        self.list.model().rowsMoved.connect(lambda *a: QTimer.singleShot(0, self._push_items_from_list))
        self.list.itemDoubleClicked.connect(lambda _: self.remove_selected())
        right.addWidget(self.list, 1)
        sel.addLayout(right, 4)
        lay.addLayout(sel, 1)

        # --- settings row
        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        ap = QGroupBox("Desktop overlay")
        f = QFormLayout(ap)
        f.setLabelAlignment(Qt.AlignRight)
        f.setHorizontalSpacing(10)
        self.chk_desktop = QCheckBox("Show on the desktop when the OSD is enabled")
        self.chk_desktop.setChecked(style.desktop)
        self.chk_desktop.toggled.connect(self._style_edit)
        f.addRow(self.chk_desktop)
        self.corner = QComboBox()
        self.corner.addItems(CORNERS)
        self.corner.setCurrentText(style.corner)
        self.corner.currentTextChanged.connect(self._style_edit)
        f.addRow("Position", self.corner)
        self.font_size = QSpinBox()
        self.font_size.setRange(7, 48)
        self.font_size.setSuffix(" pt")
        self.font_size.setValue(style.font_size)
        self.font_size.valueChanged.connect(self._style_edit)
        f.addRow("Font size", self.font_size)
        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(0, 100)
        self.opacity.setValue(style.opacity)
        self.opacity.valueChanged.connect(self._style_edit)
        self.opacity_lbl = QLabel(f"{style.opacity}%")
        self.opacity_lbl.setMinimumWidth(40)
        orow = QHBoxLayout()
        orow.addWidget(self.opacity, 1)
        orow.addWidget(self.opacity_lbl)
        f.addRow("Background opacity", orow)
        crow = QHBoxLayout()
        self.color_btns = {}
        for key, name in (("text_color", "Values"), ("label_color", "Labels"), ("bg_color", "Background")):
            b = QPushButton(name)
            b.setIcon(_swatch(getattr(style, key)))
            b.clicked.connect(lambda _=False, k=key: self._pick_color(k))
            self.color_btns[key] = b
            crow.addWidget(b)
        crow.addStretch(1)
        f.addRow("Colours", crow)
        self.rate = QSpinBox()
        self.rate.setRange(100, 5000)
        self.rate.setSingleStep(100)
        self.rate.setSuffix(" ms")
        self.rate.setValue(style.rate_ms)
        self.rate.valueChanged.connect(self._style_edit)
        f.addRow("Update interval", self.rate)
        bottom.addWidget(ap, 1)

        mh = QGroupBox("MangoHud (in-game)")
        g = QGridLayout(mh)
        g.setHorizontalSpacing(8)
        g.addWidget(QLabel("Config file"), 0, 0, Qt.AlignRight)
        self.mh_path = QLineEdit(str(st.value("mangohud/path", str(config_dir() / "MangoHud.conf"))))
        g.addWidget(self.mh_path, 0, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_mh)
        g.addWidget(browse, 0, 2)
        g.addWidget(QLabel("osd.txt path"), 1, 0, Qt.AlignRight)
        self.mh_osd = QLineEdit(str(st.value("mangohud/osd_file", "")))
        self.mh_osd.setPlaceholderText(f"from daemon (default {mangohud.DEFAULT_OSD_FILE})")
        g.addWidget(self.mh_osd, 1, 1, 1, 2)
        g.addWidget(QLabel("Hotkey"), 2, 0, Qt.AlignRight)
        self.mh_owner = QComboBox()
        self.mh_owner.addItem("MangoHud toggles itself (toggle_hud = our hotkey)", "mangohud")
        self.mh_owner.addItem("Daemon toggles MangoHud via mangohudctl", "daemon")
        i = self.mh_owner.findData(st.value("mangohud/owner", "mangohud"))
        self.mh_owner.setCurrentIndex(max(0, i))
        g.addWidget(self.mh_owner, 2, 1, 1, 2)
        g.addWidget(QLabel("In-game size"), 3, 0, Qt.AlignRight)
        self.mh_size = QSpinBox()
        self.mh_size.setRange(12, 64)
        self.mh_size.setSuffix(" px")
        self.mh_size.setValue(int(st.value("mangohud/font_size", 28)))
        self.mh_size.setToolTip("MangoHud font size in game. Changing it rewrites the config; MangoHud reloads it live.")
        self.mh_size.valueChanged.connect(lambda _: self.generate_mangohud(quiet=True))
        g.addWidget(self.mh_size, 3, 1)
        opts = QHBoxLayout()
        self.mh_fps = QCheckBox("Include MangoHud FPS")
        self.mh_fps.setChecked(setting_bool(st, "mangohud/fps", True))
        self.mh_global = QCheckBox("In every game")
        self.mh_global.setToolTip("Load MangoHud into every Vulkan game (all Proton games) without launch options.\n"
                                  "It starts hidden: press the OSD hotkey in game. Takes effect after you log out and back in.")
        self.mh_global.setChecked(mangohud.global_enabled())
        self.mh_global.toggled.connect(self._set_global)
        self.mh_auto = QCheckBox("Regenerate when items change")
        self.mh_auto.setChecked(setting_bool(st, "mangohud/auto", False))
        opts.setSpacing(16)
        opts.addWidget(self.mh_fps)
        opts.addWidget(self.mh_global)
        opts.addWidget(self.mh_auto)
        opts.addStretch(1)
        self.btn_gen = QPushButton("Generate config")
        self.btn_gen.setObjectName("primary")
        self.btn_gen.clicked.connect(lambda: self.generate_mangohud())
        opts.addWidget(self.btn_gen)
        g.addLayout(opts, 4, 0, 1, 3)
        self.mh_usage = QLabel()
        self.mh_usage.setObjectName("dim")
        self.mh_usage.setWordWrap(True)
        self.mh_usage.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.mh_usage.setFont(mono_font(-1))
        g.addWidget(self.mh_usage, 5, 0, 1, 3)
        g.setColumnStretch(1, 1)
        self._update_usage()
        self.mh_path.textChanged.connect(lambda _: self._update_usage())
        bottom.addWidget(mh, 1)
        lay.addLayout(bottom)

        link.osd_state.connect(self.on_osd_state)
        store.updated.connect(self._refresh_list_values)

    # daemon state ----------------------------------------------------
    def on_osd_state(self, osd: dict):
        en = bool(osd.get("enabled"))
        self.chk_enabled.setChecked(en)
        hk = osd.get("hotkey") or "—"
        ks = mangohud.evdev_to_keysym(hk) if osd.get("hotkey") else None
        self.hotkey_lbl.setText(f"Hotkey: <b>{pretty_hotkey(hk)}</b>"
                                + (f"  <span style='color:{theme().dim.name()}'>(MangoHud: {ks})</span>" if ks else ""))
        items = [i for i in osd.get("items") or [] if isinstance(i, str)]
        if self._inflight == 0 and items != self.items:
            self.items = items
            self._fill_list()
        self.overlay.set_items(self.items)
        self.items_lbl.setText(f"{len(self.items)} items · {'shown' if en else 'hidden'}")

    def _set_enabled(self, on: bool):
        self.link.request({"cmd": "osd_set", "enabled": bool(on)},
                          lambda r: self.status.emit(("OSD enabled" if on else "OSD disabled")
                                                     if r.get("ok") else f"osd_set failed: {r.get('msg')}"))

    # item list -------------------------------------------------------
    def _fill_list(self):
        self.list.blockSignals(True)
        self.list.clear()
        for sid in self.items:
            it = QListWidgetItem()
            it.setData(Qt.UserRole, sid)
            it.setToolTip(sid)
            self.list.addItem(it)
        self.list.blockSignals(False)
        self._refresh_list_values()

    def _refresh_list_values(self):
        for i in range(self.list.count()):
            it = self.list.item(i)
            sid = it.data(Qt.UserRole)
            s = self.store.sensors.get(sid)
            if s is None:
                it.setText(sid)
                it.setData(VALUE_ROLE, "not reported")
                it.setData(COLOR_ROLE, theme().dim)
            else:
                v = s.get("value")
                it.setText(osd_label(s))
                it.setData(VALUE_ROLE, format_value(v, s.get("decimals", 0), s.get("unit", "")))
                it.setData(COLOR_ROLE, severity_color(severity(s, v)))

    def _push_items_from_list(self):
        self.set_items([self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())])

    def set_items(self, items: list[str]):
        items = list(dict.fromkeys(items))
        self.items = items
        self._fill_list()
        self.overlay.set_items(items)
        self._inflight += 1

        def done(r):
            self._inflight -= 1
            if not r.get("ok"):
                self.status.emit(f"osd_set failed: {r.get('msg')}")
        self.link.request({"cmd": "osd_set", "items": items}, done)
        if self.mh_auto.isChecked():
            self.generate_mangohud(quiet=True)
        self.items_changed()

    def items_changed(self):
        pass   # hook for the main window (tree decorations)

    def add_ids(self, ids: list[str]):
        self.set_items(self.items + [i for i in ids if i not in self.items])

    def remove_ids(self, ids: list[str]):
        self.set_items([i for i in self.items if i not in ids])

    def add_selected(self):
        ids = []
        for idx in self.avail.selectionModel().selectedRows(0):
            sid = self.proxy.data(idx, SENSOR_ID_ROLE)
            if sid:
                ids.append(sid)
        if ids:
            self.add_ids(ids)

    def remove_selected(self):
        ids = [it.data(Qt.UserRole) for it in self.list.selectedItems()]
        if ids:
            self.remove_ids(ids)

    def move_selected(self, delta: int):
        rows = sorted(self.list.row(it) for it in self.list.selectedItems())
        if not rows:
            return
        items = list(self.items)
        if delta < 0 and rows[0] == 0 or delta > 0 and rows[-1] == len(items) - 1:
            return
        order = rows if delta < 0 else list(reversed(rows))
        for r in order:
            items[r], items[r + delta] = items[r + delta], items[r]
        self.set_items(items)
        for r in rows:
            self.list.item(r + delta).setSelected(True)

    # appearance ------------------------------------------------------
    def _pick_color(self, key: str):
        c = QColorDialog.getColor(QColor(getattr(self.style_, key)), self, "Choose colour")
        if c.isValid():
            setattr(self.style_, key, c.name())
            self.color_btns[key].setIcon(_swatch(c.name()))
            self._style_edit()

    def _style_edit(self, *_):
        s = self.style_
        s.desktop = self.chk_desktop.isChecked()
        s.corner = self.corner.currentText()
        s.font_size = self.font_size.value()
        s.opacity = self.opacity.value()
        s.rate_ms = self.rate.value()
        self.opacity_lbl.setText(f"{s.opacity}%")
        s.save()
        self.overlay.apply_style()
        self.style_changed.emit()
        self._mh_follow()

    def _mh_follow(self):
        """Opacity, colours and corner also apply in game: rewrite the MangoHud config (debounced,
        since a slider drag fires many changes). Only when the config exists or auto-regenerate is on."""
        path = os.path.expanduser(self.mh_path.text().strip() or str(config_dir() / "MangoHud.conf"))
        if not (self.mh_auto.isChecked() or os.path.exists(path)):
            return
        t = getattr(self, "_mh_timer", None)
        if t is None:
            t = self._mh_timer = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(lambda: self.generate_mangohud(quiet=True))
        t.start(400)

    # MangoHud --------------------------------------------------------
    def _browse_mh(self):
        p, _ = QFileDialog.getSaveFileName(self, "MangoHud config", self.mh_path.text(), "Config (*.conf)")
        if p:
            self.mh_path.setText(p)

    def change_hotkey(self):
        dlg = HotkeyDialog(self.link.osd.get("hotkey", ""), self)
        if dlg.exec() != QDialog.Accepted or not dlg.keys:
            return
        keys = dlg.keys

        def done(r):
            if not r.get("ok"):
                self.status.emit(f"Hotkey not changed: {r.get('msg')}")
                return
            self.link.osd = dict(self.link.osd, hotkey="+".join(keys))
            self.on_osd_state(self.link.osd)
            path = os.path.expanduser(self.mh_path.text().strip() or str(config_dir() / "MangoHud.conf"))
            note = ""
            if os.path.exists(path) and self.generate_mangohud(quiet=True):
                note = " MangoHud config updated; restart the game if it doesn't pick it up."
            self.status.emit(f"Hotkey is now {pretty_hotkey('+'.join(keys))}.{note}")
        self.link.request({"cmd": "osd_set", "hotkey": keys}, done)

    def _set_global(self, on: bool):
        try:
            if on:
                path = self.generate_mangohud(quiet=True)
                if not path:
                    raise ValueError("couldn't write the MangoHud config")
                mangohud.set_global(True, path)
                self.status.emit("MangoHud will load in every Vulkan game, hidden until you press the hotkey. "
                                 "Log out and back in for it to take effect.")
            else:
                mangohud.set_global(False, "")
                self.status.emit("MangoHud no longer loads in every game (after you log out and back in). "
                                 "Use the launch option below per game.")
        except (OSError, ValueError) as e:
            self.status.emit(f"Couldn't change it: {e}")
            self.mh_global.blockSignals(True)
            self.mh_global.setChecked(mangohud.global_enabled())
            self.mh_global.blockSignals(False)
        self._update_usage()

    def _update_usage(self):
        path = os.path.expanduser(self.mh_path.text().strip() or str(config_dir() / "MangoHud.conf"))
        if getattr(self, "mh_global", None) is not None and self.mh_global.isChecked():
            self.mh_usage.setText("No launch options needed: MangoHud loads in every Vulkan game.\n"
                                  "OpenGL-only games still need:  mangohud %command%")
            return
        self.mh_usage.setText(f"Steam launch options:\nMANGOHUD_CONFIGFILE={path} mangohud %command%")

    def osd_file(self) -> str:
        return self.mh_osd.text().strip() or self.link.osd.get("file") or mangohud.DEFAULT_OSD_FILE

    def generate_mangohud(self, quiet: bool = False) -> Optional[str]:
        path = os.path.expanduser(self.mh_path.text().strip() or str(config_dir() / "MangoHud.conf"))
        sens = [self.store.sensors.get(i) or {"id": i, "group": "", "label": i} for i in self.items]
        corner = self.style_.corner
        try:
            hexc = lambda c: str(c).lstrip("#").upper()[:6]          # noqa: E731
            text = mangohud.generate(sens, hotkey=self.link.osd.get("hotkey", ""), osd_file=self.osd_file(),
                                     text_color=hexc(self.style_.text_color),
                                     background_color=hexc(self.style_.bg_color),
                                     background_alpha=self.style_.opacity / 100,
                                     position=corner, font_size=self.mh_size.value(),
                                     include_fps=self.mh_fps.isChecked(),
                                     hotkey_owner=self.mh_owner.currentData(),
                                     # follows the OSD on/off state: MangoHud re-applies no_display on every
                                     # reload, so a forced "hidden" would hide the HUD mid-game on each change
                                     start_hidden=not self.link.osd.get("enabled", True))
            mangohud.write_config(path, text)
        except (OSError, ValueError) as e:
            self.status.emit(f"MangoHud config not written: {e}")
            return None
        st = settings()
        st.setValue("mangohud/path", self.mh_path.text().strip())
        st.setValue("mangohud/osd_file", self.mh_osd.text().strip())
        st.setValue("mangohud/owner", self.mh_owner.currentData())
        st.setValue("mangohud/fps", self.mh_fps.isChecked())
        st.setValue("mangohud/font_size", self.mh_size.value())
        st.setValue("mangohud/auto", self.mh_auto.isChecked())
        self._update_usage()
        if not quiet:
            self.status.emit(f"Wrote MangoHud config with {len(sens)} items to {path}")
        return path


def pretty_hotkey(hk: str) -> str:
    """'KEY_RIGHTCTRL+KEY_F12' -> 'Right Ctrl + F12'."""
    names = {"RIGHTCTRL": "Right Ctrl", "LEFTCTRL": "Left Ctrl", "RIGHTSHIFT": "Right Shift",
             "LEFTSHIFT": "Left Shift", "RIGHTALT": "Right Alt", "LEFTALT": "Left Alt",
             "LEFTMETA": "Left Super", "RIGHTMETA": "Right Super"}
    parts = [p.replace("KEY_", "") for p in (hk or "").split("+") if p]
    return " + ".join(names.get(p, p.title() if len(p) > 3 and not p[1:].isdigit() else p) for p in parts) or "—"


def evdev_name(scancode: int) -> Optional[str]:
    """X11/Wayland native scan code (evdev code + 8) -> 'KEY_…', keeping left/right modifiers apart."""
    try:
        from evdev import ecodes
    except ImportError:
        return None
    n = ecodes.KEY.get(scancode - 8)
    if isinstance(n, list):
        n = n[0]
    return n if isinstance(n, str) and n.startswith("KEY_") else None


class HotkeyDialog(QDialog):
    """Captures a key combo the way the daemon will see it (evdev names)."""

    def __init__(self, current: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("OSD hotkey")
        self.keys: list[str] = []
        self.held: list[str] = []
        v = QVBoxLayout(self)
        v.addWidget(QLabel(f"Current: <b>{pretty_hotkey(current)}</b><br><br>"
                           "Hold the modifier keys you want, then press the final key.<br>"
                           "Or type it, e.g. <code>RIGHTCTRL+F12</code>."))
        self.show_lbl = QLabel("<i>press a combo…</i>")
        self.show_lbl.setAlignment(Qt.AlignCenter)
        self.show_lbl.setMinimumHeight(48)
        f = self.show_lbl.font()
        f.setPointSizeF(f.pointSizeF() * 1.5)
        self.show_lbl.setFont(f)
        v.addWidget(self.show_lbl)
        self.typed = QLineEdit()
        self.typed.setPlaceholderText("or type: RIGHTCTRL+F12")
        self.typed.textEdited.connect(self._typed)
        v.addWidget(self.typed)
        self.err = QLabel("")
        self.err.setObjectName("dim")
        v.addWidget(self.err)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        self.ok = bb.button(QDialogButtonBox.Ok)
        self.ok.setEnabled(False)
        for b in bb.buttons():              # keys must reach the dialog, not a focused button
            b.setFocusPolicy(Qt.NoFocus)
            b.setAutoDefault(False)
        self.typed.setFocusPolicy(Qt.ClickFocus)
        v.addWidget(bb)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFocus()

    def _set(self, keys):
        try:
            self.keys = mangohud.parse_hotkey(keys)
            self.show_lbl.setText(f"<b>{pretty_hotkey('+'.join(self.keys))}</b>")
            ks = mangohud.evdev_to_keysym("+".join(self.keys))
            self.err.setText(f"MangoHud will use: {ks}")
            self.ok.setEnabled(True)
        except ValueError as e:
            self.keys = []
            self.err.setText(str(e))
            self.ok.setEnabled(False)

    def _typed(self, text):
        self._set(text)

    def keyPressEvent(self, e):
        if self.typed.hasFocus():
            return super().keyPressEvent(e)
        if e.isAutoRepeat():
            return
        name = evdev_name(e.nativeScanCode())
        if name is None:
            self.err.setText("can't read that key here; type the combo instead")
            return
        if name in mangohud.MODIFIERS:
            if name not in self.held:
                self.held.append(name)
            self.show_lbl.setText(pretty_hotkey("+".join(self.held)) + " + …")
            return
        self._set(self.held + [name])

    def keyReleaseEvent(self, e):
        if self.typed.hasFocus() or e.isAutoRepeat():
            return super().keyReleaseEvent(e)
        name = evdev_name(e.nativeScanCode())
        if name in self.held:
            self.held.remove(name)
