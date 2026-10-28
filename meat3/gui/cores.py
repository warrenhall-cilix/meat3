"""Per-core view: one row per CPU core, one column per metric (clock / voltage / temp / power / usage).

Rows are discovered from sensor ids '<prefix>.core<N>.clk_eff'; each column tries a list of id
templates so it works for cpu_amd (SMU PM table ids), cpu_generic and the mock daemon. Columns
with no data for a CPU are hidden. Cells show the current value with the daemon's session max
underneath in the tooltip; right-click a cell to add that sensor to the OSD.
"""
from __future__ import annotations

import re
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import (QHeaderView, QLabel, QMenu, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from meat3.core.fmt import NONE_TEXT, format_value
from meat3.gui.common import SensorStore, mono_font, severity, severity_color, theme

# (header, [id templates relative to the CPU prefix, first present wins], show session max?)
COLUMNS = [
    ("Eff clock", ["core{k}.clk_eff"], True),
    ("Active", ["core{k}.clk_active"], True),
    ("Volt", ["pmt.core{k}.volt", "core{k}.volt"], True),
    ("Temp", ["pmt.core{k}.temp", "core{k}.temp"], True),
    ("Power", ["core{k}.power", "pmt.core{k}.power"], True),
    ("Usage", ["core{k}.util", "core{k}.usage"], False),
    ("C0", ["core{k}.c0", "pmt.core{k}.c0"], False),
    ("CC6", ["pmt.core{k}.cc6"], False),
    ("Fmax", ["pmt.core{k}.fmax"], False),
    ("CPPC", ["core{k}.cppc_rank"], False),
]
MAX_SUFFIX = " max"
CORE_RE = re.compile(r"^(?P<p>.+)\.core(?P<k>\d+)\.clk_eff$")


class CoresPage(QWidget):
    osd_toggle_requested = Signal(str, bool)
    status = Signal(str)

    def __init__(self, store: SensorStore, parent=None):
        super().__init__(parent)
        self.store = store
        self.osd_items: set[str] = set()
        self.tables: dict[str, QTableWidget] = {}
        self.layout_ids: dict[str, list[list[Optional[str]]]] = {}   # prefix -> rows -> col sid
        self.cols: dict[str, list[tuple[str, int, bool]]] = {}        # prefix -> visible (header, col idx, is_max)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(8, 8, 8, 8)
        self.lay.setSpacing(6)
        self.empty = QLabel("No per-core CPU sensors yet (waiting for the daemon).")
        self.empty.setObjectName("dim")
        self.lay.addWidget(self.empty)
        self.lay.addStretch(1)
        store.updated.connect(self.refresh)

    # ------------------------------------------------------------------ discovery
    def _prefixes(self) -> dict[str, list[int]]:
        found: dict[str, list[int]] = {}
        for sid in self.store.sensors:
            m = CORE_RE.match(sid)
            if m:
                found.setdefault(m.group("p"), []).append(int(m.group("k")))
        return {p: sorted(set(ks)) for p, ks in found.items()}

    def _build(self, prefix: str, cores: list[int]) -> None:
        sens = self.store.sensors
        rows: list[list[Optional[str]]] = []
        for k in cores:
            row = []
            for _, tpls, _ in COLUMNS:
                row.append(next((f"{prefix}.{t.format(k=k)}" for t in tpls
                                 if f"{prefix}.{t.format(k=k)}" in sens), None))
            rows.append(row)
        cols = []
        for ci, (hdr, _, with_max) in enumerate(COLUMNS):
            if any(r[ci] for r in rows):
                cols.append((hdr, ci, False))
                if with_max:
                    cols.append((hdr + MAX_SUFFIX, ci, True))
        first = sens.get(f"{prefix}.core{cores[0]}.clk_eff", {})
        title = QLabel(f"<b>{(first.get('group') or prefix).split('/')[0]}</b> — {len(cores)} cores")
        t = QTableWidget(len(cores), len(cols))
        t.setHorizontalHeaderLabels([c[0] for c in cols])
        t.setVerticalHeaderLabels([f"Core {k}" for k in cores])
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        t.setSelectionMode(QTableWidget.SingleSelection)
        t.setAlternatingRowColors(True)
        t.setFont(mono_font())
        t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        t.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        t.setContextMenuPolicy(Qt.CustomContextMenu)
        t.customContextMenuRequested.connect(lambda pos, p=prefix: self._menu(p, pos))
        h = t.horizontalHeader().height() + t.verticalHeader().defaultSectionSize() * len(cores) + 4
        t.setMinimumHeight(h)
        t.setMaximumHeight(h)
        idx = self.lay.count() - 1                      # keep the stretch last
        self.lay.insertWidget(idx, title)
        self.lay.insertWidget(idx + 1, t)
        self.tables[prefix] = t
        self.layout_ids[prefix] = rows
        self.cols[prefix] = cols

    # ------------------------------------------------------------------ update
    def refresh(self) -> None:
        found = self._prefixes()
        for p, cores in found.items():
            if p not in self.tables:
                self._build(p, cores)
        self.empty.setVisible(not self.tables)
        if not self.isVisible():
            return
        sens = self.store.sensors
        th = theme()
        for p, t in self.tables.items():
            rows, cols = self.layout_ids[p], self.cols[p]
            # highlight the fastest core (by effective clock) and the hottest core
            best = {}
            for ci_name, ci in (("clk", 0), ("temp", 3)):
                vals = [(sens.get(r[ci] or "", {}).get("value"), i) for i, r in enumerate(rows)]
                vals = [v for v in vals if v[0] is not None]
                if vals:
                    best[ci_name] = max(vals)[1]
            for ri, row in enumerate(rows):
                for cj, (hdr, ci, is_max) in enumerate(cols):
                    sid = row[ci]
                    s = sens.get(sid, {}) if sid else {}
                    v = s.get("max" if is_max else "value") if s else None
                    text = format_value(v, s.get("decimals", 0), s.get("unit", "")) if s else NONE_TEXT
                    it = t.item(ri, cj)
                    if it is None:
                        it = QTableWidgetItem()
                        it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                        t.setItem(ri, cj, it)
                    it.setText(text)
                    it.setData(Qt.UserRole, sid)
                    if sid and not is_max:
                        tip = f"{s.get('label', sid)}\n{sid}"
                        if s.get("min") is not None:
                            tip += (f"\nmin {format_value(s.get('min'), s.get('decimals', 0), s.get('unit', ''))}"
                                    f" · avg {format_value(s.get('avg'), s.get('decimals', 0), s.get('unit', ''))}"
                                    f" · max {format_value(s.get('max'), s.get('decimals', 0), s.get('unit', ''))}")
                        if s.get("hint"):
                            tip += f"\n{s['hint']}"
                        it.setToolTip(tip + ("\n[on OSD]" if sid in self.osd_items else ""))
                    col = severity_color(severity(s, v)) if s else None
                    if col is None and is_max:
                        col = th.dim
                    it.setForeground(QBrush(col) if col is not None else QBrush(th.text))
                    f = it.font()
                    f.setBold((ci == 0 and best.get("clk") == ri and not is_max)
                              or (ci == 3 and best.get("temp") == ri and not is_max))
                    f.setUnderline(bool(sid) and sid in self.osd_items and not is_max)
                    it.setFont(f)

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()

    def set_osd_items(self, items: list[str]) -> None:
        self.osd_items = set(items)

    # ------------------------------------------------------------------ context menu
    def _menu(self, prefix: str, pos) -> None:
        t = self.tables[prefix]
        it = t.itemAt(pos)
        if it is None:
            return
        sid = it.data(Qt.UserRole)
        col_ci = self.cols[prefix][it.column()][1]
        col_ids = [r[col_ci] for r in self.layout_ids[prefix] if r[col_ci]]
        m = QMenu(self)
        if sid:
            on = sid in self.osd_items
            m.addAction("Remove from OSD" if on else "Add to OSD",
                        lambda: self.osd_toggle_requested.emit(sid, not on))
        if col_ids:
            hdr = COLUMNS[col_ci][0]
            m.addAction(f"Add all cores' {hdr.lower()} to OSD",
                        lambda: [self.osd_toggle_requested.emit(i, True) for i in col_ids if i not in self.osd_items])
            m.addAction(f"Remove all cores' {hdr.lower()} from OSD",
                        lambda: [self.osd_toggle_requested.emit(i, False) for i in col_ids if i in self.osd_items])
        m.exec(t.viewport().mapToGlobal(pos))
