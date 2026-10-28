"""HWiNFO-style Sensors tab: device -> group -> sensor tree updated in place."""
from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import (QAbstractItemModel, QModelIndex, QPointF, QRectF, QSize,
                            QSortFilterProxyModel, Qt, QTimer, Signal)
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QFontMetrics, QGuiApplication, QKeySequence, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMenu, QSizePolicy, QToolBar, QTreeView,
                               QVBoxLayout, QWidget)

from meat3.core.fmt import format_value
from meat3.gui.common import (THROTTLE, SensorStore, device_badge, mono_font,
                              sensor_marker, severity, severity_color, theme)

COLS = ["Sensor", "Current", "Minimum", "Maximum", "Average"]
VALUE_KEYS = [None, "value", "min", "max", "avg"]
SENSOR_ID_ROLE = Qt.UserRole + 1
KIND_ROLE = Qt.UserRole + 2


class Node:
    __slots__ = ("name", "parent", "children", "kind", "sid", "path", "row")

    def __init__(self, name: str, parent: Optional["Node"], kind: str, sid: str = "", path: str = ""):
        self.name = name
        self.parent = parent
        self.children: list[Node] = []
        self.kind = kind          # "root" | "device" | "group" | "sensor"
        self.sid = sid
        self.path = path
        self.row = 0


class SensorTreeModel(QAbstractItemModel):
    """Tree model fed from SensorStore. New ids are inserted; existing rows are updated
    with dataChanged only, so expansion/scroll/selection survive every poll."""

    def __init__(self, store: SensorStore, parent=None):
        super().__init__(parent)
        self.store = store
        self.root = Node("", None, "root")
        self.groups: dict[str, Node] = {}
        self.leaves: dict[str, Node] = {}
        self.osd_items: set[str] = set()
        self.device_kinds: dict[str, str] = {}
        self.mono = mono_font()
        self.bold = QFont()
        self.bold.setBold(True)
        store.updated.connect(self.refresh)
        store.favourites_changed.connect(self._decorations_changed)

    # structure -------------------------------------------------------
    def _index_of(self, node: Node, col: int = 0) -> QModelIndex:
        if node is self.root:
            return QModelIndex()
        return self.createIndex(node.row, col, node)

    def _append(self, parent: Node, node: Node) -> None:
        pidx = self._index_of(parent)
        r = len(parent.children)
        self.beginInsertRows(pidx, r, r)
        node.row = r
        parent.children.append(node)
        self.endInsertRows()

    def _group_node(self, path: str) -> Node:
        node = self.groups.get(path)
        if node is not None:
            return node
        parts = path.split("/")
        parent = self.root if len(parts) == 1 else self._group_node("/".join(parts[:-1]))
        node = Node(parts[-1].strip() or "?", parent, "device" if len(parts) == 1 else "group", path=path)
        self._append(parent, node)
        self.groups[path] = node
        return node

    def refresh(self) -> None:
        dirty: dict[int, Node] = {}
        for sid, s in self.store.sensors.items():
            leaf = self.leaves.get(sid)
            if leaf is None:
                g = self._group_node(s.get("group") or "Other")
                leaf = Node(s.get("label") or sid, g, "sensor", sid=sid)
                self._append(g, leaf)
                self.leaves[sid] = leaf
            else:
                leaf.name = s.get("label") or sid
                dirty[id(leaf.parent)] = leaf.parent
        for parent in dirty.values():
            n = len(parent.children)
            if n:
                self.dataChanged.emit(self.createIndex(0, 0, parent.children[0]),
                                      self.createIndex(n - 1, len(COLS) - 1, parent.children[-1]),
                                      [Qt.DisplayRole, Qt.ForegroundRole, Qt.BackgroundRole, Qt.ToolTipRole])

    def repaint_all(self) -> None:
        def walk(node):
            if node.children:
                self.dataChanged.emit(self.createIndex(0, 0, node.children[0]),
                                      self.createIndex(len(node.children) - 1, len(COLS) - 1, node.children[-1]))
                for c in node.children:
                    walk(c)
        walk(self.root)

    def set_osd_items(self, items: list[str]) -> None:
        new = set(items)
        if new != self.osd_items:
            self.osd_items = new
            self._decorations_changed()

    def _decorations_changed(self) -> None:
        for leaf in self.leaves.values():
            i = self.createIndex(leaf.row, 0, leaf)
            self.dataChanged.emit(i, i, [Qt.DecorationRole])

    # QAbstractItemModel ---------------------------------------------
    def index(self, row, column, parent=QModelIndex()):
        pnode = parent.internalPointer() if parent.isValid() else self.root
        if 0 <= row < len(pnode.children) and 0 <= column < len(COLS):
            return self.createIndex(row, column, pnode.children[row])
        return QModelIndex()

    def parent(self, index=QModelIndex()):
        if not index.isValid():
            return QModelIndex()
        node: Node = index.internalPointer()
        p = node.parent
        if p is None or p is self.root:
            return QModelIndex()
        return self.createIndex(p.row, 0, p)

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            if parent.column() != 0:
                return 0
            return len(parent.internalPointer().children)
        return len(self.root.children)

    def columnCount(self, parent=QModelIndex()):
        return len(COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal:
            if role == Qt.DisplayRole:
                return COLS[section]
            if role == Qt.TextAlignmentRole:
                return int((Qt.AlignLeft if section == 0 else Qt.AlignRight) | Qt.AlignVCenter)
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        node: Node = index.internalPointer()
        col = index.column()
        if role == KIND_ROLE:
            return node.kind
        if node.kind != "sensor":
            if col == 0:
                if role == Qt.DisplayRole:
                    return node.name
                if role == Qt.FontRole and node.kind == "device":
                    return self.bold
                if role == Qt.DecorationRole and node.kind == "device":
                    return device_badge(node.name, self.device_kinds.get(node.name, ""))
            if role == Qt.BackgroundRole and node.kind == "device":
                return QBrush(theme().device_bg)
            return None
        s = self.store.sensors.get(node.sid, {})
        if role == SENSOR_ID_ROLE:
            return node.sid
        if role == Qt.DisplayRole:
            if col == 0:
                return node.name
            return format_value(s.get(VALUE_KEYS[col]), s.get("decimals", 0), s.get("unit", ""))
        if role == Qt.TextAlignmentRole:
            return int((Qt.AlignLeft if col == 0 else Qt.AlignRight) | Qt.AlignVCenter)
        if role == Qt.FontRole and col > 0:
            return self.mono
        if role == Qt.DecorationRole and col == 0:
            return sensor_marker(node.sid in self.store.favourites, node.sid in self.osd_items)
        if role == Qt.ForegroundRole:
            if col == 0:
                return None
            if s.get(VALUE_KEYS[col]) is None:
                return QBrush(theme().dim)
            if col in (1, 3):
                level = severity(s, s.get(VALUE_KEYS[col]))
                c = severity_color(level)
                if c is not None:
                    return QBrush(c)
            return None
        if role == Qt.BackgroundRole:
            if severity(s, s.get("value")) == THROTTLE:
                return QBrush(theme().throttle_bg)
            return None
        if role == Qt.ToolTipRole:
            lines = [f"<b>{s.get('label', node.sid)}</b>", f"<code>{node.sid}</code>"]
            if s.get("hint"):
                lines.append(s["hint"])
            return "<br>".join(lines)
        return None


class SensorFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRecursiveFilteringEnabled(True)
        self.text = ""
        self.fav_only = False

    def set_text(self, text: str) -> None:
        self.text = text.strip().lower()
        self.invalidateFilter()

    def set_fav_only(self, on: bool) -> None:
        self.fav_only = on
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        if not self.text and not self.fav_only:
            return True
        src: SensorTreeModel = self.sourceModel()
        idx = src.index(row, 0, parent)
        node: Node = idx.internalPointer()
        if node is None or node.kind != "sensor":
            return False   # groups are shown when a descendant matches (recursive filtering)
        if self.fav_only and node.sid not in src.store.favourites:
            return False
        if self.text:
            s = src.store.sensors.get(node.sid, {})
            hay = f"{s.get('label', '')} {node.sid} {s.get('group', '')}".lower()
            return all(w in hay for w in self.text.split())
        return True


class ValueTree(QTreeView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlternatingRowColors(True)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.setAnimated(False)
        self.setIndentation(16)
        self.setIconSize(QSize(30, 14))


class Sparkline(QWidget):
    """Small live graph window for one sensor (client-side history)."""

    closed = Signal(str)

    def __init__(self, store: SensorStore, sid: str, parent=None):
        super().__init__(parent, Qt.Tool | Qt.WindowStaysOnTopHint)
        self.store = store
        self.sid = sid
        self.minutes = 5
        s = store.sensors.get(sid, {})
        self.setWindowTitle(f"{s.get('label', sid)} — meat³")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(420, 170)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        top = QHBoxLayout()
        self.title = QLabel(f"<b>{s.get('label', sid)}</b>  <span style='color:{theme().dim.name()}'>"
                            f"{s.get('group', '')}</span>")
        top.addWidget(self.title, 1)
        self.span = QComboBox()
        for m in (1, 5, 10, 30):
            self.span.addItem(f"{m} min", m)
        self.span.setCurrentIndex(1)
        self.span.currentIndexChanged.connect(lambda _: self._set_span())
        top.addWidget(self.span)
        lay.addLayout(top)
        lay.addStretch(1)
        store.updated.connect(self.update)

    def _set_span(self):
        self.minutes = self.span.currentData()
        self.update()

    def closeEvent(self, e):
        self.closed.emit(self.sid)
        super().closeEvent(e)

    def paintEvent(self, ev):
        t = theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), t.base)
        s = self.store.sensors.get(self.sid, {})
        hist = self.store.history.get(self.sid, ())
        now = self.store.t or time.time()
        t0 = now - self.minutes * 60
        pts = [(ts, v) for ts, v in hist if ts >= t0]
        if pts:
            t0 = min(pts[0][0], now - 10)     # fit the x axis to the data until the span fills up
        span = max(1e-6, now - t0)
        area = QRectF(10, 40, self.width() - 20, self.height() - 62)
        p.setPen(QPen(t.border, 1))
        p.drawRect(area)
        vals = [v for _, v in pts if v is not None]
        mono = mono_font()
        p.setFont(mono)
        unit, dec = s.get("unit", ""), s.get("decimals", 0)
        if not vals:
            p.setPen(t.dim)
            p.drawText(area, Qt.AlignCenter, "no data yet")
            return
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-9:
            lo, hi = lo - 1, hi + 1
        pad = (hi - lo) * 0.08
        lo, hi = lo - pad, hi + pad
        for frac in (0.25, 0.5, 0.75):
            y = area.top() + area.height() * frac
            p.setPen(QPen(t.border, 1, Qt.DotLine))
            p.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))

        def xy(ts, v):
            x = area.left() + (ts - t0) / span * area.width()
            y = area.bottom() - (v - lo) / (hi - lo) * area.height()
            return QPointF(x, y)

        path = QPainterPath()
        fill = QPainterPath()
        started = False
        first_x = None
        last = None
        for ts, v in pts:
            if v is None:
                started = False
                continue
            pt = xy(ts, v)
            if not started:
                path.moveTo(pt)
                started = True
            else:
                path.lineTo(pt)
            if first_x is None:
                first_x = pt.x()
                fill.moveTo(QPointF(pt.x(), area.bottom()))
            fill.lineTo(pt)
            last = pt
        if last is not None:
            fill.lineTo(QPointF(last.x(), area.bottom()))
            fill.closeSubpath()
            c = QColor(t.accent)
            c.setAlpha(45)
            p.fillPath(fill, c)
        p.setPen(QPen(t.accent, 1.6))
        p.drawPath(path)
        p.setPen(t.dim)
        p.drawText(QRectF(area.left() + 4, area.top() + 2, area.width() - 8, 16), Qt.AlignLeft,
                   format_value(max(vals), dec, unit))
        p.drawText(QRectF(area.left() + 4, area.bottom() - 18, area.width() - 8, 16), Qt.AlignLeft,
                   format_value(min(vals), dec, unit))
        mins = span / 60
        p.drawText(QRectF(area.left(), area.bottom() + 1, area.width(), 14), Qt.AlignRight,
                   f"last {mins:.0f} min" if mins >= 1 else f"last {span:.0f} s")
        cur = s.get("value")
        big = QFont(mono)
        big.setPointSizeF(mono.pointSizeF() + 3)
        big.setBold(True)
        p.setFont(big)
        col = severity_color(severity(s, cur)) or t.text
        p.setPen(col)
        p.drawText(QRectF(area.left(), area.top() + 2, area.width() - 6, 24), Qt.AlignRight,
                   format_value(cur, dec, unit))


class SensorsPage(QWidget):
    osd_toggle_requested = Signal(str, bool)   # sensor id, add?
    status = Signal(str)

    def __init__(self, store: SensorStore, link, parent=None):
        super().__init__(parent)
        self.store = store
        self.link = link
        self.model = SensorTreeModel(store, self)
        self.proxy = SensorFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.graphs: dict[str, Sparkline] = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 8)
        lay.setSpacing(6)
        tb = QToolBar()
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.toolbar = tb
        self.act_reset = tb.addAction("Reset min/max/avg", self.reset_stats)
        self.act_reset.setToolTip("Clear the daemon's min/max/avg statistics (reset_stats)")
        self.act_pause = tb.addAction("Pause")
        self.act_pause.setCheckable(True)
        self.act_pause.toggled.connect(self._pause)
        self.act_log = tb.addAction("Log to CSV…")
        self.act_log.setCheckable(True)
        self.act_log.toggled.connect(self._toggle_log)
        tb.addSeparator()
        tb.addAction("Expand all", self.expand_all)
        tb.addAction("Collapse all", self.collapse_all)
        self.act_fav = tb.addAction("★ Favourites only")
        self.act_fav.setCheckable(True)
        self.act_fav.toggled.connect(self._fav_only)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter sensors…  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(240)
        self.search.textChanged.connect(self._filter)
        tb.addWidget(self.search)
        lay.addWidget(tb)
        find = QAction(self)
        find.setShortcut(QKeySequence.Find)
        find.triggered.connect(lambda: (self.search.setFocus(), self.search.selectAll()))
        self.addAction(find)

        self.tree = ValueTree()
        self.tree.setModel(self.proxy)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.doubleClicked.connect(self._double)
        hdr = self.tree.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        fm = QFontMetrics(mono_font())
        w = fm.horizontalAdvance("88888.888 RPM") + 22
        for c in range(1, len(COLS)):
            hdr.setSectionResizeMode(c, QHeaderView.Interactive)
            hdr.resizeSection(c, w)
        hdr.setMinimumSectionSize(60)
        lay.addWidget(self.tree, 1)
        self._expanded_once: set[str] = set()
        self.model.rowsInserted.connect(self._rows_inserted)

    # behaviour -------------------------------------------------------
    def _rows_inserted(self, parent, first, last):
        # expand every device/group the first time it appears (HWiNFO default)
        QTimer.singleShot(0, self._expand_new)

    def _expand_new(self):
        for path, node in self.model.groups.items():
            if path in self._expanded_once:
                continue
            self._expanded_once.add(path)
            pidx = self.proxy.mapFromSource(self.model._index_of(node))
            if pidx.isValid():
                self.tree.expand(pidx)

    def reset_stats(self):
        self.link.request({"cmd": "reset_stats"},
                          lambda r: self.status.emit("Statistics reset" if r.get("ok") else
                                                     f"reset_stats failed: {r.get('msg', '')}"))

    def _pause(self, on: bool):
        self.link.set_paused(on)
        self.act_pause.setText("Resume" if on else "Pause")
        self.status.emit("Updates paused" if on else "Updates resumed")

    def _toggle_log(self, on: bool):
        if on:
            if self.store.logging:
                return
            path, _ = QFileDialog.getSaveFileName(self, "Log sensors to CSV",
                                                  time.strftime("meat3-%Y%m%d-%H%M%S.csv"),
                                                  "CSV files (*.csv);;All files (*)")
            if not path:
                self.act_log.blockSignals(True)
                self.act_log.setChecked(False)
                self.act_log.blockSignals(False)
                return
            self.start_logging(path)
        else:
            self.store.stop_log()
            self.act_log.setText("Log to CSV…")
            self.status.emit("CSV logging stopped")

    def start_logging(self, path: str):
        try:
            self.store.start_log(path)
        except OSError as e:
            self.status.emit(f"Cannot open {path}: {e}")
            self.act_log.blockSignals(True)
            self.act_log.setChecked(False)
            self.act_log.blockSignals(False)
            return
        self.act_log.blockSignals(True)
        self.act_log.setChecked(True)
        self.act_log.blockSignals(False)
        self.act_log.setText("● Logging")
        self.status.emit(f"Logging {len(self.store.log_ids)} sensors to {path}")

    def expand_all(self):
        self.tree.expandAll()

    def collapse_all(self):
        self.tree.collapseAll()

    def _filter(self, text: str):
        self.proxy.set_text(text)
        if text or self.proxy.fav_only:
            self.tree.expandAll()

    def _fav_only(self, on: bool):
        self.proxy.set_fav_only(on)
        self.tree.expandAll()

    def _sid_at(self, pidx) -> Optional[str]:
        if not pidx.isValid():
            return None
        return self.proxy.data(pidx.siblingAtColumn(0), SENSOR_ID_ROLE)

    def _double(self, pidx):
        sid = self._sid_at(pidx)
        if sid:
            self.show_graph(sid)

    def _menu(self, pos):
        m = self.build_menu(pos)
        if m is not None:
            m.exec(self.tree.viewport().mapToGlobal(pos))

    def build_menu(self, pos) -> Optional[QMenu]:
        pidx = self.tree.indexAt(pos)
        sid = self._sid_at(pidx)
        m = QMenu(self)
        if sid:
            on_osd = sid in self.model.osd_items
            a = m.addAction("Remove from OSD" if on_osd else "Add to OSD")
            a.triggered.connect(lambda: self.osd_toggle_requested.emit(sid, not on_osd))
            fav = sid in self.store.favourites
            a = m.addAction("Remove from favourites" if fav else "Add to favourites")
            a.triggered.connect(lambda: self.store.set_favourite(sid, not fav))
            m.addSeparator()
            m.addAction("Copy value", lambda: self.copy(sid, False))
            m.addAction("Copy label and value", lambda: self.copy(sid, True))
            m.addAction("Copy sensor id", lambda: QGuiApplication.clipboard().setText(sid))
            m.addSeparator()
            m.addAction("Show graph…", lambda: self.show_graph(sid))
        elif pidx.isValid():
            src = self.proxy.mapToSource(pidx.siblingAtColumn(0))
            m.addAction("Expand", lambda: self.tree.expandRecursively(pidx))
            m.addAction("Collapse", lambda: self.tree.collapse(pidx))
            node = src.internalPointer()
            ids = []

            def collect(n):
                if n.kind == "sensor":
                    ids.append(n.sid)
                for c in n.children:
                    collect(c)
            collect(node)
            m.addSeparator()
            m.addAction(f"Add {len(ids)} sensors to favourites",
                        lambda: [self.store.set_favourite(i, True) for i in ids])
        else:
            return None
        return m

    def copy(self, sid: str, with_label: bool):
        s = self.store.sensors.get(sid, {})
        v = format_value(s.get("value"), s.get("decimals", 0), s.get("unit", ""))
        QGuiApplication.clipboard().setText(f"{s.get('label', sid)}: {v}" if with_label else v)
        self.status.emit(f"Copied {v}")

    def show_graph(self, sid: str) -> Sparkline:
        g = self.graphs.get(sid)
        if g is None:
            g = Sparkline(self.store, sid, self.window())
            g.closed.connect(lambda i: self.graphs.pop(i, None))
            self.graphs[sid] = g
        g.show()
        g.raise_()
        return g
