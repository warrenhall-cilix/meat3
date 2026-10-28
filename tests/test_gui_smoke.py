"""Offscreen GUI smoke test against the mock daemon.

    QT_QPA_PLATFORM=offscreen python3 tests/test_gui_smoke.py      (or: pytest tests/)

Starts meat3.core.mockd on a temp socket, opens the main window offscreen, waits for polls,
checks the sensor tree, applies controls through the Overclock tab code path, exercises
profiles, OSD toggling/items, MangoHud generation, CSV logging, the daemon-absent banner,
and saves screenshots to docs/screenshots/.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHOTS = Path(os.environ.get("MEAT3_SCREENSHOTS", ROOT / "docs" / "screenshots"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _wait(app, cond, timeout=10.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError(f"timeout waiting for {what}")


def _pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def test_gui_smoke():
    tmp = Path(tempfile.mkdtemp(prefix="meat3-gui-"))
    os.environ["XDG_CONFIG_HOME"] = str(tmp / "config")   # keep the user's ~/.config clean
    sock = str(tmp / "mock.sock")
    osd_file = str(tmp / "osd.txt")
    mock = subprocess.Popen([sys.executable, "-m", "meat3.core.mockd", "--socket", sock,
                             "--osd-file", osd_file, "--rate", "0.5"], cwd=str(ROOT))
    try:
        _run(tmp, sock, osd_file, mock)
    finally:
        if mock.poll() is None:
            mock.terminate()
            mock.wait(5)
        shutil.rmtree(tmp, ignore_errors=True)


def _run(tmp: Path, sock: str, osd_file: str, mock):
    from PySide6.QtWidgets import QApplication

    from meat3.gui.app import MainWindow
    from meat3.gui.common import apply_theme, theme

    from meat3.gui.common import install_none_refcount_guard

    app = QApplication.instance() or QApplication(["meat3-test"])
    install_none_refcount_guard()
    apply_theme(True)
    t0 = time.monotonic()
    while not os.path.exists(sock):
        assert time.monotonic() - t0 < 10, "mockd did not start"
        time.sleep(0.05)

    w = MainWindow(sock, dark=True)
    w.resize(1280, 860)
    w.start()
    w.show()
    try:
        _exercise(app, w, tmp, sock, osd_file, mock)
    finally:
        w.quit()


def _exercise(app, w, tmp, sock, osd_file, mock):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPixmap
    from PySide6.QtWidgets import QAbstractItemView

    from meat3.gui.sensors import SENSOR_ID_ROLE
    SHOTS.mkdir(parents=True, exist_ok=True)

    # --- sensors tree populated and updating in place
    _wait(app, lambda: w.store.polls >= 3, 15, "3 polls")
    model = w.sensors.model
    devices = [n.name for n in model.root.children]
    assert len(devices) == 4, devices
    assert any("RTX 5080" in d for d in devices) and any("9070 XT" in d for d in devices)
    assert len(model.leaves) == len(w.store.sensors) > 100
    leaf = model.leaves["nv0.clk.gpc"]
    idx = model.createIndex(leaf.row, 1, leaf)
    first_text = model.data(idx)
    assert first_text.endswith("MHz"), first_text
    none_leaf = model.leaves["nv0.volt.msvdd_limit"]
    assert model.data(model.createIndex(none_leaf.row, 1, none_leaf)) == "—"

    # collapse one device; it must stay collapsed and rows must stay stable across polls
    dev0 = w.sensors.proxy.index(0, 0)
    w.sensors.tree.collapse(dev0)
    rows_before = model.rowCount()
    polls = w.store.polls
    hist_len = len(w.store.history["nv0.clk.gpc"])
    _wait(app, lambda: w.store.polls >= polls + 3, 10, "more polls")
    assert not w.sensors.tree.isExpanded(w.sensors.proxy.index(0, 0)), "expansion state lost"
    assert model.rowCount() == rows_before
    assert len(w.store.history["nv0.clk.gpc"]) > hist_len
    changed = sum(1 for sid in ("nv0.pwr.raw", "nv0.conn.pin1.a", "cpu.pwr.package", "nv0.volt.nvvdd")
                  if len({v for _, v in w.store.history[sid]}) > 1)
    assert changed >= 2, "values are not updating"
    w.sensors.tree.expand(w.sensors.proxy.index(0, 0))

    # filter
    w.sensors.search.setText("pin 3")
    _pump(app, 0.1)
    assert w.sensors.proxy.rowCount() >= 1
    w.sensors.search.setText("")
    w.store.set_favourite("nv0.clk.gpc", True)
    w.store.set_favourite("nv0.conn.pin3.a", True)
    w.sensors.act_fav.setChecked(True)
    _pump(app, 0.1)
    fav_rows = []

    def walk(parent):
        for r in range(w.sensors.proxy.rowCount(parent)):
            i = w.sensors.proxy.index(r, 0, parent)
            sid = w.sensors.proxy.data(i, SENSOR_ID_ROLE)
            if sid:
                fav_rows.append(sid)
            walk(i)
    walk(w.sensors.proxy.index(-1, -1))
    assert sorted(fav_rows) == ["nv0.clk.gpc", "nv0.conn.pin3.a"], fav_rows
    w.sensors.act_fav.setChecked(False)

    # right-click menu: build it for a sensor row and trigger "Add to OSD" / favourites
    tgt = model.leaves["nv0.temp.hotspot"]
    w.sensors.tree.scrollTo(w.sensors.proxy.mapFromSource(model.createIndex(tgt.row, 0, tgt)))
    _pump(app, 0.1)
    rect = w.sensors.tree.visualRect(w.sensors.proxy.mapFromSource(model.createIndex(tgt.row, 0, tgt)))
    menu = w.sensors.build_menu(rect.center())
    seen = {"actions": {x.text(): x for x in menu.actions()}}
    acts = seen["actions"]
    assert {"Add to OSD", "Add to favourites", "Copy value", "Show graph…"} <= set(acts), list(acts)
    acts["Add to favourites"].trigger()
    assert "nv0.temp.hotspot" in w.store.favourites
    acts["Copy value"].trigger()
    assert app.clipboard().text().endswith("°C")

    # reset stats round trip
    got = {}
    w.link.request({"cmd": "reset_stats"}, lambda r: got.setdefault("r", r))
    _wait(app, lambda: "r" in got, 5, "reset_stats reply")
    assert got["r"]["ok"]

    # CSV logging
    csv_path = tmp / "log.csv"
    w.sensors.start_logging(str(csv_path))
    p = w.store.polls
    _wait(app, lambda: w.store.polls >= p + 2, 10, "log polls")
    w.sensors.act_log.setChecked(False)
    rows = list(csv.reader(open(csv_path, encoding="utf-8")))
    assert rows[0][2:] == w.store.log_ids and len(rows) >= 5, len(rows)

    # screenshot: sensors (scroll so the GPU connector + throttle section is visible too)
    w.tabs.setCurrentIndex(0)
    _pump(app, 0.3)
    w.grab().save(str(SHOTS / "sensors-dark.png"))
    pin = model.leaves["nv0.conn.pin3.a"]
    w.sensors.tree.scrollTo(w.sensors.proxy.mapFromSource(model.createIndex(pin.row, 0, pin)),
                            QAbstractItemView.PositionAtCenter)
    _pump(app, 0.2)
    w.grab().save(str(SHOTS / "sensors-dark-gpu.png"))

    # sparkline popup
    _pump(app, 1.0)
    g = w.sensors.show_graph("nv0.pwr.real")
    _wait(app, lambda: len(w.store.history["nv0.pwr.real"]) > 8, 10, "history")
    g.update()
    _pump(app, 0.2)
    g.grab().save(str(SHOTS / "sparkline.png"))
    g.close()

    # --- graphs tab: Overview preset on first run, presets, merge, shared hover, persistence
    from meat3.gui.graphs import preset_ids
    w.tabs.setCurrentIndex(w.tabs.indexOf(w.graphs))
    gp = w.graphs
    _wait(app, lambda: len(gp.cards) >= 4, 5, "overview graphs")
    first = [c.ids[0] for c in gp.cards]
    assert set(first) == set(preset_ids("Overview", w.store.sensors)), first
    n0 = len(gp.cards)
    assert any(i.endswith(".core0.stall_be") for i in preset_ids("CPU stalls", w.store.sensors))
    gp.add_preset("CPU cores")
    assert len(gp.cards) == n0 + 8, len(gp.cards)
    temps = [c for c in gp.cards if c.unit() == "°C"]
    assert len(temps) >= 2, [c.ids for c in gp.cards]
    assert gp.merge(temps[1], temps[0]) and len(temps[0].ids) == 2
    watts = next(c for c in gp.cards if c.unit() == "W")
    assert not gp.merge(watts, temps[0])                       # different units stay apart
    sid = next(i for i, s_ in w.store.sensors.items() if s_.get("unit") == "V")
    w.sensors.graph_toggle_requested.emit(sid, True)
    assert sid in gp.ids() and sid in w.sensors.graph_ids()
    w.sensors.graph_toggle_requested.emit(sid, False)
    assert sid not in gp.ids()
    for k in range(8):                                          # drop the per-core cards again
        gp.remove_card(gp.cards[-1])
    gp.split(temps[0])
    gp.merge(gp.cards[gp.cards.index(temps[0]) + 1], temps[0])
    _wait(app, lambda: all(len(w.store.history.get(i, ())) > 6 for i in gp.ids()), 10, "graph history")
    gp.set_hover(w.store.t - 3)
    _pump(app, 0.3)
    w.grab().save(str(SHOTS / "graphs-dark.png"))
    gp.set_hover(None)
    import json as _json
    from meat3.gui.common import settings as _settings
    assert _json.loads(_settings().value("graphs/cards")) == [c.ids for c in gp.cards]
    gp.cmb_window.setCurrentIndex(0)
    assert gp.window_s == 60

    # --- cores tab: one table per CPU, 8 rows, voltage + temp columns present, values filled
    w.tabs.setCurrentIndex(w.tabs.indexOf(w.cores))
    _pump(app, 1.2)
    cp = w.cores
    assert len(cp.tables) == 1, list(cp.tables)
    (pfx, tbl), = cp.tables.items()
    hdrs = [tbl.horizontalHeaderItem(i).text() for i in range(tbl.columnCount())]
    assert tbl.rowCount() == 8 and {"Eff clock", "Volt", "Temp", "Power", "Usage"} <= set(hdrs), hdrs
    assert {"IPC", "Back-end", "Front-end", "SMT"} <= set(hdrs), hdrs
    assert tbl.item(0, hdrs.index("Back-end")).text().endswith("%"), tbl.item(0, hdrs.index("Back-end")).text()
    assert tbl.item(0, hdrs.index("Volt")).text().endswith(" V"), tbl.item(0, hdrs.index("Volt")).text()
    assert tbl.item(7, hdrs.index("Temp")).text().endswith("°C")
    vid = cp.layout_ids[pfx][0][2]
    cp.osd_toggle_requested.emit(vid, True)
    _pump(app, 0.3)
    assert vid in w.osd.items
    cp.osd_toggle_requested.emit(vid, False)
    _pump(app, 1.2)
    at = cp.app_table
    assert at is not None and at.rowCount() == 3, at and at.rowCount()
    assert at.item(0, 0).text() == "Cyberpunk2077.exe", at.item(0, 0).text()
    ah = [at.horizontalHeaderItem(i).text() for i in range(at.columnCount())]
    assert at.item(0, ah.index("IPC")).text() not in ("", "—"), at.item(0, ah.index("IPC")).text()
    assert at.item(0, ah.index("Back-end")).text().endswith("%")
    assert "Game" in cp.app_title.text() and "Cyberpunk2077.exe" in cp.app_title.text()
    assert "L3 reserved" in cp.app_title.text() and "everything else 12 MB" in cp.app_title.text(), cp.app_title.text()
    w.grab().save(str(SHOTS / "cores-dark.png"))

    # --- overclock tab: apply through the row code path
    w.tabs.setCurrentIndex(w.tabs.indexOf(w.overclock))
    oc = w.overclock
    _wait(app, lambda: "nv0.oc.core_offset" in oc.rows, 5, "OC rows")
    assert set(oc.boxes) == {"GPU 0: NVIDIA GeForce RTX 5080", "GPU 1: AMD Radeon RX 9070 XT",
                             "CPU: AMD Ryzen 7 9800X3D", "Board: ASUS ROG STRIX X870E-E"}
    row = oc.rows["nv0.oc.core_offset"]
    row.set_value(150)
    assert row.dirty() and oc.btn_apply_all.isEnabled()
    assert oc.apply_rows([row])
    ctrl = lambda cid: next(c for c in w.store.controls if c["id"] == cid)
    _wait(app, lambda: ctrl("nv0.oc.core_offset")["value"] == 150, 5, "core offset applied")
    _wait(app, lambda: not oc.rows["nv0.oc.core_offset"].dirty(), 5, "row clean")
    assert "150 MHz" in oc.rows["nv0.oc.core_offset"].current.text()

    # unavailable control is disabled and shows its hint
    fan = oc.rows["nv0.oc.fan_speed"]
    assert not fan.editor.isEnabled() and fan.hint.isVisibleTo(oc) and "manual" in fan.hint.text()

    # danger=2 needs confirmation
    asked = []
    oc.confirm = lambda title, text: (asked.append(title), False)[1]
    oc.rows["nv0.oc.nvvdd_max"].set_value(1.05)
    assert not oc.apply_all()
    assert asked == ["Dangerous setting"]
    oc.confirm = lambda title, text: True
    oc.rows["amd1.oc.power_cap"].set_value(320)
    oc.rows["nv0.oc.fan_mode"].set_value("manual")
    assert oc.apply_all()
    _wait(app, lambda: ctrl("nv0.oc.nvvdd_max")["value"] == 1.05 and ctrl("amd1.oc.power_cap")["value"] == 320
          and ctrl("nv0.oc.fan_mode")["value"] == "manual", 5, "apply all")
    _wait(app, lambda: oc.rows["nv0.oc.fan_speed"].editor.isEnabled(), 5, "fan speed enabled")
    assert "OK" in oc.log.toPlainText()

    # an out-of-range apply is refused by the daemon and logged as FAIL
    assert not oc.apply_values([(ctrl("cpu.oc.boost_override"), 100)])   # unavailable: not sent
    oc.apply_values([(ctrl("amd1.oc.power_cap"), 5000)])                   # daemon must refuse
    _wait(app, lambda: "FAIL" in oc.log.toPlainText(), 5, "refusal logged")

    # profiles
    pth = oc.save_profile("smoke test")
    assert pth and json.loads(pth.read_text())["nv0.oc.core_offset"] == 150
    oc.rows["nv0.oc.core_offset"].set_value(0)
    oc.load_profile("smoke test")   # fills editors only (returns pending-change count)
    assert oc.rows["nv0.oc.core_offset"].value() == 150
    oc.revert_all()
    # profile application path
    data = json.loads(pth.read_text())
    data["nv0.oc.mem_offset"] = 500
    pth.write_text(json.dumps(data))
    assert oc.apply_profile("smoke test")
    _wait(app, lambda: ctrl("nv0.oc.mem_offset")["value"] == 500, 5, "profile applied")

    # leave a pending (unapplied) edit visible for the screenshot
    oc.rows["cpu.oc.co_all"].set_value(-20)
    _pump(app, 0.3)
    w.grab().save(str(SHOTS / "overclock-dark.png"))

    # reset to stock
    assert oc.reset_device("GPU 0: NVIDIA GeForce RTX 5080")
    _wait(app, lambda: ctrl("nv0.oc.core_offset")["value"] == 0 and ctrl("nv0.oc.nvvdd_max")["value"] == 1.095,
          5, "reset to stock")
    oc.revert_all()

    # --- OSD
    w.tabs.setCurrentIndex(w.tabs.indexOf(w.osd))
    _wait(app, lambda: w.link.osd.get("items"), 5, "osd_get")
    assert w.link.osd["hotkey"] == "KEY_RIGHTCTRL+KEY_F12"
    assert w.overlay.isVisible(), "overlay should follow daemon enabled=True"
    w.osd.add_ids(["nv0.conn.pin3.a", "nv0.throttle.power"])
    _wait(app, lambda: "nv0.conn.pin3.a" in (w.link.osd.get("items") or []), 5, "osd items set")
    assert w.osd.list.count() == len(w.osd.items)
    # reorder like a drag would: move last item to the top
    w.osd.list.setCurrentRow(w.osd.list.count() - 1)
    w.osd.list.item(w.osd.list.count() - 1).setSelected(True)
    for _ in range(w.osd.list.count() - 1):
        w.osd.move_selected(-1)
    _wait(app, lambda: (w.link.osd.get("items") or [None])[0] == "nv0.throttle.power", 5, "reorder")
    w.osd.move_selected(1)
    _pump(app, 0.8)
    lines = Path(osd_file).read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(w.osd.items) and ": " in lines[0], lines

    # MangoHud config + run its exec lines in /bin/sh against the mock's osd.txt
    w.osd.mh_path.setText(str(tmp / "MangoHud.conf"))
    out = w.osd.generate_mangohud()
    conf = Path(out).read_text(encoding="utf-8")
    assert "legacy_layout=0" in conf and "toggle_hud=Control_R+F12" in conf
    execs = [l[5:] for l in conf.splitlines() if l.startswith("exec=")]
    assert len(execs) == len(w.osd.items)
    for l in conf.splitlines():
        if not l.startswith("#"):
            assert "#" not in l, l
    res = subprocess.run(["/bin/sh"], input="\n".join(execs) + "\n", capture_output=True, text=True, timeout=5)
    vals = res.stdout.splitlines()
    assert len(vals) == len(execs) and all(v.strip() for v in vals), vals

    w.osd.avail.expandAll()
    _pump(app, 0.3)
    w.grab().save(str(SHOTS / "osd-dark.png"))

    # overlay screenshot composited over a fake game frame
    w.overlay.refresh()
    _pump(app, 0.2)
    ov = w.overlay.grab()
    bg = QPixmap(ov.width() + 260, ov.height() + 120)
    p = QPainter(bg)
    grad = QLinearGradient(QPointF(0, 0), QPointF(bg.width(), bg.height()))
    grad.setColorAt(0, QColor("#6f8fb5"))
    grad.setColorAt(0.55, QColor("#c9b48a"))
    grad.setColorAt(1, QColor("#3b4a2f"))
    p.fillRect(bg.rect(), grad)
    p.drawPixmap(18, 18, ov)
    p.end()
    bg.save(str(SHOTS / "overlay.png"))

    # toggle OSD -> overlay hides; toggle again -> shows
    w.toggle_osd()
    _wait(app, lambda: w.link.osd.get("enabled") is False, 5, "osd disabled")
    _wait(app, lambda: not w.overlay.isVisible(), 5, "overlay hidden")
    assert Path(osd_file).read_text() == ""
    w.toggle_osd()
    _wait(app, lambda: w.link.osd.get("enabled") is True and w.overlay.isVisible(), 5, "osd re-enabled")

    # light theme screenshot
    w.theme_actions["light"].trigger()
    w.tabs.setCurrentIndex(0)
    _pump(app, 1.2)
    w.grab().save(str(SHOTS / "sensors-light.png"))
    w.tabs.setCurrentIndex(w.tabs.indexOf(w.graphs))
    _pump(app, 0.5)
    w.grab().save(str(SHOTS / "graphs-light.png"))
    w.tabs.setCurrentIndex(w.tabs.indexOf(w.overclock))
    _pump(app, 0.3)
    w.grab().save(str(SHOTS / "overclock-light.png"))
    # Liquid Ass theme screenshots (every tab)
    from meat3.gui.common import theme
    w.theme_actions["liquid"].trigger()
    assert theme().glass and w.theme_actions["liquid"].isChecked()
    for page, nm in ((w.sensors, "sensors"), (w.graphs, "graphs"), (w.cores, "cores"),
                     (w.overclock, "overclock"), (w.osd, "osd")):
        w.tabs.setCurrentIndex(w.tabs.indexOf(page))
        _pump(app, 0.6)
        w.grab().save(str(SHOTS / f"{nm}-liquid.png"))
    w.theme_actions["dark"].trigger()
    assert not theme().glass
    w.tabs.setCurrentIndex(0)

    # --- hotkey picker: typed combo -> daemon -> label
    from meat3.gui.osd import HotkeyDialog, pretty_hotkey
    hd = HotkeyDialog("KEY_RIGHTCTRL+KEY_F12")
    hd._typed("leftalt+o")
    assert hd.keys == ["KEY_LEFTALT", "KEY_O"] and hd.ok.isEnabled()
    assert "Alt_L+o" in hd.err.text()
    hd._typed("leftalt")
    assert not hd.ok.isEnabled()
    assert pretty_hotkey("KEY_RIGHTCTRL+KEY_F12") == "Right Ctrl + F12"
    got = {}
    w.link.request({"cmd": "osd_set", "hotkey": ["KEY_LEFTALT", "KEY_O"]}, lambda r: got.setdefault("r", r))
    _wait(app, lambda: "r" in got, 5, "osd_set hotkey")
    assert got["r"]["ok"], got
    _wait(app, lambda: "Left Alt + O" in w.osd.hotkey_lbl.text(), 5, "hotkey label follows daemon")

    # --- updates: status-bar button and dialog (mockd reports 2 new commits)
    w._poll_updates()
    _wait(app, lambda: w.upd_btn.isVisible(), 5, "update button")
    assert w.upd_btn.text() == "⬆ 2 updates available", w.upd_btn.text()
    w.show_updates()
    dlg = w._upd_dlg
    _wait(app, lambda: "2 updates available" in dlg.head.text(), 5, "update dialog status")
    assert "mock change one" in dlg.text.toPlainText()
    assert dlg.btn_update.isEnabled()
    dlg.btn_update.click()
    _wait(app, lambda: "failed" in dlg.head.text().lower(), 5, "mock refuses update")
    dlg.close()

    # --- daemon goes away: banner, '—'-safe, and recovery
    mock.terminate()
    mock.wait(5)
    _wait(app, lambda: w.banner_wrap.isVisible(), 10, "daemon-not-running banner")
    assert not w.overlay.isVisible()
    failed = {}
    w.link.request({"cmd": "apply", "id": "nv0.oc.core_offset", "value": 5}, lambda r: failed.setdefault("r", r))
    _wait(app, lambda: "r" in failed, 5, "failed request callback")
    assert failed["r"]["ok"] is False
    _pump(app, 0.3)
    w.grab().save(str(SHOTS / "daemon-absent.png"))

    mock2 = subprocess.Popen([sys.executable, "-m", "meat3.core.mockd", "--socket", sock,
                              "--osd-file", osd_file, "--rate", "0.5"], cwd=str(ROOT))
    try:
        w.link.retry()
        _wait(app, lambda: w.link.is_connected and not w.banner_wrap.isVisible(), 15, "reconnect")
        p = w.store.polls
        _wait(app, lambda: w.store.polls > p, 5, "poll after reconnect")
    finally:
        w.quit()
        mock2.terminate()
        mock2.wait(5)
    print(f"GUI smoke test OK; screenshots in {SHOTS}")


if __name__ == "__main__":
    test_gui_smoke()
