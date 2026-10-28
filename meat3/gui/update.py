"""Update dialog: shows what's new on GitHub and runs the daemon's pull + reinstall."""
from __future__ import annotations

import html

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout


class UpdateDialog(QDialog):
    def __init__(self, link, parent=None):
        super().__init__(parent)
        self.link = link
        self.setWindowTitle("Update meat³")
        self.resize(640, 420)
        v = QVBoxLayout(self)
        self.head = QLabel("Checking…")
        self.head.setWordWrap(True)
        v.addWidget(self.head)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        v.addWidget(self.text, 1)
        bb = QDialogButtonBox()
        self.btn_check = QPushButton("Check again")
        self.btn_update = QPushButton("Update now")
        self.btn_update.setDefault(True)
        bb.addButton(self.btn_check, QDialogButtonBox.ActionRole)
        bb.addButton(self.btn_update, QDialogButtonBox.AcceptRole)
        bb.addButton(QDialogButtonBox.Close)
        bb.rejected.connect(self.close)
        bb.button(QDialogButtonBox.Close).clicked.connect(self.close)
        self.btn_check.clicked.connect(self.check)
        self.btn_update.clicked.connect(self.start)
        self.btn_update.setEnabled(False)
        v.addWidget(bb)
        self.started = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.check()

    def check(self):
        self.started = False
        self.link.request({"cmd": "update_check"}, lambda r: self.refresh())

    def start(self):
        def done(r):
            self.started = bool(r.get("ok"))
            self.head.setText(html.escape(str(r.get("msg", ""))) if r.get("ok")
                              else f"<b>Update failed to start:</b> {html.escape(str(r.get('msg', '')))}")
            self.refresh()
        self.btn_update.setEnabled(False)
        self.link.request({"cmd": "update_start"}, done)

    def refresh(self):
        self.link.request({"cmd": "update_status"}, self.show_status)

    def show_status(self, u: dict):
        if not u.get("ok"):
            if self.started:      # meat3d restarts at the end of an update
                self.head.setText("Installing… meat3d is restarting.")
            else:
                self.head.setText("This daemon has no updater yet. Update once by hand:<br>"
                                  "<code>cd ~/meat3 &amp;&amp; packaging/update.sh</code>")
            return
        inst = html.escape(u.get("installed", "?"))
        log = u.get("log") or []
        if u.get("running") or self.started:
            if u.get("running"):
                self.head.setText(f"<b>Updating…</b> (installed {inst}). The window reconnects when meat3d restarts.")
            elif log and log[-1].startswith("== done"):
                self.head.setText(f"<b>Updated to {inst}.</b> Restart meat³ to load the new window too.")
                win = self.parent()
                if hasattr(win, "restart"):
                    self.btn_update.setText("Restart meat³")
                    try:
                        self.btn_update.clicked.disconnect()
                    except (RuntimeError, TypeError):
                        pass
                    self.btn_update.clicked.connect(win.restart)
                    self.btn_update.setEnabled(True)
                    return
            else:
                self.head.setText("<b>Update failed.</b> Nothing was changed if it stopped before installing; "
                                  "the log below says why.")
            self.text.setPlainText("\n".join(log))
            self.text.verticalScrollBar().setValue(self.text.verticalScrollBar().maximum())
            self.btn_update.setEnabled(False)
            return
        if u.get("checking"):
            self.head.setText(f"Installed {inst}. Checking GitHub…")
        elif u.get("error"):
            self.head.setText(f"Installed {inst}. <b>Can't check for updates:</b> {html.escape(u['error'])}")
        elif u.get("behind"):
            who = "" if u.get("can_update") else " (only the owner of the meat³ git checkout can install them)"
            self.head.setText(f"Installed {inst}. <b>{u['behind']} update{'s' * (u['behind'] != 1)} available</b>{who}:")
            self.text.setPlainText("\n".join(u.get("commits") or []))
        elif u.get("behind") == 0:
            self.head.setText(f"Installed {inst}. <b>Up to date.</b>")
            self.text.setPlainText("")
        self.btn_update.setEnabled(bool(u.get("behind")) and bool(u.get("can_update")) and not u.get("checking"))

    def closeEvent(self, e):
        self.timer.stop()
        super().closeEvent(e)
