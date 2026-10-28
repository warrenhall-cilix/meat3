"""Desktop overlay in its own XWayland process, so it can sit in a screen corner on Wayland.

Wayland doesn't let a client place its own window, so KWin (and others) centre the overlay. An X11
window may position itself, and every Plasma/GNOME Wayland session runs XWayland, so on Wayland the
GUI runs the overlay as a child process with QT_QPA_PLATFORM=xcb.

    parent (GUI, native Wayland)  --stdin JSON lines-->  child (xcb): OverlayWindow
                                                          reads sensor values from meat3d itself

Messages: {"items": [...]}, {"style": {...}}, {"visible": bool}. The child exits when stdin closes.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment

STYLE_KEYS = ("corner", "font_size", "opacity", "text_color", "label_color", "bg_color", "rate_ms",
              "desktop", "margin")


def want_separate_process(platform_name: str) -> bool:
    """True on a Wayland session that also has XWayland (DISPLAY set)."""
    return (platform_name == "wayland" and bool(os.environ.get("DISPLAY"))
            and os.environ.get("MEAT3_OVERLAY_INPROCESS") != "1")


class OverlayProxy(QObject):
    """Same interface the GUI uses on OverlayWindow, backed by the child process."""

    def __init__(self, store, style, socket_path: str, parent=None):
        super().__init__(parent)
        self.style_ = style
        self.socket_path = socket_path
        self.items: list[str] = []
        self.visible = False
        self.restarts = 0
        self.proc = None
        self._start()

    def _start(self):
        p = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("QT_QPA_PLATFORM", "xcb")
        p.setProcessEnvironment(env)
        p.setProcessChannelMode(QProcess.ForwardedChannels)
        p.finished.connect(self._finished)
        p.start(sys.executable, ["-m", "meat3.gui.overlay_proc", "--socket", self.socket_path])
        self.proc = p
        self.apply_style()
        self._send({"items": self.items})
        self._send({"visible": self.visible})

    def _finished(self, *_):
        if self.proc is None:
            return
        self.proc = None
        if self.restarts < 3:              # crashed: try again, but don't loop forever
            self.restarts += 1
            self._start()

    def _send(self, msg: dict):
        if self.proc is not None:
            self.proc.write((json.dumps(msg) + "\n").encode())

    def apply_style(self):
        self._send({"style": {k: getattr(self.style_, k, None) for k in STYLE_KEYS}})

    def set_items(self, items):
        if list(items) != self.items:
            self.items = list(items)
            self._send({"items": self.items})

    def refresh(self):
        pass                                 # the child polls the daemon itself

    def show(self):
        self.visible = True
        self._send({"visible": True})

    def hide(self):
        self.visible = False
        self._send({"visible": False})

    def isVisible(self) -> bool:
        return self.visible

    def close(self):
        p, self.proc = self.proc, None
        if p is not None:
            p.closeWriteChannel()
            if not p.waitForFinished(1500):
                p.kill()


def main(argv=None) -> int:
    import argparse

    from PySide6.QtCore import QSocketNotifier
    from PySide6.QtWidgets import QApplication

    from meat3.gui.client import DaemonLink, default_socket_path
    from meat3.gui.common import SensorStore, install_none_refcount_guard
    from meat3.gui.osd import OverlayWindow

    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default=default_socket_path())
    args = ap.parse_args(argv)
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    install_none_refcount_guard()
    style = SimpleNamespace(corner="top-left", font_size=13, opacity=55, text_color="#ffffff",
                            label_color="#aeb6c4", bg_color="#0b0c0f", rate_ms=1000, desktop=True, margin=18)
    store = SensorStore(app)
    link = DaemonLink(args.socket, interval=1.0, parent=app)
    link.snapshot.connect(store.ingest)
    win = OverlayWindow(store, style)
    buf = bytearray()

    def on_stdin():
        chunk = os.read(0, 65536)
        if not chunk:                        # parent gone
            app.quit()
            return
        buf.extend(chunk)
        while b"\n" in buf:
            line, _, rest = bytes(buf).partition(b"\n")
            buf[:] = rest
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "style" in msg:
                for k, v in msg["style"].items():
                    if k in STYLE_KEYS and v is not None:
                        setattr(style, k, v)
                link.set_interval(min(1.0, style.rate_ms / 1000))
                win.apply_style()
            if "items" in msg:
                win.set_items(msg["items"])
            if "visible" in msg:
                if msg["visible"]:
                    win.refresh()
                    win.show()
                else:
                    win.hide()

    sn = QSocketNotifier(0, QSocketNotifier.Read, app)
    sn.activated.connect(on_stdin)
    link.start()
    rc = app.exec()
    link.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
