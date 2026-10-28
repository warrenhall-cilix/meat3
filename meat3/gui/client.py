"""Daemon socket client for the meat³ GUI.

Two layers:

* `DaemonClient` - plain blocking JSON-lines client (no Qt). Short socket timeout,
  reconnects lazily, raises `DaemonError` on any transport failure. Usable from scripts.
* `PollThread` (QThread) + `DaemonLink` (QObject living in the GUI thread). The thread owns
  the socket: it polls `snapshot` + `osd_get` at the configured interval and executes queued
  one-off requests (apply, reset_stats, osd_set, ...) in FIFO order between polls. Results
  come back to the GUI thread as queued signals, so the UI never blocks on the socket.
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import socket
import threading
import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot

DEFAULT_SOCKET = "/run/meat3/meat3.sock"
MAX_LINE = 32 * 1024 * 1024


def default_socket_path() -> str:
    return os.environ.get("MEAT3_SOCKET") or DEFAULT_SOCKET


class DaemonError(Exception):
    pass


class DaemonClient:
    def __init__(self, path: str, timeout: float = 2.0):
        self.path = path
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self.buf = b""
        self.lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def connect(self) -> None:
        self.close()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self.path)
        except OSError as e:
            s.close()
            if isinstance(e, FileNotFoundError):
                raise DaemonError(f"socket {self.path} not found") from e
            if isinstance(e, PermissionError):
                raise DaemonError(f"permission denied on {self.path} (are you in group 'meat3'?)") from e
            raise DaemonError(f"cannot connect to {self.path}: {e.strerror or e}") from e
        self.sock = s
        self.buf = b""

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.buf = b""

    def _readline(self) -> bytes:
        assert self.sock is not None
        deadline = time.monotonic() + self.timeout
        while b"\n" not in self.buf:
            if time.monotonic() > deadline:
                raise DaemonError("timeout waiting for daemon response")
            chunk = self.sock.recv(262144)
            if not chunk:
                raise DaemonError("daemon closed the connection")
            self.buf += chunk
            if len(self.buf) > MAX_LINE:
                raise DaemonError("response too large")
        line, _, self.buf = self.buf.partition(b"\n")
        return line

    def call(self, req: dict) -> dict:
        with self.lock:
            if self.sock is None:
                self.connect()
            try:
                self.sock.sendall((json.dumps(req, separators=(",", ":")) + "\n").encode())
                line = self._readline()
                resp = json.loads(line)
            except DaemonError:
                self.close()
                raise
            except (OSError, ValueError) as e:  # socket.timeout is an OSError
                self.close()
                raise DaemonError(f"{type(e).__name__}: {e}") from e
            if not isinstance(resp, dict):
                raise DaemonError("malformed response")
            return resp


class PollThread(QThread):
    connected = Signal(dict)          # hello response
    disconnected = Signal(str)        # reason; emitted on every failed (re)connect attempt
    snapshot = Signal(dict)
    osd_state = Signal(dict)
    reply = Signal(int, dict)

    def __init__(self, path: str, interval: float = 1.0, retry: float = 2.0, parent=None):
        super().__init__(parent)
        self.client = DaemonClient(path)
        self.interval = interval
        self.retry = retry
        self.paused = False
        self.q: "queue.Queue[tuple[int, dict]]" = queue.Queue()
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._ids = itertools.count(1)
        self.is_connected = False

    # thread-safe API -------------------------------------------------
    def submit(self, req: dict) -> int:
        rid = next(self._ids)
        self.q.put((rid, req))
        return rid

    def retry_now(self) -> None:
        self._kick.set()
        self.q.put((0, {}))   # wake the queue wait as well

    def stop(self) -> None:
        self._stop.set()
        self.retry_now()
        self.wait(3000)
        self.client.close()

    # thread body ------------------------------------------------------
    def _fail_pending(self, why: str) -> None:
        while True:
            try:
                rid, req = self.q.get_nowait()
            except queue.Empty:
                return
            if rid:
                self.reply.emit(rid, {"ok": False, "msg": f"daemon not running ({why})"})

    def _lost(self, why: str) -> None:
        self.client.close()
        if self.is_connected:
            self.is_connected = False
        self.disconnected.emit(why)

    def run(self) -> None:
        next_poll = 0.0
        while not self._stop.is_set():
            if not self.is_connected:
                try:
                    hello = self.client.call({"cmd": "hello"})
                    self.is_connected = True
                    self.connected.emit(hello)
                    next_poll = 0.0
                except DaemonError as e:
                    self._lost(str(e))
                    self._fail_pending(str(e))
                    self._kick.wait(self.retry)
                    self._kick.clear()
                    continue
            # wait for a request or the next poll
            timeout = max(0.0, next_poll - time.monotonic())
            batch = []
            try:
                batch.append(self.q.get(timeout=timeout))
                while True:
                    batch.append(self.q.get_nowait())
            except queue.Empty:
                pass
            pending = [b for b in batch if b[0]]
            try:
                while pending:
                    rid, req = pending[0]
                    resp = self.client.call(req)
                    pending.pop(0)
                    self.reply.emit(rid, resp)
                if time.monotonic() >= next_poll and not self._stop.is_set():
                    next_poll = time.monotonic() + self.interval
                    if not self.paused:
                        snap = self.client.call({"cmd": "snapshot"})
                        if snap.get("ok", True):
                            self.snapshot.emit(snap)
                    osd = self.client.call({"cmd": "osd_get"})
                    if osd.get("ok", True):
                        self.osd_state.emit(osd)
            except DaemonError as e:
                for rid, _ in pending:
                    self.reply.emit(rid, {"ok": False, "msg": f"daemon not reachable ({e})"})
                self._lost(str(e))
                self._fail_pending(str(e))


class DaemonLink(QObject):
    """GUI-thread facade: async requests with callbacks, connection state signals."""

    connected = Signal(dict)
    disconnected = Signal(str)
    snapshot = Signal(dict)
    osd_state = Signal(dict)

    def __init__(self, path: str, interval: float = 1.0, parent=None):
        super().__init__(parent)
        self.path = path
        self.hello: dict = {}
        self.is_connected = False
        self.last_error = ""
        self.osd: dict = {"enabled": False, "items": [], "hotkey": ""}
        self._cbs: dict[int, Callable[[dict], None]] = {}
        self.thread = PollThread(path, interval)
        self.thread.connected.connect(self._on_connected)
        self.thread.disconnected.connect(self._on_disconnected)
        self.thread.snapshot.connect(self.snapshot)
        self.thread.osd_state.connect(self._on_osd)
        self.thread.reply.connect(self._on_reply)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.thread.stop()

    def set_interval(self, seconds: float) -> None:
        self.thread.interval = max(0.1, float(seconds))

    def set_paused(self, paused: bool) -> None:
        self.thread.paused = paused

    def retry(self) -> None:
        self.thread.retry_now()

    def request(self, req: dict, callback: Optional[Callable[[dict], None]] = None) -> int:
        rid = self.thread.submit(req)
        if callback is not None:
            self._cbs[rid] = callback
        return rid

    @Slot(dict)
    def _on_connected(self, hello: dict) -> None:
        self.hello = hello
        self.is_connected = True
        self.last_error = ""
        self.connected.emit(hello)

    @Slot(str)
    def _on_disconnected(self, why: str) -> None:
        self.is_connected = False
        self.last_error = why
        self.disconnected.emit(why)

    @Slot(dict)
    def _on_osd(self, osd: dict) -> None:
        self.osd = osd
        self.osd_state.emit(osd)

    @Slot(int, dict)
    def _on_reply(self, rid: int, resp: dict) -> None:
        cb = self._cbs.pop(rid, None)
        if cb is not None:
            cb(resp)
