"""Guard-trip dialog: what tripped, what it changed, and buttons to put the previous values back or dismiss."""
from __future__ import annotations

import html
import time

from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
                               QScrollArea)


def alert_text(e: dict) -> str:
    before = e.get("before") or {}
    acts = ", ".join(e.get("actions") or [])
    return (f"{time.strftime('%H:%M:%S', time.localtime(e.get('t', 0)))}  {e.get('guard', '')}: {e.get('trigger', '')}"
            + (f"  →  {acts}" if acts else "")
            + (f"  (was {', '.join(f'{k}={v}' for k, v in before.items())})" if before else ""))


class GuardDialog(QDialog):
    def __init__(self, link, parent=None):
        super().__init__(parent)
        self.link = link
        self.setWindowTitle("Safety guard tripped")
        self.resize(720, 360)
        v = QVBoxLayout(self)
        self.head = QLabel("A guard changed a setting to protect the hardware. The new value stays until you "
                           "change it; 'Restore' puts back what was set before the trip.")
        self.head.setWordWrap(True)
        v.addWidget(self.head)
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.body = QWidget()
        self.rows = QVBoxLayout(self.body)
        self.rows.addStretch(1)
        self.area.setWidget(self.body)
        v.addWidget(self.area, 1)
        bb = QDialogButtonBox()
        self.btn_all = QPushButton("Dismiss all")
        bb.addButton(self.btn_all, QDialogButtonBox.ActionRole)
        bb.addButton(QDialogButtonBox.Close)
        bb.rejected.connect(self.close)
        bb.button(QDialogButtonBox.Close).clicked.connect(self.close)
        self.btn_all.clicked.connect(lambda: self._cmd({"cmd": "guard_ack", "id": "all"}))
        v.addWidget(bb)
        self.entries: list[dict] = []
        self.result_lbl = QLabel("")
        self.result_lbl.setWordWrap(True)
        v.addWidget(self.result_lbl)

    def load(self, alerts: list[dict]) -> None:
        if [e.get("id") for e in alerts] == [e.get("id") for e in self.entries]:
            return
        self.entries = list(alerts)
        while self.rows.count() > 1:
            w = self.rows.takeAt(0).widget()
            if w:
                w.deleteLater()
        for e in reversed(self.entries):
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            lab = QLabel(html.escape(alert_text(e)))
            lab.setWordWrap(True)
            h.addWidget(lab, 1)
            if e.get("before"):
                b = QPushButton("Restore previous")
                b.setToolTip("Apply the values these controls had before the trip (the condition may still be there)")
                b.clicked.connect(lambda _=False, i=e["id"]: self._cmd({"cmd": "guard_restore", "id": i}))
                h.addWidget(b)
            d = QPushButton("Dismiss")
            d.clicked.connect(lambda _=False, i=e["id"]: self._cmd({"cmd": "guard_ack", "id": i}))
            h.addWidget(d)
            self.rows.insertWidget(self.rows.count() - 1, row)
        if not self.entries and not self.result_lbl.text():
            self.result_lbl.setText("No open guard trips.")

    def _cmd(self, req: dict) -> None:
        def done(r):
            self.result_lbl.setText(html.escape(str(r.get("msg") or ("done" if r.get("ok") else "failed"))))
        self.link.request(req, done)
