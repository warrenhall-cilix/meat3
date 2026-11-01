"""Stress-test dialog for the Overclock tab: pick a preset, run it, watch the key CPU sensors live."""
from __future__ import annotations

import html
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (QComboBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QSpinBox, QVBoxLayout)

from meat3.gui.common import mono_font, theme
from meat3.stresstest import PRESETS, StressRun, install_hint, pick_sensors, tool_path

SAMPLE_EVERY_MS = 2000


def _fmt(label: str, v) -> str:
    if not isinstance(v, (int, float)):
        return "—"
    if label == "vcore":
        return f"{v:.3f} V"
    if label == "Tctl":
        return f"{v:.1f} °C"
    if label == "clock":
        return f"{v:.0f} MHz"
    if label == "power":
        return f"{v:.0f} W"
    if label == "current":
        return f"{v:.0f} A"
    return f"{v:.1f}"


class StressDialog(QDialog):
    finished_run = Signal(object)      # the StressRun, once it has a result

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.run: Optional[StressRun] = None
        self.command_override: Optional[list[str]] = None     # tests: run this instead of the real tool
        self.setWindowTitle("CPU stress test")
        self.resize(640, 420)
        v = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Test"))
        self.preset = QComboBox()
        for key, p in PRESETS.items():
            self.preset.addItem(p["label"], key)
        self.preset.currentIndexChanged.connect(self._preset_changed)
        top.addWidget(self.preset, 1)
        top.addWidget(QLabel("for"))
        self.seconds = QSpinBox()
        self.seconds.setRange(30, 7200)
        self.seconds.setSuffix(" s")
        self.seconds.setSingleStep(30)
        top.addWidget(self.seconds)
        self.btn_start = QPushButton("Start")
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(self.start)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop)
        top.addWidget(self.btn_start)
        top.addWidget(self.btn_stop)
        v.addLayout(top)

        self.hint = QLabel("")
        self.hint.setObjectName("dim")
        self.hint.setWordWrap(True)
        v.addWidget(self.hint)
        self.warn = QLabel("Manual overclocks do not survive a crash: if the box locks up or reboots it comes "
                           "back at stock. Save a profile first so you can re-apply.")
        self.warn.setWordWrap(True)
        self.warn.setObjectName("dim")
        v.addWidget(self.warn)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        for col, head in enumerate(("", "now", "peak")):
            lab = QLabel(head)
            lab.setObjectName("dim")
            grid.addWidget(lab, 0, col)
        self.rows: dict[str, tuple[QLabel, QLabel, QLabel]] = {}
        for i, label in enumerate(("clock", "Tctl", "power", "current", "vcore"), start=1):
            name = QLabel(label)
            now = QLabel("—")
            peak = QLabel("—")
            for w in (now, peak):
                w.setFont(mono_font())
                w.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                w.setMinimumWidth(QFontMetrics(mono_font()).horizontalAdvance("88888 MHz") + 8)
            grid.addWidget(name, i, 0)
            grid.addWidget(now, i, 1)
            grid.addWidget(peak, i, 2)
            self.rows[label] = (name, now, peak)
        grid.setColumnStretch(3, 1)
        self.elapsed = QLabel("")
        self.elapsed.setFont(mono_font())
        grid.addWidget(self.elapsed, 1, 3, Qt.AlignRight | Qt.AlignTop)
        v.addLayout(grid)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.setTextFormat(Qt.RichText)
        v.addWidget(self.result)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(mono_font())
        self.log.setMaximumBlockCount(2000)
        v.addWidget(self.log, 1)

        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._tick)
        self._next_sample = 0.0
        self.ids: dict[str, str] = {}
        self._preset_changed()

    # ------------------------------------------------------------------ ui
    @property
    def preset_key(self) -> str:
        return self.preset.currentData()

    def _preset_changed(self):
        p = PRESETS[self.preset_key]
        if self.run is None or not self.run.running:
            self.seconds.setValue(int(p["seconds"]))
        path = tool_path(p["tool"]) if self.command_override is None else "override"
        self.hint.setText(p["hint"] + ("" if path else f"   —   needs {install_hint(p['tool'])}"))
        self.btn_start.setEnabled(bool(path) and not (self.run is not None and self.run.running))

    def _set_result(self, text: str, ok: Optional[bool]):
        t = theme()
        colour = t.text if ok is None else (t.green if ok else t.red)
        self.result.setText(f"<span style='color:{colour.name()}; font-weight:bold'>{html.escape(text)}</span>")

    def _append(self, line: str):
        self.log.appendPlainText(line)

    # ------------------------------------------------------------------ run
    def start(self):
        if self.run is not None and self.run.running:
            return
        self.ids = pick_sensors(self.store.sensors.keys())
        for label, (_, now, peak) in self.rows.items():
            now.setText("—")
            peak.setText("—")
            self.rows[label][0].setEnabled(label in self.ids)
        self.run = StressRun(self.preset_key, self.seconds.value(), command=self.command_override)
        self.run.start()
        self._set_result("running…", None)
        self.log.clear()
        self._append(f"{PRESETS[self.preset_key]['label']} for {self.run.seconds}s; "
                     f"watching {', '.join(self.ids.values()) or 'no CPU sensors found'}")
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.preset.setEnabled(False)
        self.seconds.setEnabled(False)
        self._next_sample = 0.0
        self.timer.start()
        self._tick()

    def stop(self):
        if self.run is not None and self.run.running:
            self.run.stop()
            self._append("stopping…")

    def _tick(self):
        run = self.run
        if run is None:
            return
        done = run.poll()
        self.elapsed.setText(f"{run.elapsed:.0f} / {run.seconds} s")
        if not done and run.elapsed >= self._next_sample:
            self._next_sample = run.elapsed + SAMPLE_EVERY_MS / 1000
            vals = {k: self.store.sensors.get(sid, {}).get("value") for k, sid in self.ids.items()}
            run.sample(vals)
            for k, v in vals.items():
                _, now, peak = self.rows[k]
                now.setText(_fmt(k, v))
                peak.setText(_fmt(k, run.peaks.get(k)))
            self._append(f"{run.elapsed:5.0f}s  " + "  ".join(f"{k}={_fmt(k, v)}" for k, v in vals.items()))
        if done:
            self.timer.stop()
            self._set_result(run.summary(), None if run.user_stop else run.ok)
            self._append(run.summary())
            if run.kernel:
                for ln in run.kernel[:5]:
                    self._append("kernel: " + ln)
            if not run.ok and run.output.strip():
                for ln in run.output.strip().splitlines()[-6:]:
                    self._append("  " + ln[:160])
            self.btn_stop.setEnabled(False)
            self.preset.setEnabled(True)
            self.seconds.setEnabled(True)
            self._preset_changed()
            self.finished_run.emit(run)

    def closeEvent(self, ev):
        if self.run is not None and self.run.running:
            self.run.stop()
            ev.ignore()                # stay open until the tool has gone and the result is shown
            self._append("stopping… (window closes when the test has ended)")
            QTimer.singleShot(1500, self.close)
            return
        super().closeEvent(ev)
