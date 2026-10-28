"""meat³ GUI entry point:  python3 -m meat3.gui [--socket PATH] [--light] [--hidden]"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from typing import Optional

from PySide6.QtCore import QByteArray, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow, QMenu,
                               QMessageBox, QPushButton, QStatusBar, QSystemTrayIcon, QTabWidget,
                               QVBoxLayout, QWidget)

from meat3.gui.client import DaemonLink, default_socket_path
from meat3.gui.cores import CoresPage
from meat3.gui.common import (SensorStore, app_icon, apply_theme, install_none_refcount_guard,
                              setting_bool, settings, theme)
from meat3.gui.osd import OsdPage, OsdStyle, OverlayWindow
from meat3.gui.overclock import OverclockPage
from meat3.gui.sensors import SensorsPage

APP_TITLE = "meat³"


class Banner(QFrame):
    def __init__(self, on_retry, parent=None):
        super().__init__(parent)
        self.setObjectName("banner")
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 8, 8, 8)
        self.text = QLabel()
        self.text.setObjectName("bannerText")
        self.text.setWordWrap(True)
        h.addWidget(self.text, 1)
        self.retry = QPushButton("Retry now")
        self.retry.clicked.connect(on_retry)
        h.addWidget(self.retry)

    def show_error(self, path: str, why: str):
        self.text.setText(f"<b>Daemon not running</b> — cannot reach <code>{path}</code>: {why}.<br>"
                          f"Start it with <code>sudo systemctl start meat3d</code> "
                          f"(or <code>python3 -m meat3.core.mockd --socket {path}</code> for the mock). "
                          f"Retrying every 2 s…")
        self.show()


class MainWindow(QMainWindow):
    def __init__(self, socket_path: str, dark: bool = True):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(app_icon())
        self.quitting = False
        self.socket_path = socket_path
        self.store = SensorStore(self)
        self.osd_style = OsdStyle()
        self.link = DaemonLink(socket_path, interval=min(1.0, self.osd_style.rate_ms / 1000), parent=self)
        self.overlay = OverlayWindow(self.store, self.osd_style)

        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.banner = Banner(self.link.retry)
        self.banner.hide()
        bw = QWidget()
        bl = QVBoxLayout(bw)
        bl.setContentsMargins(8, 8, 8, 0)
        bl.addWidget(self.banner)
        v.addWidget(bw)
        self.banner_wrap = bw
        bw.hide()

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.sensors = SensorsPage(self.store, self.link)
        self.cores = CoresPage(self.store)
        self.overclock = OverclockPage(self.store, self.link)
        self.osd = OsdPage(self.store, self.link, self.sensors.model, self.overlay, self.osd_style)
        self.tabs.addTab(self.sensors, "Sensors")
        self.tabs.addTab(self.cores, "Cores")
        self.tabs.addTab(self.overclock, "Overclock")
        self.tabs.addTab(self.osd, "OSD")
        v.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        sb = QStatusBar()
        self.setStatusBar(sb)
        self.conn_lbl = QLabel("connecting…")
        self.poll_lbl = QLabel("")
        self.log_lbl = QLabel("")
        sb.addPermanentWidget(self.log_lbl)
        sb.addPermanentWidget(self.poll_lbl)
        sb.addPermanentWidget(self.conn_lbl)

        for page in (self.sensors, self.cores, self.overclock, self.osd):
            page.status.connect(lambda m: self.statusBar().showMessage(m, 6000))
        self.sensors.osd_toggle_requested.connect(self._osd_toggle_item)
        self.cores.osd_toggle_requested.connect(self._osd_toggle_item)
        self.osd.style_changed.connect(self._style_changed)
        self.link.connected.connect(self._on_connected)
        self.link.disconnected.connect(self._on_disconnected)
        self.link.snapshot.connect(self._on_snapshot)
        self.link.osd_state.connect(self._on_osd_state)
        self.store.logging_changed.connect(self._on_logging)

        self._build_menu(dark)
        self._build_tray()
        self.resize(1180, 820)
        st = settings()
        geo = st.value("window/geometry")
        if isinstance(geo, QByteArray):
            self.restoreGeometry(geo)
        self.tabs.setCurrentIndex(int(st.value("window/tab", 0)))

    # ---------------------------------------------------------------- UI chrome
    def _build_menu(self, dark: bool):
        mb = self.menuBar()
        m = mb.addMenu("&File")
        m.addAction("Hide to tray", self.hide).setShortcut(QKeySequence("Ctrl+W"))
        a = m.addAction("&Quit", self.quit)
        a.setShortcut(QKeySequence.Quit)
        m = mb.addMenu("&View")
        self.act_dark = m.addAction("Dark theme")
        self.act_dark.setCheckable(True)
        self.act_dark.setChecked(dark)
        self.act_dark.toggled.connect(self.set_dark)
        m.addSeparator()
        for i, name in enumerate(("Sensors", "Cores", "Overclock", "OSD")):
            a = m.addAction(name, lambda i=i: self.tabs.setCurrentIndex(i))
            a.setShortcut(QKeySequence(f"Ctrl+{i + 1}"))
        m = mb.addMenu("&OSD")
        self.act_osd = m.addAction("OSD enabled", self.toggle_osd)
        self.act_osd.setCheckable(True)
        m = mb.addMenu("&Help")
        m.addAction("About meat³", self._about)

    def _build_tray(self):
        self.tray: Optional[QSystemTrayIcon] = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip(APP_TITLE)
        menu = QMenu()
        self.tray_show = menu.addAction("Hide window", self.toggle_window)
        self.tray_osd = menu.addAction("OSD enabled", self.toggle_osd)
        self.tray_osd.setCheckable(True)
        menu.addSeparator()
        menu.addAction("Quit", self.quit)
        self.tray.setContextMenu(menu)
        self.tray_menu = menu
        self.tray.activated.connect(lambda r: self.toggle_window() if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()

    def toggle_window(self):
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def showEvent(self, e):
        super().showEvent(e)
        if self.tray:
            self.tray_show.setText("Hide window")

    def hideEvent(self, e):
        super().hideEvent(e)
        if self.tray:
            self.tray_show.setText("Show window")

    def set_dark(self, dark: bool):
        apply_theme(dark)
        settings().setValue("theme/dark", dark)
        self.sensors.model.repaint_all()      # colours are computed in data(); keep expansion
        self.overclock.signature = ()       # rebuild rows so danger colours follow the theme
        self.overclock.refresh()

    def _about(self):
        QMessageBox.about(self, "About meat³",
                          "<h3>meat³</h3><p>Linux hardware monitor &amp; overclocking tool.</p>"
                          f"<p>Daemon socket: <code>{self.socket_path}</code><br>"
                          f"Daemon: {self.link.hello.get('version', '?')} on {self.link.hello.get('host', '?')}</p>")

    # ---------------------------------------------------------------- daemon events
    def _on_connected(self, hello: dict):
        self.banner_wrap.hide()
        self.banner.hide()
        kinds = {b.get("name", ""): b.get("kind", "") for b in hello.get("backends", [])}
        # device badge colours: tree device names are matched against backend names
        self.sensors.model.device_kinds = {}
        self._backend_kinds = kinds
        self.conn_lbl.setText(f"● {hello.get('host', '?')} · v{hello.get('version', '?')} · "
                              f"{len(hello.get('backends', []))} devices")
        self.conn_lbl.setStyleSheet(f"color:{theme().green.name()}")
        self.statusBar().showMessage("Connected to daemon", 4000)

    def _on_disconnected(self, why: str):
        self.banner.show_error(self.socket_path, why)
        self.banner_wrap.show()
        self.conn_lbl.setText("● daemon not running")
        self.conn_lbl.setStyleSheet(f"color:{theme().red.name()}")
        self.poll_lbl.setText("")
        self.store.mark_stale()
        self._update_overlay_visibility()

    def _on_snapshot(self, snap: dict):
        kinds = getattr(self, "_backend_kinds", {})
        if kinds:
            for path, node in self.sensors.model.groups.items():
                if node.kind == "device" and node.name not in self.sensors.model.device_kinds:
                    for bname, kind in kinds.items():
                        if bname and bname.lower() in node.name.lower():
                            self.sensors.model.device_kinds[node.name] = kind
        self.store.ingest(snap)
        self.poll_lbl.setText(f"{len(self.store.sensors)} sensors · {time.strftime('%H:%M:%S', time.localtime(self.store.t))}")

    def _on_osd_state(self, osd: dict):
        en = bool(osd.get("enabled"))
        self.act_osd.setChecked(en)
        if self.tray:
            self.tray_osd.setChecked(en)
            self.tray.setIcon(app_icon())
        self.sensors.model.set_osd_items(osd.get("items") or [])
        self.cores.set_osd_items(osd.get("items") or [])
        self._update_overlay_visibility()

    def _update_overlay_visibility(self):
        want = (self.link.is_connected and bool(self.link.osd.get("enabled"))
                and self.osd_style.desktop)
        if want and not self.overlay.isVisible():
            self.overlay.refresh()
            self.overlay.show()
        elif not want and self.overlay.isVisible():
            self.overlay.hide()

    def _style_changed(self):
        self.link.set_interval(min(1.0, self.osd_style.rate_ms / 1000))
        self._update_overlay_visibility()

    def _on_logging(self, on: bool, path: str):
        self.log_lbl.setText(f"● logging to {path}" if on else "")
        self.log_lbl.setStyleSheet(f"color:{theme().accent.name()}")

    def _osd_toggle_item(self, sid: str, add: bool):
        (self.osd.add_ids if add else self.osd.remove_ids)([sid])
        self.sensors.model.set_osd_items(self.osd.items)
        self.cores.set_osd_items(self.osd.items)

    def toggle_osd(self):
        def done(r):
            if r.get("ok"):
                self.link.osd = dict(self.link.osd, enabled=bool(r.get("enabled")))
                self._on_osd_state(self.link.osd)
                self.osd.on_osd_state(self.link.osd)
                self.statusBar().showMessage("OSD " + ("enabled" if r.get("enabled") else "disabled"), 3000)
            else:
                self.statusBar().showMessage(f"osd_toggle failed: {r.get('msg')}", 6000)
        self.link.request({"cmd": "osd_toggle"}, done)

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        self.link.start()

    def closeEvent(self, e):
        if self.tray is not None and self.tray.isVisible() and not self.quitting:
            e.ignore()
            self.hide()
            st = settings()
            if not setting_bool(st, "tray/hinted", False):
                self.tray.showMessage(APP_TITLE, "meat³ keeps running in the tray. Use Quit to exit.",
                                      app_icon(), 4000)
                st.setValue("tray/hinted", True)
            return
        self.shutdown()
        e.accept()

    def shutdown(self):
        st = settings()
        st.setValue("window/geometry", self.saveGeometry())
        st.setValue("window/tab", self.tabs.currentIndex())
        st.sync()
        self.store.stop_log()
        self.overlay.hide()
        self.link.stop()

    def quit(self):
        self.quitting = True
        self.shutdown()
        QApplication.quit()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="meat3.gui", description="meat³ GUI")
    ap.add_argument("--socket", default=default_socket_path(),
                    help="daemon socket (default $MEAT3_SOCKET or /run/meat3/meat3.sock)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--light", action="store_true", help="start with the light theme")
    g.add_argument("--dark", action="store_true", help="start with the dark theme")
    ap.add_argument("--hidden", action="store_true", help="start minimised to the tray")
    args, qt_args = ap.parse_known_args(argv)
    QApplication.setApplicationName("meat3")
    QApplication.setApplicationDisplayName(APP_TITLE)
    QApplication.setDesktopFileName("meat3")
    app = QApplication.instance() or QApplication([sys.argv[0]] + qt_args)
    install_none_refcount_guard()
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)
    st = settings()
    dark = True if args.dark else False if args.light else setting_bool(st, "theme/dark", True)
    apply_theme(dark)
    w = MainWindow(args.socket, dark)
    w.start()
    if not (args.hidden and w.tray is not None):
        w.show()
    if w.tray is None:
        app.setQuitOnLastWindowClosed(True)
    signal.signal(signal.SIGINT, lambda *a: w.quit())
    pump = QTimer()
    pump.start(250)                       # let Python handle SIGINT while Qt runs
    pump.timeout.connect(lambda: None)
    app.aboutToQuit.connect(lambda: w.link.stop() if w.link.thread.isRunning() else None)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
