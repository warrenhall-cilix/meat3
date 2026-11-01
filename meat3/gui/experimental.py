"""Experimental tab: the RDNA4 firmware pin, the regulator write switches, the scale caps and the guard
thresholds, all behind a notice the user has to accept once per machine (stored in the daemon's config).

Everything here goes through meat3d (`experimental_get`, `experimental_set`, `smufw_run`), so it needs the
meat3 group like the Overclock tab. The daemon re-reads the settings at once; no restart.
"""
from __future__ import annotations

import html
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QStackedWidget, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

NOTICE = """<h3>Experimental features</h3>
<p>What's behind this page changes the card below the driver and below AMD's own firmware limits. It was
built from reverse engineering and has run on a handful of cards. It can crash the machine, corrupt a
session, and in the wrong combination shorten the life of a graphics card. Nothing here is covered by
anyone's warranty.</p>
<ul>
<li><b>SMU firmware pin</b> rewrites the Radeon firmware file your distribution installed and rebuilds the
initramfs. The card's security processor only runs AMD-signed firmware, so a bad file is refused, not run;
the one known failure mode so far was a boot on the basic framebuffer until <i>Restore</i> was run.</li>
<li><b>Regulator writes</b> change what the card's voltage regulators report to the firmware and what they
deliver. Telemetry scaling makes the firmware's power and current limits blind by the chosen factor; the
only remaining limits are the temperatures, the guards on this page, and your cooling.</li>
<li><b>Guards</b> are the safety net for all of the above. Raising a threshold is your decision.</li>
</ul>
<p>Everything is reversible: nothing is flashed, the gains go back to stock when the daemon stops, the
offsets are lost at power-off, and the firmware pin has a Restore button.</p>"""


def _unit_for(sensor: str) -> str:
    s = sensor or ""
    if ".t." in s or "temp" in s:
        return "°C"
    if "power" in s:
        return "W"
    if s.endswith(".a") or "current" in s:
        return "A"
    return ""


class ExperimentalPage(QWidget):
    def __init__(self, link, parent=None):
        super().__init__(parent)
        self.link = link
        self.state: dict = {}
        self.loaded = False
        self.can_write = True
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self.stack.addWidget(self._build_gate())
        self.stack.addWidget(self._build_body())
        self.unavailable = QLabel("")
        self.unavailable.setWordWrap(True)
        self.stack.addWidget(self.unavailable)
        self.stack.setCurrentIndex(2)
        self.unavailable.setText("Waiting for the daemon…")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)

    # ---------------------------------------------------------------- gate
    def _build_gate(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        lbl = QLabel(NOTICE)
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.RichText)
        v.addWidget(lbl)
        self.chk_understand = QCheckBox("I understand that these features can crash this machine or damage the hardware, "
                                        "and that I use them at my own risk.")
        v.addWidget(self.chk_understand)
        self.btn_enable = QPushButton("Enable experimental features")
        self.btn_enable.setEnabled(False)
        self.chk_understand.toggled.connect(self.btn_enable.setEnabled)
        self.btn_enable.clicked.connect(self.accept)
        h = QHBoxLayout()
        h.addWidget(self.btn_enable)
        h.addStretch(1)
        v.addLayout(h)
        v.addStretch(1)
        return w

    def accept(self):
        self._set({"experimental.accepted": True}, note="experimental features enabled")

    def disable(self):
        self._set({"experimental.accepted": False}, note="experimental features disabled")

    # ---------------------------------------------------------------- body
    def _build_body(self) -> QWidget:
        area = QScrollArea()
        area.setWidgetResizable(True)
        w = QWidget()
        v = QVBoxLayout(w)
        area.setWidget(w)

        # firmware pin -------------------------------------------------
        g = QGroupBox("SMU firmware pin (RDNA4: RX 9070 / 9070 XT)")
        gv = QVBoxLayout(g)
        self.fw_status = QLabel("…")
        self.fw_status.setWordWrap(True)
        self.fw_status.setTextFormat(Qt.RichText)
        gv.addWidget(self.fw_status)
        hb = QHBoxLayout()
        self.btn_pin = QPushButton("Pin the known-good firmware")
        self.btn_restore = QPushButton("Restore the distribution firmware")
        self.btn_pin.clicked.connect(lambda: self.run_smufw("pin"))
        self.btn_restore.clicked.connect(lambda: self.run_smufw("restore"))
        hb.addWidget(self.btn_pin)
        hb.addWidget(self.btn_restore)
        hb.addStretch(1)
        gv.addLayout(hb)
        self.fw_log = QPlainTextEdit()
        self.fw_log.setReadOnly(True)
        self.fw_log.setMaximumHeight(140)
        self.fw_log.hide()
        gv.addWidget(self.fw_log)
        v.addWidget(g)

        # regulator writes ---------------------------------------------
        g = QGroupBox("Regulator (VRM) writes")
        f = QFormLayout(g)
        self.chk_allow = QCheckBox("Allow writes to the voltage regulators (TBP/TDC scaling, voltage offsets)")
        f.addRow(self.chk_allow)
        self.cmb_lsb = QComboBox()
        self.cmb_lsb.addItems(["not verified (offset writes refused)", "5 mV per step", "6.25 mV per step"])
        f.addRow("MPS offset step size", self.cmb_lsb)
        self.chk_restore = QCheckBox("Put the stock gains back when the daemon stops")
        f.addRow(self.chk_restore)
        self.chk_mps_exp = QCheckBox("Allow current-sense gain writes on MPS controllers (encoding unverified)")
        f.addRow(self.chk_mps_exp)
        self.btn_vrm_apply = QPushButton("Apply")
        self.btn_vrm_apply.clicked.connect(self.apply_vrm)
        f.addRow(self.btn_vrm_apply)
        v.addWidget(g)

        # limits ---------------------------------------------------------
        g = QGroupBox("Limits of the scaling controls")
        f = QFormLayout(g)
        self.spin_tbp = QDoubleSpinBox()
        self.spin_tbp.setRange(1.0, 1.5)
        self.spin_tbp.setSingleStep(0.05)
        self.spin_tbp.setDecimals(2)
        self.spin_tbp.setSuffix(" ×")
        f.addRow("Maximum board power (TBP) scale", self.spin_tbp)
        self.spin_tdc = QDoubleSpinBox()
        self.spin_tdc.setRange(1.0, 1.5)
        self.spin_tdc.setSingleStep(0.05)
        self.spin_tdc.setDecimals(2)
        self.spin_tdc.setSuffix(" ×")
        f.addRow("Maximum GFX current (TDC) scale", self.spin_tdc)
        self.spin_off = QDoubleSpinBox()
        self.spin_off.setRange(0, 100)
        self.spin_off.setSingleStep(5)
        self.spin_off.setDecimals(0)
        self.spin_off.setSuffix(" mV")
        f.addRow("Maximum regulator voltage offset", self.spin_off)
        self.limits_note = QLabel("")
        self.limits_note.setWordWrap(True)
        f.addRow(self.limits_note)
        self.btn_limits_apply = QPushButton("Apply")
        self.btn_limits_apply.clicked.connect(self.apply_limits)
        f.addRow(self.btn_limits_apply)
        v.addWidget(g)

        # guards ---------------------------------------------------------
        g = QGroupBox("Safety guards")
        gv = QVBoxLayout(g)
        note = QLabel("A guard trips when its sensor stays above the threshold for a few polls and then sets its "
                      "control to the safe value. Edit the threshold or switch a guard off here; the built-in "
                      "list and its actions stay as they are.")
        note.setWordWrap(True)
        gv.addWidget(note)
        self.guards = QTableWidget(0, 4)
        self.guards.setHorizontalHeaderLabels(["Guard", "On", "Trips above", "Action"])
        self.guards.horizontalHeader().setStretchLastSection(True)
        self.guards.verticalHeader().hide()
        self.guards.setMinimumHeight(220)
        gv.addWidget(self.guards)
        hb = QHBoxLayout()
        self.btn_guards_apply = QPushButton("Apply guard changes")
        self.btn_guards_apply.clicked.connect(self.apply_guards)
        self.btn_guards_reset = QPushButton("Reset to the built-in guards")
        self.btn_guards_reset.setToolTip("Removes your own guards list and every override from the config file")
        self.btn_guards_reset.clicked.connect(self.reset_guards)
        hb.addWidget(self.btn_guards_apply)
        hb.addWidget(self.btn_guards_reset)
        hb.addStretch(1)
        gv.addLayout(hb)
        v.addWidget(g)

        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        v.addWidget(self.msg)
        hb = QHBoxLayout()
        self.btn_disable = QPushButton("Disable experimental features")
        self.btn_disable.setFlat(True)
        self.btn_disable.clicked.connect(self.disable)
        hb.addStretch(1)
        hb.addWidget(self.btn_disable)
        v.addLayout(hb)
        v.addStretch(1)
        return area

    # ---------------------------------------------------------------- daemon I/O
    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()
        self.timer.start(2000)

    def hideEvent(self, e):
        self.timer.stop()
        super().hideEvent(e)

    def refresh(self):
        if getattr(self.link, "is_connected", True):
            self.link.request({"cmd": "experimental_get"}, self.on_state)

    def on_state(self, r: dict):
        if not r.get("ok"):
            self.unavailable.setText("This daemon has no experimental page (update meat3d) or refused: "
                                     + html.escape(str(r.get("msg", ""))))
            self.stack.setCurrentIndex(2)
            return
        self.state = r
        self.can_write = bool(r.get("can_write", True))
        if not r.get("accepted"):
            self.stack.setCurrentIndex(0)
            self.btn_enable.setEnabled(self.chk_understand.isChecked() and self.can_write)
            if not self.can_write:
                self.chk_understand.setToolTip("join the meat3 group to change anything here")
            return
        self.stack.setCurrentIndex(1)
        self._show_smufw(r.get("smufw") or {}, r.get("smufw_run") or {})
        if not self.loaded:
            self._fill_settings(r, "all")
            self.loaded = True
        for b in (self.btn_vrm_apply, self.btn_limits_apply, self.btn_guards_apply, self.btn_guards_reset, self.btn_disable):
            b.setEnabled(self.can_write)

    def _fill_settings(self, r: dict, which: str = "all"):
        """Fill one card ("vrm", "limits", "guards") or all of them from the daemon's state. Only the card that was
        just applied is refilled, so unsaved edits elsewhere survive."""
        a = r.get("amdgpu_vrm") or {}
        hard = float(r.get("hard_max_scale", 1.5))
        if which in ("all", "vrm"):
            self.chk_allow.setChecked(bool(a.get("allow_writes")))
            lsb = a.get("mps_offset_lsb_mv")
            self.cmb_lsb.setCurrentIndex(1 if lsb == 5 else 2 if lsb == 6.25 else 0)
            self.chk_restore.setChecked(bool(a.get("restore_on_stop", True)))
            self.chk_mps_exp.setChecked(bool(a.get("experimental_mps_writes")))
        if which in ("all", "limits"):
            for spin, key in ((self.spin_tbp, "max_tbp_scale"), (self.spin_tdc, "max_tdc_scale")):
                spin.setRange(1.0, hard)
                spin.setValue(float(a.get(key) or 1.3))
            self.spin_off.setValue(float(a.get("max_offset_mv") or 50))
            self.limits_note.setText(f"The hard cap in the code is {hard:g}×. Real power = the firmware's limit × the "
                                     "TBP scale; a 374 W limit at 1.5× is 561 W, which is what the guards below are for.")
        if which not in ("all", "guards"):
            return
        self.guards.setRowCount(0)
        for g in r.get("guards") or []:
            row = self.guards.rowCount()
            self.guards.insertRow(row)
            name = QTableWidgetItem(str(g.get("name", "")))
            name.setFlags(name.flags() & ~Qt.ItemIsEditable)
            name.setToolTip(f"sensor {g.get('sensor')} for {g.get('count', 1)} polls")
            self.guards.setItem(row, 0, name)
            on = QTableWidgetItem("")
            on.setFlags((on.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable)
            on.setCheckState(Qt.Checked if g.get("enabled", True) else Qt.Unchecked)
            self.guards.setItem(row, 1, on)
            unit = _unit_for(g.get("sensor", ""))
            above = QTableWidgetItem(f"{g.get('above')}" + (f" {unit}" if unit else ""))
            self.guards.setItem(row, 2, above)
            act = QTableWidgetItem(f"{g.get('control')} := {g.get('value')}")
            act.setFlags(act.flags() & ~Qt.ItemIsEditable)
            self.guards.setItem(row, 3, act)
        self.guards.resizeColumnsToContents()
        self.btn_guards_reset.setVisible(True)

    def _show_smufw(self, fw: dict, run: dict):
        devs = fw.get("devices") or []
        if fw.get("error"):
            self.fw_status.setText(f"Firmware status unavailable: {html.escape(str(fw['error']))}")
        elif not devs:
            self.fw_status.setText("No RDNA4 GPU detected. The firmware pin only applies to RX 9070 / 9070 XT cards.")
        else:
            lines = []
            for d in devs:
                state = "VRM bus open" if d.get("vrm_bus_open") else f"VRM bus closed (newer than {d.get('good_pmfw_hex')})"
                inst = d.get("installed")
                file_state = ("not installed" if not inst else "known-good build" if d.get("pinned")
                              else "distribution build" if d.get("pinned") is False else "unknown")
                lines.append(f"<b>{html.escape(d.get('pci', ''))}</b>: running PMFW {d.get('pmfw_hex')} ({state}); "
                             f"installed file: {file_state}" + (f"; backup kept" if d.get("backup") else "")
                             + (f"<br><b>{html.escape(d['note'])}</b>" if d.get("note") else ""))
            if fw.get("hook"):
                lines.append("pacman hook installed: the pin survives firmware package updates")
            self.fw_status.setText("<br>".join(lines))
        busy = bool(run.get("running"))
        for b in (self.btn_pin, self.btn_restore):
            b.setEnabled(bool(devs) and not busy and self.can_write)
        log = run.get("log") or []
        if log or busy:
            self.fw_log.show()
            text = "\n".join(log)
            if self.fw_log.toPlainText() != text:
                self.fw_log.setPlainText(text)
                self.fw_log.verticalScrollBar().setValue(self.fw_log.verticalScrollBar().maximum())
            if not busy and run.get("rc") is not None:
                self.msg.setText(f"{run.get('action')} finished with exit code {run.get('rc')}."
                                 + (" <b>Reboot to run the new firmware.</b>" if run.get("rc") == 0 else
                                    " Nothing was changed if it stopped before installing; the log says why."))

    def confirm(self, title: str, text: str) -> bool:
        mb = QMessageBox(QMessageBox.Warning, title, text, QMessageBox.Yes | QMessageBox.Cancel, self)
        mb.setDefaultButton(QMessageBox.Cancel)
        return mb.exec() == QMessageBox.Yes

    def run_smufw(self, action: str):
        what = ("replace the installed smu_14_0_3.bin with the June 2025 build from linux-firmware (sha256-checked), "
                "keep a backup, rebuild the initramfs and install a pacman hook" if action == "pin" else
                "put the distribution's smu_14_0_3.bin back from the backup and rebuild the initramfs")
        if not self.confirm("SMU firmware " + action, f"This will {what}. A reboot is needed afterwards.\n\n"
                            "If the desktop comes back on one screen at 60 Hz, the card refused to start: run "
                            "Restore here or `sudo meat3-smufw --restore` and reboot again."):
            return
        self.msg.setText("")
        self.fw_log.setPlainText("")
        self.fw_log.show()
        self.link.request({"cmd": "smufw_run", "action": action},
                          lambda r: self.msg.setText(html.escape(str(r.get("msg", ""))) if r.get("ok")
                                                     else f"<b>Refused:</b> {html.escape(str(r.get('msg', '')))}"))
        self.refresh()

    def _set(self, settings: dict, guards: Optional[dict] = None, reset_guards: bool = False, note: str = "",
             refill: str = "all"):
        req = {"cmd": "experimental_set", "settings": settings}
        if guards:
            req["guards"] = guards
        if reset_guards:
            req["reset_guards"] = True

        def done(r):
            if r.get("ok"):
                self.msg.setText(html.escape(note or "saved") + " (the daemon re-read its settings)")
                full = dict(r, ok=True, can_write=self.can_write, smufw=self.state.get("smufw", {}),
                            smufw_run=self.state.get("smufw_run", {}))
                if refill == "all":
                    self.loaded = False        # accept / disable / reset: everything from what the daemon now has
                    self.on_state(full)
                else:
                    self.state = full
                    self.on_state(full)
                    self._fill_settings(full, refill)
            else:
                self.msg.setText(f"<b>Not saved:</b> {html.escape(str(r.get('msg', '')))}")
        self.link.request(req, done)

    def apply_vrm(self):
        lsb = [None, 5, 6.25][self.cmb_lsb.currentIndex()]
        self._set({"amdgpu_vrm.allow_writes": self.chk_allow.isChecked(), "amdgpu_vrm.mps_offset_lsb_mv": lsb,
                   "amdgpu_vrm.restore_on_stop": self.chk_restore.isChecked(),
                   "amdgpu_vrm.experimental_mps_writes": self.chk_mps_exp.isChecked()}, note="regulator settings saved",
                  refill="vrm")

    def apply_limits(self):
        self._set({"amdgpu_vrm.max_tbp_scale": round(self.spin_tbp.value(), 2),
                   "amdgpu_vrm.max_tdc_scale": round(self.spin_tdc.value(), 2),
                   "amdgpu_vrm.max_offset_mv": float(self.spin_off.value())}, note="limits saved", refill="limits")

    def apply_guards(self):
        changes: dict = {}
        for row in range(self.guards.rowCount()):
            name = self.guards.item(row, 0).text()
            on = self.guards.item(row, 1).checkState() == Qt.Checked
            raw = self.guards.item(row, 2).text().split()[0] if self.guards.item(row, 2).text().strip() else ""
            try:
                above = float(raw)
            except ValueError:
                self.msg.setText(f"<b>Not saved:</b> guard {html.escape(name)}: threshold {html.escape(raw)!r} is not a number")
                return
            changes[name] = {"above": above, "enabled": on}
        self._set({}, guards=changes, note="guard changes saved", refill="guards")

    def reset_guards(self):
        if not self.confirm("Reset guards", "Remove your own guards list and every override from the config file, "
                            "so the built-in guards apply again?"):
            return
        self._set({}, reset_guards=True, note="built-in guards restored")
