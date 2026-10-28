#!/usr/bin/env python3
"""Screenshot the GUI against the live daemon (offscreen). Usage:
QT_QPA_PLATFORM=offscreen python3 tools/shots_live.py OUTDIR [sensor_id_to_center ...]"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from PySide6.QtWidgets import QApplication, QAbstractItemView   # noqa: E402

from meat3.gui.app import MainWindow, apply_theme, install_none_refcount_guard   # noqa: E402

out = sys.argv[1]
targets = sys.argv[2:] or ["nv0.t.hotspot", "nv0.pin3.a"]
os.makedirs(out, exist_ok=True)
app = QApplication(sys.argv)
install_none_refcount_guard()
apply_theme(True)
w = MainWindow("/run/meat3/meat3.sock", True)
w.start()
w.resize(1500, 1000)
w.show()


def pump(sec):
    t = time.time() + sec
    while time.time() < t:
        app.processEvents()
        time.sleep(0.02)


pump(6)
model = w.sensors.model
w.sensors.tree.collapseAll()
w.sensors.tree.expandAll()
for i, sid in enumerate(targets):
    leaf = model.leaves.get(sid)
    if leaf is None:
        print("missing", sid)
        continue
    w.tabs.setCurrentIndex(0)
    w.sensors.tree.scrollTo(w.sensors.proxy.mapFromSource(model.createIndex(leaf.row, 0, leaf)),
                            QAbstractItemView.PositionAtCenter)
    pump(1.5)
    w.grab().save(os.path.join(out, f"live-sensors-{i}.png"))
for page, name in ((w.graphs, "graphs"), (w.cores, "cores"), (w.overclock, "overclock"), (w.osd, "osd")):
    w.tabs.setCurrentIndex(w.tabs.indexOf(page))
    pump(2)
    w.grab().save(os.path.join(out, f"live-{name}.png"))
print("sensors:", len(model.leaves))
