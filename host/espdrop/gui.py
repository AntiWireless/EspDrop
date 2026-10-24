# EspDrop - the desktop app (PySide6) on top of the Session.
#
#   espdrop gui [-p PORT]          (Windows: .\tools\espdrop-gui.cmd)
#
# One window: the ESP's state, a receive window that closes by itself (like
# Apple's "Everyone for 10 minutes"), the receivers a search found with their
# names, sending by button or by dropping files on a receiver, every transfer
# with its progress, and a dialog that asks before anything is received. It
# keeps running in the tray when the window is closed.
#
# Session events arrive on worker threads; a Qt signal carries them to the GUI
# thread. Session calls that talk to the device run on a short-lived thread so
# a slow device never freezes the window.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import os
import sys
import threading
import time

from PySide6.QtCore import QObject, QPoint, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QFont, QIcon, QPainter,
                           QPixmap, QPolygon)
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QSystemTrayIcon, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from .session import Session

APP = "EspDrop"
WINDOWS = {"10 Minuten": 600, "1 Stunde": 3600, "dauerhaft": None}

SEND_STATES = {"new": "wartet", "asking": "wartet auf Annehmen", "uploading": "sendet",
               "done": "gesendet", "declined": "abgelehnt", "failed": "fehlgeschlagen",
               "cancelled": "abgebrochen"}


def app_icon():
    """A drawn icon (no image files to ship): a blue tile with a white drop."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#1f6feb"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(2, 2, 60, 60, 14, 14)
    p.setBrush(QColor("white"))
    p.drawEllipse(18, 26, 28, 28)
    p.drawPolygon(QPolygon([QPoint(32, 8), QPoint(20, 34), QPoint(44, 34)]))
    p.end()
    return QIcon(pm)


def run_bg(fn, *args, on_error=None):
    """Run a session call off the GUI thread."""
    def run():
        try:
            fn(*args)
        except Exception as e:                 # noqa: BLE001 - shown, not fatal
            if on_error:
                on_error(str(e))
    threading.Thread(target=run, daemon=True).start()


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


class Bridge(QObject):
    """Session callbacks (any thread) -> Qt signals (GUI thread)."""
    event = Signal(str, object)
    ask = Signal(object, object)               # IncomingAsk, reply dict

    def __init__(self, ask_timeout):
        super().__init__()
        self.ask_timeout = ask_timeout

    def on_event(self, kind, **info):
        self.event.emit(kind, info)

    def on_ask(self, ask):
        reply = {"done": threading.Event(), "ok": False}
        self.ask.emit(ask, reply)
        reply["done"].wait(self.ask_timeout + 1)
        return reply["ok"]


class AskDialog(QDialog):
    """'Bens iPhone möchte 2 Dateien senden' - Annehmen / Ablehnen, with a countdown."""

    def __init__(self, parent, ask, reply, timeout):
        super().__init__(parent)
        self.reply = reply
        self.setWindowTitle("AirDrop")
        self.setWindowFlag(Qt.WindowStaysOnTopHint)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        if ask.icon:
            pm = QPixmap()
            if pm.loadFromData(ask.icon):
                thumb = QLabel()
                thumb.setPixmap(pm.scaled(96, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                top.addWidget(thumb)
        what = ask.files or ask.urls
        noun = ("Link" if len(what) == 1 else "Links") if ask.urls and not ask.files else \
               ("Datei" if len(what) == 1 else "Dateien")
        text = QLabel(f"<b>{_esc(ask.sender)}</b> möchte {len(what) or ''} {noun} senden:"
                      f"<br>{'<br>'.join(_esc(w) for w in what[:8])}"
                      f"{'<br>…' if len(what) > 8 else ''}")
        text.setTextFormat(Qt.RichText)
        text.setWordWrap(True)
        top.addWidget(text, 1)
        lay.addLayout(top)
        self.left = QLabel()
        lay.addWidget(self.left)
        buttons = QDialogButtonBox()
        self.yes = buttons.addButton("Annehmen", QDialogButtonBox.AcceptRole)
        self.no = buttons.addButton("Ablehnen", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.end = time.monotonic() + timeout
        self.timer = QTimer(self, interval=500, timeout=self._tick)
        self.timer.start()
        self._tick()
        self.finished.connect(self._answer)

    def _tick(self):
        left = self.end - time.monotonic()
        if left <= 0:
            self.reject()
            return
        self.left.setText(f"Ohne Antwort abgelehnt in {left:.0f} s")

    def _answer(self, result):
        self.timer.stop()
        self.reply["ok"] = result == QDialog.Accepted
        self.reply["done"].set()


def _esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


class ReceiverList(QListWidget):
    """The receivers; files dropped on one are sent to it."""
    dropped = Signal(str, list)                # receiver id, paths

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        item = self.itemAt(e.position().toPoint())
        if e.mimeData().hasUrls() and item is not None and item.data(Qt.UserRole):
            self.setCurrentItem(item)
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e):
        item = self.itemAt(e.position().toPoint())
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if item is not None and paths:
            self.dropped.emit(item.data(Qt.UserRole), paths)
            e.acceptProposedAction()


class MainWindow(QMainWindow):
    def __init__(self, port=None, settings=None, session_factory=None, ask_timeout=60.0,
                 tray=True):
        super().__init__()
        self.settings = settings or QSettings(APP, APP)
        self.setWindowTitle(APP)
        self.setWindowIcon(app_icon())
        self.resize(720, 640)
        self.port = port or self.settings.value("port", "") or None
        self.bridge = Bridge(ask_timeout)
        self.bridge.event.connect(self._on_event)
        self.bridge.ask.connect(self._on_ask)
        # session_factory(window) -> Session, for tests; it must use window.bridge
        self._make_session = ((lambda: session_factory(self)) if session_factory
                              else self._default_session)
        self._rows = {}                        # ("send", id) / ("recv", n) -> QTreeWidgetItem
        self._recv_n = 0
        self._recv_row = None                  # the row of the transfer being received
        self._quitting = False
        self._build()
        self.tray = self._make_tray() if tray and QSystemTrayIcon.isSystemTrayAvailable() else None
        self.session = self._make_session()
        self.session.start()
        self._update_dir()
        self.clock = QTimer(self, interval=1000, timeout=self._tick)
        self.clock.start()

    # ---- setup ---------------------------------------------------------------

    def _default_session(self):
        d = self.settings.value("download_dir", "") or default_download_dir()
        return Session(port=self.port, name=self.name_edit.text() or APP, receive=False,
                       download_dir=d, on_event=self.bridge.on_event,
                       on_ask=self.bridge.on_ask, ask_timeout=self.bridge.ask_timeout)

    def _build(self):
        root = QWidget()
        lay = QVBoxLayout(root)

        # device and name
        head = QHBoxLayout()
        self.device_label = QLabel("Suche ESP …")
        self.power_label = QLabel("")
        self.power_label.setStyleSheet("color: gray")
        head.addWidget(self.device_label, 1)
        head.addWidget(self.power_label)
        lay.addLayout(head)
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name für andere Geräte:"))
        self.name_edit = QLineEdit(self.settings.value("name", "") or APP)
        self.name_edit.editingFinished.connect(self._name_changed)
        name_row.addWidget(self.name_edit, 1)
        lay.addLayout(name_row)

        # receiving
        rbox = QGroupBox("Empfangen")
        rl = QVBoxLayout(rbox)
        r1 = QHBoxLayout()
        self.receive_btn = QPushButton("Empfangsbereit machen")
        self.receive_btn.setCheckable(True)
        self.receive_btn.toggled.connect(self._receive_toggled)
        self.window_combo = QComboBox()
        self.window_combo.addItems(list(WINDOWS))
        self.window_combo.setCurrentText(self.settings.value("window", "10 Minuten"))
        self.receive_left = QLabel("aus")
        r1.addWidget(self.receive_btn)
        r1.addWidget(self.window_combo)
        r1.addWidget(self.receive_left, 1)
        rl.addLayout(r1)
        r2 = QHBoxLayout()
        self.dir_label = QLabel()
        self.dir_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        pick = QPushButton("Ordner …")
        pick.clicked.connect(self._pick_dir)
        show = QPushButton("Öffnen")
        show.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(self.session.download_dir or os.getcwd())))
        r2.addWidget(QLabel("Ziel:"))
        r2.addWidget(self.dir_label, 1)
        r2.addWidget(pick)
        r2.addWidget(show)
        rl.addLayout(r2)
        lay.addWidget(rbox)

        # sending
        sbox = QGroupBox("Senden")
        sl = QVBoxLayout(sbox)
        s1 = QHBoxLayout()
        self.search_btn = QPushButton("Geräte suchen")
        self.search_btn.clicked.connect(self._search)
        self.search_label = QLabel("Am iPhone/iPad: AirDrop „Für alle“, entsperrt, "
                                   "Teilen-Menü offen.")
        self.search_label.setWordWrap(True)
        s1.addWidget(self.search_btn)
        s1.addWidget(self.search_label, 1)
        sl.addLayout(s1)
        self.receivers_list = ReceiverList()
        self.receivers_list.dropped.connect(self._send_to)
        self.receivers_list.itemDoubleClicked.connect(lambda item: self._send_dialog())
        sl.addWidget(self.receivers_list, 1)
        s2 = QHBoxLayout()
        self.send_btn = QPushButton("Dateien senden …")
        self.send_btn.clicked.connect(self._send_dialog)
        s2.addWidget(QLabel("Dateien auf ein Gerät ziehen oder:"))
        s2.addStretch(1)
        s2.addWidget(self.send_btn)
        sl.addLayout(s2)
        lay.addWidget(sbox, 2)

        # transfers
        tbox = QGroupBox("Übertragungen")
        tl = QVBoxLayout(tbox)
        self.transfers = QTreeWidget()
        self.transfers.setHeaderLabels(["", "Gerät", "Dateien", "Status"])
        self.transfers.setRootIsDecorated(False)
        self.transfers.header().setSectionResizeMode(2, QHeaderView.Stretch)
        self.transfers.setColumnWidth(0, 28)
        self.transfers.setColumnWidth(1, 160)
        self.transfers.itemDoubleClicked.connect(self._open_transfer)
        self.transfers.setContextMenuPolicy(Qt.CustomContextMenu)
        self.transfers.customContextMenuRequested.connect(self._transfer_menu)
        tl.addWidget(self.transfers)
        lay.addWidget(tbox, 2)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setFont(QFont("monospace", 8))
        self.log.setVisible(False)
        lay.addWidget(self.log, 1)

        menu = self.menuBar().addMenu("&EspDrop")
        a = QAction("Protokoll anzeigen", self, checkable=True)
        a.toggled.connect(self.log.setVisible)
        menu.addAction(a)
        a = QAction("Port …", self)
        a.triggered.connect(self._pick_port)
        menu.addAction(a)
        menu.addSeparator()
        a = QAction("Beenden", self)
        a.triggered.connect(self.quit)
        menu.addAction(a)
        self.setCentralWidget(root)
        self._update_dir()
        self._set_device_ui(False)

    def _make_tray(self):
        tray = QSystemTrayIcon(app_icon(), self)
        menu = QMenu()
        menu.addAction("Anzeigen", self._show)
        self.tray_receive = menu.addAction("Empfangsbereit machen",
                                           lambda: self.receive_btn.setChecked(
                                               not self.receive_btn.isChecked()))
        menu.addSeparator()
        menu.addAction("Beenden", self.quit)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda reason: self._show()
                               if reason == QSystemTrayIcon.Trigger else None)
        tray.setToolTip(APP)
        tray.show()
        return tray

    # ---- window life -----------------------------------------------------------

    def _show(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e):
        if self.tray and not self._quitting:
            e.ignore()
            self.hide()
            self.tray.showMessage(APP, "Läuft im Hintergrund weiter (Symbol in der "
                                       "Taskleiste).", QSystemTrayIcon.Information, 3000)
            return
        self._shutdown()
        e.accept()

    def quit(self):
        self._quitting = True
        self._shutdown()
        QApplication.quit()

    def _shutdown(self):
        self.clock.stop()
        self.session.stop()
        if self.tray:
            self.tray.hide()

    # ---- session events ----------------------------------------------------------

    def _on_event(self, kind, info):
        if kind == "device":
            state = info.get("state")
            if state == "connected":
                mac = (info.get("info") or {}).get("mac", "")
                self.device_label.setText(f"ESP verbunden ({mac})")
                self._set_device_ui(True)       # the session restarts receiving itself
            elif state == "connecting":
                pass
            elif state == "lost":
                err = info.get("error") or ""
                self.device_label.setText(f"Kein ESP: {err}" if err else "Kein ESP")
                self._set_device_ui(False)
            elif state == "stopped":
                self.device_label.setText("beendet")
        elif kind == "power":
            self.power_label.setText(power_text(info.get("text", "")))
        elif kind == "browse":
            self.search_btn.setEnabled(not info["on"] and self.session.state == "connected")
            self.search_btn.setText("Suche läuft …" if info["on"] else "Geräte suchen")
        elif kind == "receiver":
            self._receiver_item(info["receiver"])
        elif kind == "receiving":
            self._receiving_ui(info["on"])
        elif kind == "send":
            self._send_row(info)
        elif kind == "receive":
            self._receive_row(info)
        elif kind == "log":
            self._log(info.get("text", ""))

    def _log(self, text):
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')} {text}")

    def _set_device_ui(self, connected):
        self.search_btn.setEnabled(connected and not self.session_browsing())
        self.send_btn.setEnabled(connected)
        if not connected:
            self.power_label.setText("")

    def session_browsing(self):
        s = getattr(self, "session", None)
        return bool(s and s.browsing)

    def _tick(self):
        until = self.session.receive_until
        if self.session.receiving:
            if until:
                left = max(0, until - time.monotonic())
                self.receive_left.setText(f"empfangsbereit, noch {int(left // 60)}:"
                                          f"{int(left % 60):02d}")
            else:
                self.receive_left.setText("empfangsbereit (dauerhaft)")

    # ---- receiving -------------------------------------------------------------

    def _receive_toggled(self, on):
        secs = WINDOWS.get(self.window_combo.currentText())
        self.settings.setValue("window", self.window_combo.currentText())
        self._receiving_ui(on)
        run_bg(self.session.set_receiving, on, secs, on_error=self._log)

    def _receiving_ui(self, on):
        if self.receive_btn.isChecked() != on:
            self.receive_btn.blockSignals(True)
            self.receive_btn.setChecked(on)
            self.receive_btn.blockSignals(False)
        self.receive_btn.setText("Empfangen beenden" if on else "Empfangsbereit machen")
        self.window_combo.setEnabled(not on)
        if not on:
            self.receive_left.setText("aus")
        if self.tray:
            self.tray_receive.setText("Empfangen beenden" if on else "Empfangsbereit machen")

    def _on_ask(self, ask, reply):
        self._show()
        if self.tray:
            self.tray.showMessage(APP, f"{ask.sender} möchte etwas senden",
                                  QSystemTrayIcon.Information, 5000)
        dlg = AskDialog(self, ask, reply, self.bridge.ask_timeout)
        dlg.show()
        dlg.raise_()
        self._last_ask_dialog = dlg            # for tests

    def _receive_row(self, info):
        phase = info["phase"]
        ask = info.get("ask")
        if phase == "asked":
            if not info.get("accepted"):
                self._log(f"abgelehnt: {ask.summary() if ask else '?'}")
                return
            self._recv_n += 1
            item = QTreeWidgetItem(["↓", ask.sender if ask else "?",
                                    ", ".join(ask.files or ask.urls) if ask else "", ""])
            self.transfers.insertTopLevelItem(0, item)
            bar = QProgressBar()
            bar.setRange(0, 0)                 # busy until sizes are known
            self.transfers.setItemWidget(item, 3, bar)
            self._rows[("recv", self._recv_n)] = item
            self._recv_row = item
        elif phase == "receiving" and self._recv_row is not None:
            bar = self.transfers.itemWidget(self._recv_row, 3)
            total = info.get("total")
            if isinstance(bar, QProgressBar):
                if total:
                    bar.setRange(0, 1000)
                    bar.setValue(int(1000 * min(1.0, info["received"] / total)))
                bar.setFormat(human(info["received"]))
        elif phase == "received":
            item = self._recv_row
            self._recv_row = None
            if item is None:
                return
            self.transfers.removeItemWidget(item, 3)
            files = info.get("files") or []
            if info.get("ok"):
                item.setText(3, f"empfangen ({human(info.get('size', 0))})")
                item.setData(0, Qt.UserRole, files)
                item.setText(2, ", ".join(os.path.basename(f) for f in files))
                if self.tray:
                    self.tray.showMessage(APP, f"Empfangen: {item.text(2)}",
                                          QSystemTrayIcon.Information, 5000)
            else:
                item.setText(3, "unvollständig")
                item.setData(0, Qt.UserRole, files + ([info["raw"]] if info.get("raw") else []))

    def _pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Ordner für empfangene Dateien",
                                             self.session.download_dir or os.getcwd())
        if d:
            self.session.set_download_dir(d)
            self.settings.setValue("download_dir", d)
            self._update_dir()

    def _update_dir(self):
        s = getattr(self, "session", None)
        d = (s and s.download_dir) or self.settings.value("download_dir", "") \
            or default_download_dir()
        self.dir_label.setText(d)

    def _name_changed(self):
        name = self.name_edit.text().strip() or APP
        if name == self.session.name:
            return
        self.settings.setValue("name", name)
        run_bg(self.session.set_name, name, on_error=self._log)

    # ---- sending ---------------------------------------------------------------

    def _search(self):
        self.search_btn.setEnabled(False)
        run_bg(self.session.browse, 60, on_error=self._log)

    def _receiver_item(self, r):
        item = None
        for i in range(self.receivers_list.count()):
            if self.receivers_list.item(i).data(Qt.UserRole) == r.id:
                item = self.receivers_list.item(i)
        if item is None:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, r.id)
            self.receivers_list.addItem(item)
            if self.receivers_list.currentItem() is None:
                self.receivers_list.setCurrentItem(item)
        model = f"  ({r.model})" if r.model else ""
        item.setText(f"{r.label}{model}" if r.present else f"{r.label}{model}  – nicht mehr da")
        item.setForeground(QColor("black") if r.present else QColor("gray"))

    def _selected_receiver(self):
        item = self.receivers_list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _send_dialog(self):
        rid = self._selected_receiver()
        if not rid:
            QMessageBox.information(self, APP, "Zuerst „Geräte suchen“ und ein Gerät wählen.")
            return
        files, _ = QFileDialog.getOpenFileNames(self, "Dateien senden")
        if files:
            self._send_to(rid, files)

    def _send_to(self, rid, files):
        files = [f for f in files if os.path.isfile(f)]
        if not files:
            QMessageBox.information(self, APP, "Ordner lassen sich noch nicht senden, nur Dateien.")
            return
        try:
            t = self.session.send(rid, files)
        except Exception as e:                 # noqa: BLE001
            QMessageBox.warning(self, APP, f"Senden geht nicht: {e}")
            return
        self._send_row({"transfer": t, "state": t.state, "progress": 0.0, "error": None})

    def _send_row(self, info):
        t = info["transfer"]
        key = ("send", t.id)
        item = self._rows.get(key)
        if item is None:
            item = QTreeWidgetItem(["↑", t.receiver.label,
                                    ", ".join(os.path.basename(f) for f in t.files), ""])
            item.setData(0, Qt.UserRole + 1, t.id)
            self.transfers.insertTopLevelItem(0, item)
            self._rows[key] = item
        state = info["state"]
        if state == "uploading":
            bar = self.transfers.itemWidget(item, 3)
            if not isinstance(bar, QProgressBar):
                bar = QProgressBar()
                bar.setRange(0, 1000)
                self.transfers.setItemWidget(item, 3, bar)
            bar.setValue(int(1000 * info["progress"]))
        else:
            if self.transfers.itemWidget(item, 3) is not None:
                self.transfers.removeItemWidget(item, 3)
            text = SEND_STATES.get(state, state)
            if state == "failed" and info.get("error"):
                text += f": {info['error']}"
            item.setText(3, text)

    def _transfer_menu(self, pos):
        item = self.transfers.itemAt(pos)
        if item is None:
            return
        tid = item.data(0, Qt.UserRole + 1)
        menu = QMenu(self)
        if tid is not None:
            menu.addAction("Abbrechen", lambda: run_bg(self.session.cancel, tid))
        if item.data(0, Qt.UserRole):
            menu.addAction("Öffnen", lambda: self._open_transfer(item))
        menu.addAction("Aus der Liste entfernen",
                       lambda: self.transfers.takeTopLevelItem(
                           self.transfers.indexOfTopLevelItem(item)))
        menu.exec(self.transfers.viewport().mapToGlobal(pos))

    def _open_transfer(self, item, column=0):
        files = item.data(0, Qt.UserRole) or []
        target = files[0] if len(files) == 1 else (os.path.dirname(files[0]) if files else None)
        if target:
            QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    # ---- settings --------------------------------------------------------------

    def _pick_port(self):
        from PySide6.QtWidgets import QInputDialog
        from .transport import candidates
        ports = ["automatisch"] + candidates()
        current = self.port or "automatisch"
        port, ok = QInputDialog.getItem(self, APP, "Serieller Port des ESP:", ports,
                                        ports.index(current) if current in ports else 0, True)
        if not ok:
            return
        self.port = None if port == "automatisch" else port
        self.settings.setValue("port", self.port or "")
        receiving = self.session.receiving
        secs = WINDOWS.get(self.window_combo.currentText())
        old, new = self.session, self._make_session()
        self.session = new

        def restart():
            old.stop()                          # frees the port for the new one
            new.start()
            if receiving:
                new.set_receiving(True, secs)
        run_bg(restart, on_error=self._log)


def power_text(reply):
    """The device's `power` line, short: 'Funk aus' / 'Funk an (…)'."""
    words = reply.split()
    if len(words) < 3 or words[0] != "power":
        return ""
    if words[1] == "on":
        return "Funk immer an"
    listen = next((w[7:] for w in words if w.startswith("listen=")), "off")
    why = {"(hold)": "für die App", "(wake)": "zum Senden", "(busy)": "Übertragung",
           "(advert)": "Gerät in der Nähe"}.get(words[3] if len(words) > 3 else "", "")
    if words[2] == "asleep":
        return "Funk schläft (empfangsbereit)" if listen != "off" else "Funk aus (spart Strom)"
    return f"Funk an ({why})" if why else "Funk an"


def default_download_dir():
    home = os.path.expanduser("~")
    downloads = os.path.join(home, "Downloads")
    return os.path.join(downloads if os.path.isdir(downloads) else home, "EspDrop")


def main(port=None):
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP)
    app.setQuitOnLastWindowClosed(False)
    w = MainWindow(port=port)
    w.show()
    return app.exec()
