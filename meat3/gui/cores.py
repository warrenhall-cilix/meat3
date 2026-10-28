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
    ("IPC", ["core{k}.ipc"], False),
    ("Back-end", ["core{k}.stall_be"], False),
    ("Front-end", ["core{k}.stall_fe"], False),
    ("SMT", ["core{k}.stall_smt"], False),
]
MAX_SUFFIX = " max"
CORE_RE = re.compile(r"^(?P<p>.+)\.core(?P<k>\d+)\.clk_eff$")



def reserve_text(sens: dict, pfx: str) -> str:
    """Second title line: state of the L3 game reservation (cpu0.l3.reserve.*), or "" without CAT."""
    r = sens.get(f"{pfx}.l3.reserve.active")
    if r is None:
        return ""
    state = r.get("label", "").removeprefix("L3 reservation: ")
    if r.get("value"):
        g = sens.get(f"{pfx}.l3.reserve.game_mb", {}).get("value")
        b = sens.get(f"{pfx}.l3.reserve.bg_mb", {}).get("value")
        split = f": game {g:.0f} MB, everything else {b:.0f} MB" if g is not None and b is not None else ""
        return f"<br><b>L3 reserved</b> {state.removeprefix('active ')}{split}"
    return f"<br>L3 reservation: {state} <i>(Overclock → L3 cache)</i>"

class CoresPage(QWidget):
    osd_toggle_requested = Signal(str, bool)
    status = Signal(str)

    def __init__(self, store: SensorStore, parent=None):
        super().__init__(parent)
        self.store = store
        self.osd_items: set[str] = set()
        self.tables: dict[str, QTableWidget] = {}
        self.titles: dict[str, tuple[QLabel, str]] = {}
        self.layout_ids: dict[str, list[list[Optional[str]]]] = {}   # prefix -> rows -> col sid
        self.cols: dict[str, list[tuple[str, int, bool]]] = {}        # prefix -> visible (header, col idx, is_max)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(8, 8, 8, 8)
        self.lay.setSpacing(6)
        self.empty = QLabel("No per-core CPU sensors yet (waiting for the daemon).")
        self.empty.setObjectName("dim")
        self.lay.addWidget(self.empty)
        self.lay.addStretch(1)
        self.app_title: Optional[QLabel] = None
        self.app_table: Optional[QTableWidget] = None
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
        title = QLabel()
        self.titles[prefix] = (title, f"<b>{(first.get('group') or prefix).split('/')[0]}</b> — {len(cores)} cores")
        title.setText(self.titles[prefix][1])
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
        self._refresh_apps()
        sens = self.store.sensors
        th = theme()
        for p, t in self.tables.items():
            lbl, base = self.titles[p]
            l3 = []
            for i in range(16):
                occ = sens.get(f"{p}.l3.ccd{i}.occupancy", {}).get("value")
                if f"{p}.l3.ccd{i}.occupancy" not in sens:
                    break
                pct = sens.get(f"{p}.l3.ccd{i}.occupancy_pct", {}).get("value")
                if occ is not None:
                    l3.append(f"CCD {i} L3 {occ:.1f} MB" + (f" ({pct:.0f}%)" if pct is not None else ""))
            lbl.setText(base + ("  ·  " + "  ·  ".join(l3) if l3 else ""))
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

    # ------------------------------------------------------------------ L3 by app
    APP_RE = re.compile(r"^(?P<p>.+)\.l3app\.(?P<k>[a-z0-9_]+)\.occupancy$")

    def _refresh_apps(self) -> None:
        sens = self.store.sensors
        rows, game, other, pfx = [], None, None, None
        for sid, s in sens.items():
            m = self.APP_RE.match(sid)
            if not m:
                continue
            pfx = m.group("p")
            k = m.group("k")
            if k == "game":
                game = s
            elif k == "other":
                other = s
            elif s.get("value") is not None:
                cpu = sens.get(f"{pfx}.l3app.{k}.cpu", {}).get("value")
                rows.append((s["value"], s["label"].removesuffix(" L3 in use"), cpu, s.get("hint", ""), sid))
        if pfx is None:
            return
        if self.app_table is None:
            self.app_title = QLabel()
            t = QTableWidget(0, 4)
            t.setHorizontalHeaderLabels(["App", "L3 in use", "% of L3", "CPU"])
            t.setEditTriggers(QTableWidget.NoEditTriggers)
            t.setAlternatingRowColors(True)
            t.setFont(mono_font())
            t.verticalHeader().hide()
            hh = t.horizontalHeader()
            hh.setSectionResizeMode(QHeaderView.Stretch)
            t.setContextMenuPolicy(Qt.CustomContextMenu)
            t.customContextMenuRequested.connect(self._app_menu)
            idx = self.lay.count() - 1
            self.lay.insertWidget(idx, self.app_title)
            self.lay.insertWidget(idx + 1, t)
            self.app_table = t
        size_mb = None
        for sid, s in sens.items():
            if sid.startswith(f"{pfx}.l3.ccd") and sid.endswith(".occupancy_pct"):
                m = re.search(r"% of (\d+) MiB", s.get("label", ""))
                if m:
                    size_mb = (size_mb or 0) + int(m.group(1)) * 1.048576
        if other is not None and other.get("value") is not None:
            rows.append((other["value"], "Everything else", None, other.get("hint", ""), other.get("id")))
        rows.sort(key=lambda r: -r[0])
        gtxt = ""
        if game is not None and game.get("value") is not None:
            gname = (game.get("hint") or "").removeprefix("game: ").split(" (")[0]
            gtxt = f"  ·  <b>Game</b> {gname}: {game['value']:.1f} MB"
        self.app_title.setText(f"<b>L3 by app</b> — live cache occupancy per program{gtxt}"
                               + reserve_text(sens, pfx))
        self.app_title.setToolTip(sens.get(f"{pfx}.l3.reserve.active", {}).get("hint", ""))
        t = self.app_table
        t.setRowCount(len(rows))
        for i, (mb, name, cpu, hint, sid) in enumerate(rows):
            cells = [name, f"{mb:.1f} MB", "—" if not size_mb else f"{100 * mb / size_mb:.0f}%",
                     "—" if cpu is None else f"{cpu:.1f}%"]
            for j, txt in enumerate(cells):
                it = t.item(i, j)
                if it is None:
                    it = QTableWidgetItem()
                    it.setTextAlignment((Qt.AlignLeft if j == 0 else Qt.AlignRight) | Qt.AlignVCenter)
                    t.setItem(i, j, it)
                it.setText(txt)
                it.setToolTip(hint)
                it.setData(Qt.UserRole, sid)
        h = t.horizontalHeader().height() + t.verticalHeader().defaultSectionSize() * max(1, len(rows)) + 4
        t.setMinimumHeight(h)
        t.setMaximumHeight(h)

    def _app_menu(self, pos) -> None:
        it = self.app_table.itemAt(pos)
        sid = it.data(Qt.UserRole) if it else None
        game_sid = next((k for k in self.store.sensors if k.endswith(".l3app.game.occupancy")), None)
        m = QMenu(self)
        if sid:
            on = sid in self.osd_items
            m.addAction("Remove from OSD" if on else "Add to OSD", lambda: self.osd_toggle_requested.emit(sid, not on))
        if game_sid:
            on = game_sid in self.osd_items
            m.addAction("Remove game L3 from OSD" if on else "Add game L3 to OSD (follows whatever game runs)",
                        lambda: self.osd_toggle_requested.emit(game_sid, not on))
        if not m.isEmpty():
            m.exec(self.app_table.viewport().mapToGlobal(pos))

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
