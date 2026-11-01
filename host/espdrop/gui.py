# EspDrop - the desktop app (PySide6) on top of the Session.
#
#   espdrop gui [-p PORT]          (Windows: .\tools\espdrop-gui.cmd)
#   espdrop gui --background       tray only, receiving; what autostart runs
#   espdrop gui --send FILE...     the AirDrop sheet (the file manager's right-click menu)
#
# In the look of Apple's own apps (ui.py): one column of rounded cards -
# visibility ("Aus / 10 Minuten / Für alle", as on the iPhone), name and
# folder; the devices nearby as round avatars to click or drop files on; the
# transfers; the general settings - a floating card that asks before anything
# is received, and an AirDrop sheet for sending from the file manager. It
# keeps running in the tray when the window is closed; in the background it
# only shows itself for an incoming transfer. Light and dark follow the system.
#
# One copy per user: a second start hands its files (or "show yourself") to
# the running one over a local socket and exits, so the file manager and
# autostart never fight over the serial port.
#
# Session events arrive on worker threads; a Qt signal carries them to the GUI
# thread. Session calls that talk to the device run on a short-lived thread so
# a slow device never freezes the window.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import getpass
import json
import os
import re
import sys
import threading
import time

import shiboken6
from PySide6.QtCore import QObject, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QGuiApplication, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFileDialog, QFrame,
                               QGraphicsDropShadowEffect, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                               QScrollArea, QSystemTrayIcon, QToolButton, QVBoxLayout, QWidget)

from . import env, flash, integration, ui
from .session import Session
from .version import __version__

APP = "EspDrop"
# Who may send to us, as AirDrop on the iPhone puts it: label, seconds (0: off,
# None: until switched off).
VISIBILITY = [("Aus", 0), ("10 Minuten", 600), ("Für alle", None)]

THEMES = [("Automatisch", "auto"), ("Hell", "light"), ("Dunkel", "dark")]
THEME_KEYS = [key for _, key in THEMES]

SEND_STATES = {"new": "Warten …", "asking": "Warten auf Annahme …", "uploading": "Senden …",
               "done": "Gesendet", "declined": "Abgelehnt", "failed": "Fehlgeschlagen",
               "cancelled": "Abgebrochen"}
TILE_PHASE = {"new": "waiting", "asking": "waiting", "uploading": "sending", "done": "sent",
              "declined": "declined", "failed": "failed", "cancelled": "cancelled"}
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".tif", ".tiff",
             ".bmp", ".dng"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".hevc"}

app_icon = ui.app_icon


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
            text = f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            return text.replace(".", ",")
        n /= 1000


def _safe(fn, default):
    """fn(), or `default` when asking the system fails (the window still opens)."""
    try:
        return fn()
    except Exception:                          # noqa: BLE001
        return default


def _kind(path):
    ext = os.path.splitext(path)[1].lower()
    return "photo" if ext in PHOTO_EXT else "video" if ext in VIDEO_EXT else "file"


def what_text(files, urls=()):
    """'ein Foto', '3 Dateien', 'einen Link' - what a transfer carries."""
    if urls and not files:
        return "einen Link" if len(urls) == 1 else f"{len(urls)} Links"
    n = len(files)
    if not n:
        return "etwas"
    kinds = {_kind(f) for f in files}
    one, many = {"photo": ("ein Foto", "Fotos"), "video": ("ein Video", "Videos")}.get(
        kinds.pop() if len(kinds) == 1 else "file", ("eine Datei", "Dateien"))
    return one if n == 1 else f"{n} {many}"


# ---- one copy per user -------------------------------------------------------

def instance_name():
    """The local socket (a named pipe on Windows) of this user's running copy
    (ESPDROP_INSTANCE: another one, for a test run beside the real app)."""
    if os.environ.get("ESPDROP_INSTANCE"):
        return re.sub(r"[^A-Za-z0-9_-]", "_", os.environ["ESPDROP_INSTANCE"])
    user = re.sub(r"[^A-Za-z0-9_-]", "_", getpass.getuser() or "user")
    return f"espdrop-{user}"


def send_to_running(message, name=None, timeout_ms=1000):
    """Hand `message` ({"files": [...], "show": bool}) to the running copy.
    False when there is none (then this process becomes it)."""
    sock = QLocalSocket()
    sock.connectToServer(name or instance_name())
    if not sock.waitForConnected(timeout_ms):
        return False
    sock.write(json.dumps(message).encode("utf-8") + b"\n")
    sock.waitForBytesWritten(timeout_ms)
    sock.disconnectFromServer()
    if sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
        sock.waitForDisconnected(timeout_ms)
    return True


class InstanceServer(QObject):
    """The running copy's end: each later start sends one JSON line.

    The sockets of finished starts are deleted here, explicitly, at the next
    start and in close(), never with deleteLater: a deferred delete still
    queued when the server goes (quit) deletes the socket a second time."""
    message = Signal(object)

    def __init__(self, name=None):
        super().__init__()
        self.name = name or instance_name()
        self._conns = []                       # [socket, bytes so far] per later start
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not self.server.listen(self.name):
            # A socket file left by a copy that crashed (Linux); main() only
            # gets here when nothing answered on it.
            QLocalServer.removeServer(self.name)
            self.server.listen(self.name)
        self.server.newConnection.connect(self._accept)

    def _accept(self):
        self._prune()
        while self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            self._conns.append([sock, bytearray()])
            sock.readyRead.connect(self._read)  # a method: no closure per socket
        self._read()                           # what came in before the connect

    def _read(self):
        for sock, buf in list(self._conns):    # a handler may start a nested loop
            if not shiboken6.isValid(sock):
                continue
            buf.extend(bytes(sock.readAll()))
            while b"\n" in buf:
                line, _, rest = bytes(buf).partition(b"\n")
                buf[:] = rest
                try:
                    msg = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self.message.emit(msg)

    def _prune(self, everything=False):
        keep = []
        for conn in self._conns:
            sock = conn[0]
            if not shiboken6.isValid(sock):
                continue
            if everything or sock.state() == QLocalSocket.LocalSocketState.UnconnectedState:
                shiboken6.delete(sock)
            else:
                keep.append(conn)
        self._conns = keep

    def close(self):
        self.server.close()
        self._read()
        self._prune(everything=True)


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


# ---- asking before a transfer -------------------------------------------------

class AskDialog(QDialog):
    """A floating card, as AirDrop asks: the picture, '„Bens iPhone“ möchte ein
    Foto teilen.', Ablehnen / Annehmen, and a line running out until it is
    declined by itself. Enter accepts nothing by accident: no default button."""

    def __init__(self, parent, ask, reply, timeout):
        super().__init__(parent)
        self.reply = reply
        self.timeout = timeout
        self._grab = None
        self.setWindowTitle("AirDrop")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 20, 28, 36)          # room for the shadow
        card = QFrame()
        card.setObjectName("alert")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(44)
        shadow.setOffset(0, 12)
        shadow.setColor(QColor(0, 0, 0, ui.T["shadow_alpha"] + 25))
        card.setGraphicsEffect(shadow)
        v = QVBoxLayout(card)
        v.setContentsMargins(22, 22, 22, 18)
        v.setSpacing(8)
        pic = QLabel()
        pic.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        pic.setPixmap(self._preview(ask))
        v.addWidget(pic)
        v.addSpacing(4)
        v.addWidget(ui.label(f"„{ask.sender}“ möchte {what_text(ask.files, ask.urls)} teilen.",
                             "headline", wrap=True, center=True))
        what = ask.files or ask.urls
        if what:
            names = ", ".join(what[:3]) + (f" und {len(what) - 3} weitere" if len(what) > 3 else "")
            v.addWidget(ui.label(names, "secondary", wrap=True, center=True))
        v.addSpacing(6)
        self.countdown = ui.ThinProgress()
        self.countdown.set_value(1.0)
        v.addWidget(self.countdown)
        self.left = ui.label("", "caption", center=True)
        v.addWidget(self.left)
        v.addSpacing(2)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.no = ui.button("Ablehnen")
        self.yes = ui.button("Annehmen", "primary")
        for b in (self.no, self.yes):
            b.setAutoDefault(False)
            b.setDefault(False)
            b.setMinimumHeight(32)
            row.addWidget(b, 1)
        self.no.clicked.connect(self.reject)
        self.yes.clicked.connect(self.accept)
        v.addLayout(row)
        outer.addWidget(card)
        self.setFixedWidth(400)
        self.end = time.monotonic() + timeout
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self._tick()
        self.finished.connect(self._answer)

    @staticmethod
    def _preview(ask):
        pm = QPixmap()
        if ask.icon and pm.loadFromData(ask.icon):
            return ui.rounded_fit(pm, 260, 170, 12)
        if ask.files:
            return ui.file_badge(os.path.splitext(ask.files[0])[1], 72)
        return ui.app_pixmap(64)

    def showEvent(self, e):
        super().showEvent(e)
        parent = self.parentWidget()
        screen = (parent.screen() if parent and parent.isVisible()
                  else QGuiApplication.primaryScreen())
        if screen:
            g = screen.availableGeometry()
            self.adjustSize()
            self.move(g.center().x() - self.width() // 2, g.top() + int(g.height() * 0.2))

    def mousePressEvent(self, e):                 # no title bar: drag it anywhere
        if e.button() == Qt.MouseButton.LeftButton:
            self._grab = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._grab is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._grab)

    def mouseReleaseEvent(self, e):
        self._grab = None

    def _tick(self):
        left = self.end - time.monotonic()
        if left <= 0:
            self.reject()
            return
        self.countdown.set_value(left / self.timeout)
        self.left.setText(f"Ohne Antwort abgelehnt in {left:.0f} s")

    def _answer(self, result):
        self.timer.stop()
        self.reply["ok"] = result == QDialog.DialogCode.Accepted
        self.reply["done"].set()


# ---- the AirDrop sheet ----------------------------------------------------------

class SendDialog(QDialog):
    """The AirDrop sheet: the files on top, the devices a search finds as
    avatars, one click sends. The avatar shows the wait for Accept and the
    progress; the sheet closes itself when the files are through. Abbrechen
    (or closing it) cancels a running transfer."""

    def __init__(self, window, files):
        super().__init__(None)                 # top-level: the main window may be hidden
        self.w = window
        self.files = []
        self.transfer_id = None
        self._target = None
        self._browsing = False
        self.setWindowTitle("AirDrop")
        self.setWindowIcon(ui.app_icon())
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(480, 470)
        self.setMinimumWidth(400)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(14)
        self.preview = QLabel()
        self.preview.setFixedSize(64, 72)
        head.addWidget(self.preview, 0, Qt.AlignmentFlag.AlignVCenter)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addStretch(1)
        self.title = ui.ElidedLabel()
        self.title.setFont(ui.font(15, QFont.Weight.DemiBold))
        self.subtitle = ui.label("", "secondary")
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        titles.addStretch(1)
        head.addLayout(titles, 1)
        self.more_btn = ui.button("Hinzufügen …", "link")
        self.more_btn.clicked.connect(self._choose_files)
        head.addWidget(self.more_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addLayout(head)
        lay.addWidget(ui.section("AirDrop"))
        card = ui.Card()
        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(8)
        self.radar = ui.Radar()
        self.grid = ui.DeviceGrid()
        self.grid.clicked.connect(self._pick)
        self.grid.dropped.connect(self._dropped)
        self.status = ui.label("", "secondary", wrap=True, center=True)
        bv.addWidget(self.radar)
        bv.addWidget(self.grid)
        bv.addWidget(self.status)
        card.add(body, margins=(12, 14, 12, 16))
        lay.addWidget(card, 1)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.close_btn = ui.button("Abbrechen")
        self.close_btn.clicked.connect(self.reject)
        bottom.addWidget(self.close_btn)
        lay.addLayout(bottom)
        self.add_files(files)
        for r in window.session.receivers():
            if r.present:
                self.on_receiver(r)
        self.on_device(window.session.state == "connected")
        self._update_view()

    # ---- files ----------------------------------------------------------------

    def add_files(self, files):
        """More files for this send (Explorer passes them one call at a time)."""
        if self.transfer_id is not None:
            return False
        for f in files:
            f = os.path.abspath(f)
            if os.path.isfile(f) and f not in self.files:
                self.files.append(f)
        self.preview.setPixmap(ui.files_preview(self.files, 64))
        if self.files:
            size = sum(os.path.getsize(f) for f in self.files)
            if len(self.files) == 1:
                self.title.setText(os.path.basename(self.files[0]))
                ext = os.path.splitext(self.files[0])[1].lstrip(".").upper()
                self.subtitle.setText(f"{ext} · {human(size)}" if ext else human(size))
            else:
                text = what_text(self.files)
                self.title.setText(text[0].upper() + text[1:])
                self.subtitle.setText(human(size))
        else:
            self.title.setText("Keine Datei gewählt")
            self.subtitle.setText("„Hinzufügen …“ wählt Dateien (Ordner gehen noch nicht).")
        return True

    def _choose_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Per AirDrop senden")
        self.add_files(files)

    # ---- devices --------------------------------------------------------------

    def _update_view(self):
        has = self.grid.count() > 0
        self.radar.setVisible(not has)
        self.radar.set_running(self._browsing)
        self.grid.setVisible(has)

    def on_device(self, connected):
        if not connected:
            self.status.setText("Verbinde mit dem ESP …")
            self._browsing = False
            self._update_view()
            return
        if not self._browsing and self.transfer_id is None:
            self._browsing = True
            self.status.setText("Suche nach Geräten in der Nähe …\nAm iPhone oder iPad: "
                                "AirDrop „Für alle“, entsperrt.")
            run_bg(self.w.session.browse, 60, on_error=self.w._log)
        self._update_view()

    def on_receiver(self, r):
        if r.id not in self.grid.tiles and not r.present:
            return
        tile = self.grid.upsert(r.id, r.label, r.model, r.present, r.discovered)
        if self.transfer_id is not None:
            tile.setEnabled(r.id == self._target)
        elif self._browsing:
            self.status.setText("Zum Senden auf ein Gerät klicken.")
        self._update_view()

    def _dropped(self, rid, files):
        self.add_files(files)
        self._pick(rid)

    def _pick(self, rid):
        if self.transfer_id is not None:
            return
        if not self.files:
            self._choose_files()
            if not self.files:
                return
        try:
            t = self.w.session.send(rid, self.files)
        except Exception as e:                 # noqa: BLE001
            self.status.setText(f"Senden geht nicht: {e}")
            return
        self.transfer_id, self._target = t.id, rid
        self.grid.set_tiles_enabled(False, except_rid=rid)
        tile = self.grid.tiles.get(rid)
        if tile:
            tile.set_phase("waiting")
        self.more_btn.setEnabled(False)
        self.status.setText(f"Warten, bis {t.receiver.label} annimmt …")

    # ---- the transfer ---------------------------------------------------------

    def on_send(self, info):
        t = info["transfer"]
        if t.id != self.transfer_id:
            return
        state = info["state"]
        tile = self.grid.tiles.get(t.receiver.id)
        if tile:
            tile.set_phase(TILE_PHASE.get(state, "idle"), info.get("progress") or 0.0)
        if state == "uploading":
            self.status.setText(f"Senden an {t.receiver.label} …")
        elif state == "done":
            self.status.setText(f"Gesendet an {t.receiver.label}.")
            self.transfer_id = None
            self.close_btn.setText("Fertig")
            QTimer.singleShot(1500, self.accept)
        elif state in ("declined", "failed", "cancelled"):
            text = {"declined": f"{t.receiver.label} hat abgelehnt.",
                    "cancelled": "Abgebrochen."}.get(state, f"Fehlgeschlagen: {info.get('error')}")
            self.status.setText(text + " Zum erneuten Versuch auf ein Gerät klicken.")
            self.transfer_id = None
            self.grid.set_tiles_enabled(True)
            self.more_btn.setEnabled(True)

    def done(self, result):
        if result != QDialog.DialogCode.Accepted and self.transfer_id is not None:
            run_bg(self.w.session.cancel, self.transfer_id)
        self.w._send_dialog_closed(self)
        super().done(result)


# ---- the ESP's firmware ---------------------------------------------------------------

MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def built_text(built):
    """The compiler's 'Oct 31 2026 12:34:56' as '31.10.2026, 12:34'."""
    m = re.match(r"([A-Z][a-z]{2})\s+(\d{1,2}) (\d{4})(?: (\d{2}):(\d{2}))?", built or "")
    if not m or m.group(1) not in MONTHS:
        return built
    text = f"{int(m.group(2))}.{MONTHS.index(m.group(1)) + 1}.{m.group(3)}"
    return text + (f", {m.group(4)}:{m.group(5)}" if m.group(4) else "")


FLASH_STAGES = {"connect": "Verbinde mit dem ESP …",
                "write": "Schreibe die Firmware …",
                "verify": "Prüfe, ob alles angekommen ist …",
                "restart": "Starte den ESP neu …"}
TRANSFER_DONE = ("done", "declined", "failed", "cancelled")


class FlashSignals(QObject):
    """From the flashing thread to the dialog."""
    progress = Signal(str, object)             # stage, fraction
    log = Signal(str)
    finished = Signal(object)                  # None, or what went wrong


class FirmwareDialog(QDialog):
    """Put EspDrop's firmware on the ESP: what it runs now, what the app comes
    with (or a file the user picks), one button. The app lets go of the port
    while esptool writes, and connects again afterwards. No buttons on the ESP
    are needed (its USB-Serial/JTAG resets it into the ROM bootloader)."""

    def __init__(self, window, flasher=None, loader=None):
        super().__init__(None)                 # top-level: the main window may be hidden
        self.w = window
        self.flasher = flasher or flash.flash
        self.loader = loader or flash.load
        self.fw = None
        self.busy = False
        self.setWindowTitle("ESP-Firmware")
        self.setWindowIcon(ui.app_icon())
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(10)
        title = QLabel("ESP-Firmware")
        title.setFont(ui.font(15, QFont.Weight.DemiBold))
        lay.addWidget(title)
        lay.addWidget(ui.label("Die App bringt die Firmware für den ESP32-C5 mit und spielt sie "
                               "über das USB-Kabel auf; Tasten am ESP braucht es dafür nicht.",
                               "secondary", wrap=True))
        card = ui.Card()
        self.device_sub = card.add_row("Auf dem ESP", None, "")
        self.file_btn = ui.button("Andere Datei …", "link")
        self.file_btn.clicked.connect(self._choose)
        self.fw_sub = card.add_row("Zum Installieren", self.file_btn, "")
        self.port_box = QComboBox()
        self.port_box.setMinimumWidth(130)
        self.port_sub = card.add_row("Anschluss", self.port_box, "")
        lay.addWidget(card)
        self.status = ui.label("", "secondary", wrap=True)
        lay.addWidget(self.status)
        self.bar = ui.ThinProgress()
        self.bar.hide()
        lay.addWidget(self.bar)
        lay.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.close_btn = ui.button("Schließen")
        self.close_btn.clicked.connect(self.close)
        self.install_btn = ui.button("Installieren", "primary")
        self.install_btn.clicked.connect(self._install)
        row.addWidget(self.close_btn)
        row.addWidget(self.install_btn)
        lay.addLayout(row)
        self.sig = FlashSignals(self)
        self.sig.progress.connect(self._on_progress)
        self.sig.log.connect(self.w._log)
        self.sig.finished.connect(self._on_finished)
        self.w.bridge.event.connect(self._on_event)
        self._load(flash.bundled())
        self._fill_ports()
        self._refresh()

    # ---- what is where ---------------------------------------------------------

    def _load(self, path):
        self.fw = None
        if not path:
            self.fw_sub.setText("Keine Firmware dabei: eine Datei wählen (espdrop-esp32c5.bin "
                                "aus einem Release).")
            return
        try:
            self.fw = self.loader(path)
        except flash.FirmwareError as e:
            self.fw_sub.setText(str(e))
            return
        name = os.path.basename(self.fw.source.rstrip("/\\"))
        self.fw_sub.setText(f"EspDrop {self.fw.version} für den {self.fw.chip}, gebaut "
                            f"{built_text(self.fw.built)}\n{name}")
        self._fit()

    def _choose(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Firmware wählen", os.path.expanduser("~"),
            "ESP-Firmware (*.bin flasher_args.json);;Alle Dateien (*)")
        if path:
            self._load(path)
            self._refresh()

    def _fill_ports(self):
        found = _safe(flash.ports, [])
        if self.w.port and self.w.port not in found:
            found.insert(0, self.w.port)
        self.port_box.clear()
        self.port_box.addItems(found)
        if self.w.port:
            self.port_box.setCurrentText(self.w.port)
        self.port_box.setVisible(len(found) > 1)
        self.port_sub.setText(found[0] if len(found) == 1 else
                              "Kein ESP gefunden: per USB anstecken." if not found else
                              "Mehrere gefunden: den ESP wählen.")

    def _refresh(self):
        self.device_sub.setText(self.w.firmware_text())
        if not self.busy:
            self.install_btn.setEnabled(self.fw is not None and self.port_box.count() > 0)

    def _on_event(self, kind, info):
        if kind == "device":
            if not self.busy and info.get("state") in ("connected", "lost"):
                self._fill_ports()             # plugged in or out meanwhile
            self._refresh()

    # ---- installing ----------------------------------------------------------

    def _install(self):
        self._fill_ports()
        port = self.port_box.currentText()
        if not port or self.fw is None:
            self._refresh()
            return
        if self.w.transfer_running():
            self._say("Gerade läuft eine Übertragung; danach installieren.", "bad")
            return
        self.busy = True
        for b in (self.install_btn, self.close_btn, self.file_btn, self.port_box):
            b.setEnabled(False)
        self.bar.set_value(None)
        self.bar.show()
        self._say(FLASH_STAGES["connect"])
        self.w.set_flashing(True)
        session, fw, sig, flasher = self.w.session, self.fw, self.sig, self.flasher

        def work():
            error = None
            try:
                session.stop()                 # let go of the port
                flasher(fw, port, on_progress=sig.progress.emit, on_log=sig.log.emit)
            except flash.FirmwareError as e:
                error = str(e)
            except Exception as e:             # noqa: BLE001 - shown to the user
                error = f"Flashen fehlgeschlagen: {e}"
            finally:
                session.start()                # connect again, new firmware or old
            sig.finished.emit(error)
        threading.Thread(target=work, name="espdrop-flash", daemon=True).start()

    def _on_progress(self, stage, fraction):
        if stage == "write" and fraction is not None:
            self.bar.set_value(fraction)
            self._say(f"{FLASH_STAGES['write']} {fraction * 100:.0f} %")
        elif stage in FLASH_STAGES:
            if stage != "connect":
                self.bar.set_value(1.0)
            self._say(FLASH_STAGES[stage])

    def _on_finished(self, error):
        self.busy = False
        self.w.set_flashing(False)
        for b in (self.close_btn, self.file_btn, self.port_box):
            b.setEnabled(True)
        if error:
            self.bar.hide()
            self._say(error, "bad")
            self.install_btn.setText("Erneut versuchen")
        else:
            self.bar.set_value(1.0)
            self._say(f"Fertig: EspDrop {self.fw.version} ist auf dem ESP. Er startet neu und "
                      "verbindet sich gleich wieder.", "good")
            self.install_btn.setText("Erneut installieren")
        self._refresh()

    def _say(self, text, tone=None):
        self.status.setText(text)
        self.status.setProperty("tone", tone or "")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self._fit()

    def _fit(self):
        """Grow to fit longer text (wrapped lines need the height for this width)."""
        lay = self.layout()
        lay.activate()
        need = (lay.totalHeightForWidth(self.width()) if lay.hasHeightForWidth()
                else self.sizeHint().height())
        if self.height() < need:
            self.resize(self.width(), need)

    def closeEvent(self, e):
        if self.busy:                          # half a firmware helps nobody
            e.ignore()
            return
        super().closeEvent(e)

    def reject(self):                          # Esc
        if not self.busy:
            super().reject()


# ---- the window -------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, port=None, settings=None, session_factory=None, ask_timeout=60.0,
                 tray=True, background=False):
        super().__init__()
        ui.ensure(QApplication.instance())
        self.settings = settings or QSettings(APP, APP)
        ui.set_mode(QApplication.instance(), self._theme_key())
        self.setWindowTitle(APP)
        self.setWindowIcon(ui.app_icon())
        self.resize(560, 800)
        self.setMinimumWidth(480)
        self.port = port or self.settings.value("port", "") or None
        self.bridge = Bridge(ask_timeout)
        self.bridge.event.connect(self._on_event)
        self.bridge.ask.connect(self._on_ask)
        # session_factory(window) -> Session, for tests; it must use window.bridge
        self._make_session = ((lambda: session_factory(self)) if session_factory
                              else self._default_session)
        self._rows = {}                        # ("send", id) / ("recv", n) -> TransferRow
        self._recv_n = 0
        self._recv_row = None                  # the row of the transfer being received
        self._quitting = False
        self._browsing = False
        self._connected = False
        self._send_dialogs = []                # open AirDrop sheets
        self._ask_dialogs = []                 # open accept cards (may have no parent)
        self._notify_files = None              # what clicking the last notification opens
        self.firmware = None                   # the ESP's `version` reply (dict), None: too old
        self._silent = 0                       # HELLOs in a row an ESP did not answer
        self._flashing = False
        self._bundled = None                   # (path, Firmware or None), loaded when needed
        self._fw_dialog = None
        self._build()
        self._update_firmware_ui()
        self.tray = self._make_tray() if tray and QSystemTrayIcon.isSystemTrayAvailable() else None
        self.session = self._make_session()
        self.session.start()
        self._update_dir()
        self.clock = QTimer(self)
        self.clock.setInterval(1000)
        self.clock.timeout.connect(self._tick)
        self.clock.start()
        if background:
            # Autostart: visible to everyone until switched off; the window
            # stays hidden and only an incoming transfer shows its card.
            self._receiving_ui(True, None)
            run_bg(self.session.set_receiving, True, None, on_error=self._log)
        self._tick()

    # ---- setup ---------------------------------------------------------------

    def _default_session(self):
        d = self.settings.value("download_dir", "") or default_download_dir()
        return Session(port=self.port, name=self._name(), receive=False,
                       download_dir=d, on_event=self.bridge.on_event,
                       on_ask=self.bridge.on_ask, ask_timeout=self.bridge.ask_timeout)

    def _build(self):
        page = QWidget()
        page.setObjectName("page")
        col = QVBoxLayout(page)
        col.setContentsMargins(24, 22, 24, 24)
        col.setSpacing(4)

        # the app, and whether the ESP is there
        head = QHBoxLayout()
        head.setSpacing(14)
        mark = QLabel()
        mark.setPixmap(ui.app_pixmap(56))
        head.addWidget(mark)
        titles = QVBoxLayout()
        titles.setSpacing(3)
        titles.addStretch(1)
        titles.addWidget(ui.label(APP, "title"))
        status = QHBoxLayout()
        status.setSpacing(6)
        self.dot = ui.StatusDot()
        status.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.device_label = ui.label("Suche ESP …", "secondary")
        self.power_label = ui.label("", "secondary")
        status.addWidget(self.device_label)
        status.addWidget(self.power_label)
        self.fw_hint = ui.button("Firmware installieren …", "link")
        self.fw_hint.clicked.connect(self.open_firmware_dialog)
        self.fw_hint.hide()
        status.addWidget(self.fw_hint)
        status.addStretch(1)
        titles.addLayout(status)
        titles.addStretch(1)
        head.addLayout(titles, 1)
        self.more_btn = ui.tool_button("more", "Mehr")
        self.more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.more_btn)
        self.log_action = menu.addAction("Protokoll anzeigen")
        self.log_action.setCheckable(True)
        menu.addAction("ESP-Firmware …", self.open_firmware_dialog)
        menu.addAction("Serieller Port …", self._pick_port)
        menu.addSeparator()
        menu.addAction("Beenden", self.quit)
        self.more_btn.setMenu(menu)
        head.addWidget(self.more_btn, 0, Qt.AlignmentFlag.AlignTop)
        col.addLayout(head)

        # AirDrop: who may send to us, our name, where files go
        col.addWidget(ui.section("AirDrop"))
        card = ui.Card()
        self.visibility = ui.Segmented([lbl for lbl, _ in VISIBILITY])
        self.visibility.changed.connect(self._visibility_picked)
        vis = QWidget()                        # the segments, and below them how long
        vv = QVBoxLayout(vis)
        vv.setContentsMargins(0, 0, 0, 0)
        vv.setSpacing(4)
        vh = QHBoxLayout()
        vh.addWidget(QLabel("Empfangen von"))
        vh.addStretch(1)
        vh.addWidget(self.visibility)
        vv.addLayout(vh)
        self.receive_left = ui.label("", "caption")
        vv.addWidget(self.receive_left)
        card.add(vis, margins=(16, 10, 14, 9))
        # Empty (never changed): this PC's name; the field shows it greyed.
        self.name_edit = QLineEdit(self.settings.value("name", "") or "")
        self.name_edit.setPlaceholderText(env.device_name())
        self.name_edit.setFixedWidth(210)
        self.name_edit.editingFinished.connect(self._name_changed)
        card.add_row("Name", self.name_edit, "So sehen dich iPhone, iPad und Mac.")
        folder = QWidget()
        fh = QHBoxLayout(folder)
        fh.setContentsMargins(0, 0, 0, 0)
        fh.setSpacing(8)
        fv = QVBoxLayout()
        fv.setSpacing(2)
        fv.addWidget(QLabel("Empfangene Dateien"))
        self.dir_label = ui.ElidedLabel()
        self.dir_label.setProperty("role", "caption")
        fv.addWidget(self.dir_label)
        fh.addLayout(fv, 1)
        change = ui.button("Ändern …", "link")
        change.clicked.connect(self._pick_dir)
        show = ui.button("Öffnen", "link")
        show.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(self.session.download_dir or os.getcwd())))
        fh.addWidget(change)
        fh.addWidget(show)
        card.add(folder, margins=(16, 9, 12, 9))
        self.clipboard_switch = ui.Switch()
        self.clipboard_switch.set_quiet(self.settings.value("clipboard", False, type=bool))
        self.clipboard_switch.toggled.connect(
            lambda on: self.settings.setValue("clipboard", bool(on)))
        card.add_row("In die Zwischenablage kopieren", self.clipboard_switch,
                     "Empfangenes zusätzlich zum Einfügen bereithalten: Bilder, Dateien "
                     "und Links.")
        col.addWidget(card)

        # devices nearby
        self.search_btn = ui.button("Suchen")
        self.search_btn.clicked.connect(self._search)
        col.addWidget(ui.section("Geräte in der Nähe", self.search_btn))
        dcard = ui.Card()
        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(6)
        self.radar = ui.Radar()
        self.radar_text = ui.label("Suche nach Geräten in der Nähe …", "secondary", center=True)
        self.devices_note = ui.label("„Suchen“ findet AirDrop-Geräte in der Nähe. Am iPhone "
                                     "oder iPad: AirDrop „Für alle“, entsperrt.",
                                     "secondary", wrap=True, center=True)
        self.devices_note.setContentsMargins(12, 10, 12, 10)
        self.devices = ui.DeviceGrid()
        self.devices.clicked.connect(self._tile_clicked)
        self.devices.dropped.connect(self._send_to)
        self.devices_hint = ui.label("Dateien auf ein Gerät ziehen oder es anklicken.",
                                     "caption", center=True)
        for w in (self.radar, self.radar_text, self.devices_note, self.devices,
                  self.devices_hint):
            bv.addWidget(w)
        dcard.add(body, margins=(12, 12, 12, 12))
        col.addWidget(dcard)

        # transfers
        col.addWidget(ui.section("Übertragungen"))
        tcard = ui.Card()
        self.transfers = ui.TransferList()
        tcard.add(self.transfers, margins=(0, 0, 0, 0))
        col.addWidget(tcard)

        # general
        caps = integration.supported()
        self.autostart_switch = self.send_menu_switch = None
        col.addWidget(ui.section("Allgemein"))
        gcard = ui.Card()
        self.theme = ui.Segmented([lbl for lbl, _ in THEMES])
        self.theme.set_index(THEME_KEYS.index(self._theme_key()), animate=False)
        self.theme.changed.connect(self._theme_picked)
        gcard.add_row("Erscheinungsbild", self.theme)
        if caps["autostart"]:
            s = self.autostart_switch = ui.Switch()
            s.set_quiet(_safe(integration.autostart_enabled, False))
            s.toggled.connect(lambda on, s=s: self._integrate(integration.set_autostart, on, s))
            gcard.add_row("Beim Anmelden im Hintergrund starten", s,
                          "Unsichtbar im Infobereich und für alle sichtbar; nur eingehende "
                          "Dateien melden sich.")
        if caps["send"]:
            s = self.send_menu_switch = ui.Switch()
            s.set_quiet(_safe(integration.send_menu_enabled, False))
            s.toggled.connect(lambda on, s=s: self._integrate(integration.set_send_menu, on, s))
            gcard.add_row(caps["send"], s, caps.get("send_hint"))
        self.fw_btn = ui.button("Installieren …")
        self.fw_btn.clicked.connect(self.open_firmware_dialog)
        self.fw_sub = gcard.add_row("ESP-Firmware", self.fw_btn, "")
        col.addWidget(gcard)

        # the log, off unless asked for
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPixelSize(11)
        self.log.setFont(mono)
        self.log.setMinimumHeight(140)
        self.log_box = QWidget()
        lb = QVBoxLayout(self.log_box)
        lb.setContentsMargins(0, 0, 0, 0)
        lb.addWidget(ui.section("Protokoll"))
        lb.addWidget(self.log)
        self.log_box.hide()
        self.log_action.toggled.connect(self.log_box.setVisible)
        col.addWidget(self.log_box)
        col.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(page)
        self.setCentralWidget(scroll)
        self._update_dir()
        self._set_device_ui(False)
        self._update_devices_ui()

    def _make_tray(self):
        tray = QSystemTrayIcon(ui.app_icon(), self)
        menu = QMenu()
        menu.addAction("EspDrop öffnen", self._show)
        menu.addAction("Dateien senden …", lambda: self.open_send_dialog([]))
        self.tray_receive = menu.addAction("Für alle sichtbar (10 Minuten)",
                                           lambda: self.visibility.select(
                                               0 if self.session.receiving else 1))
        menu.addSeparator()
        menu.addAction("Beenden", self.quit)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda reason: self._show()
                               if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        tray.messageClicked.connect(self._notification_clicked)
        tray.setToolTip(APP)
        tray.show()
        return tray

    def _notify(self, text, files=None, msecs=5000):
        """A tray notification; clicking it opens `files` (else the window)."""
        self._notify_files = files
        if self.tray:
            self.tray.showMessage(APP, text, QSystemTrayIcon.MessageIcon.Information, msecs)

    def _notification_clicked(self):
        if self._notify_files:
            self._open_files(self._notify_files)
        else:
            self._show()

    def _open_files(self, files):
        if files:
            target = files[0] if len(files) == 1 else os.path.dirname(files[0])
            QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    def _integrate(self, fn, on, switch):
        """Autostart / file manager menu on or off; a failure is shown and undone."""
        try:
            fn(on)
        except Exception as e:                 # noqa: BLE001
            switch.set_quiet(not on)
            QMessageBox.warning(self, APP, f"Das ging nicht: {e}")

    # ---- other starts and the share menu -------------------------------------

    def handle_message(self, msg):
        """From a later start (InstanceServer): files to send, or show."""
        files = msg.get("files") or []
        if files or msg.get("send"):
            self.open_send_dialog(files)
        elif msg.get("show"):
            self._show()

    def open_send_dialog(self, files):
        """The AirDrop sheet for these files; Explorer passes files one call at
        a time, so they join a sheet that has not started sending yet."""
        for d in self._send_dialogs:
            if d.transfer_id is None and d.add_files(files):
                d.show()
                d.raise_()
                d.activateWindow()
                return d
        d = SendDialog(self, files)
        self._send_dialogs.append(d)
        d.show()
        d.raise_()
        d.activateWindow()
        if not files:
            QTimer.singleShot(0, d._choose_files)
        return d

    def _send_dialog_closed(self, d):
        if d in self._send_dialogs:
            self._send_dialogs.remove(d)
        if not self._send_dialogs and self.session.browsing and not self.isVisible():
            run_bg(self.session.stop_browse)

    # ---- window life -----------------------------------------------------------

    def _show(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e):
        if self.tray and not self._quitting:
            e.ignore()
            self.hide()
            self._notify("Läuft im Hintergrund weiter (Symbol im Infobereich).", None, 3000)
            return
        if self._flashing:                     # no tray: wait for the firmware
            e.ignore()
            return
        self._shutdown()
        e.accept()

    def quit(self):
        if self._flashing:                     # half a firmware: finish first
            self._notify("Die Firmware wird gerade installiert; danach beenden.", None, 4000)
            return
        self._quitting = True
        self._shutdown()
        QApplication.quit()

    def _shutdown(self):
        self.clock.stop()
        for d in list(self._send_dialogs) + list(self._ask_dialogs):
            d.close()
        self.session.stop()
        if self.tray:
            self.tray.hide()

    # ---- session events ----------------------------------------------------------

    def _on_event(self, kind, info):
        if kind == "device":
            state = info.get("state")
            if state == "connected":
                self._connected = True
                self._silent = 0
                self.firmware = (info.get("info") or {}).get("firmware")
                self.dot.set_state("green")
                self.device_label.setText("ESP verbunden")
                mac = (info.get("info") or {}).get("mac", "")
                self.device_label.setToolTip(f"ESP {mac}" if mac else "")
                self._set_device_ui(True)       # the session restarts receiving itself
            elif state == "lost":
                self._connected = False
                self._silent = info.get("silent", 0)
                self.dot.set_state("red")
                # An ESP that does not answer twice in a row (not just booting):
                # other or no firmware, or one too old for this app.
                self.device_label.setText("ESP antwortet nicht" if self._silent >= 2
                                          else "Kein ESP")
                self.device_label.setToolTip(info.get("error") or "")
                self._set_device_ui(False)
            elif state == "stopped":
                self._connected = False
                self._set_device_ui(False)
                if self._flashing:             # the dialog has the port for esptool
                    self.dot.set_state("orange")
                    self.device_label.setText("Firmware wird installiert …")
                else:
                    self.device_label.setText("Beendet")
            self._update_firmware_ui()
            if state in ("connected", "lost", "stopped"):
                for d in list(self._send_dialogs):
                    d.on_device(state == "connected")
        elif kind == "power":
            text = power_text(info.get("text", ""))
            self.power_label.setText(f"· {text}" if text else "")
        elif kind == "browse":
            self._browsing = info["on"]
            self.search_btn.setText("Suche läuft …" if info["on"] else "Suchen")
            self.search_btn.setEnabled(not info["on"] and self._connected)
            self._update_devices_ui()
        elif kind == "receiver":
            r = info["receiver"]
            self.devices.upsert(r.id, r.label, r.model, r.present, r.discovered)
            self._update_devices_ui()
            for d in list(self._send_dialogs):
                d.on_receiver(r)
        elif kind == "receiving":
            self._receiving_ui(info["on"], info.get("until"))
        elif kind == "send":
            self._send_row(info)
            for d in list(self._send_dialogs):
                d.on_send(info)
            self._send_notice(info)
        elif kind == "receive":
            self._receive_row(info)
        elif kind == "log":
            self._log(info.get("text", ""))

    def _send_notice(self, info):
        """In the background nobody sees the sheet close: say how it went."""
        if self.isVisible() or self._send_dialogs:
            return
        t, state = info["transfer"], info["state"]
        if state == "done":
            self._notify(f"Gesendet an {t.receiver.label}")
        elif state in ("declined", "failed"):
            self._notify(f"Nicht gesendet an {t.receiver.label}: "
                         f"{'abgelehnt' if state == 'declined' else info.get('error')}")

    # ---- the ESP's firmware ------------------------------------------------------

    def bundled_firmware(self):
        """The firmware the app comes with (flash.Firmware); None if there is none."""
        path = flash.bundled()
        if self._bundled is None or self._bundled[0] != path:
            fw = None
            if path:
                try:
                    fw = flash.load(path)
                except flash.FirmwareError as e:
                    self._log(f"Firmware {path}: {e}")
            self._bundled = (path, fw)
        return self._bundled[1]

    def firmware_text(self):
        """What the ESP runs, in words."""
        if self._flashing:
            return "Wird gerade installiert …"
        if self._connected:
            fw = self.firmware
            if not fw:
                return "EspDrop, eine ältere Firmware ohne Versionsangabe"
            chip = {"esp32c5": "ESP32-C5", "esp32c3": "ESP32-C3"}.get(fw.get("chip"),
                                                                     fw.get("chip"))
            return f"EspDrop {fw['version']}" + (f" auf dem {chip}" if chip else "")
        if self._silent >= 2:
            return "Ein ESP steckt, antwortet aber nicht: keine oder eine andere Firmware"
        return "Kein ESP verbunden"

    def _update_firmware_ui(self):
        bundled = self.bundled_firmware()
        newer = (bundled is not None and self._connected
                 and flash.is_newer(bundled.version, (self.firmware or {}).get("version")))
        text = self.firmware_text()
        if newer:
            text += f" · neu in der App: {bundled.version}"
        self.fw_sub.setText(text)
        self.fw_btn.setText("Aktualisieren …" if newer else "Installieren …")
        self.fw_hint.setVisible(self._silent >= 2 and not self._flashing)

    def open_firmware_dialog(self):
        d = self._fw_dialog
        if d is not None and shiboken6.isValid(d):
            d.raise_()
            d.activateWindow()
            return d
        d = self._fw_dialog = FirmwareDialog(self)
        d.show()
        return d

    def transfer_running(self):
        return (self._recv_row is not None
                or any(t.state not in TRANSFER_DONE for t in self.session.transfers()))

    def set_flashing(self, on):
        self._flashing = on
        self._update_firmware_ui()

    def _log(self, text):
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')} {text}")

    def _set_device_ui(self, connected):
        self.search_btn.setEnabled(connected and not self._browsing)
        self.devices.set_tiles_enabled(connected)
        if not connected:
            self.power_label.setText("")

    def session_browsing(self):
        return self._browsing

    def _update_devices_ui(self):
        has = self.devices.count() > 0
        self.radar.setVisible(not has and self._browsing)
        self.radar_text.setVisible(not has and self._browsing)
        self.radar.set_running(self._browsing)
        self.devices_note.setVisible(not has and not self._browsing)
        self.devices.setVisible(has)
        self.devices_hint.setVisible(has)

    def _tick(self):
        s = getattr(self, "session", None)
        if s is not None and s.receiving:
            until = s.receive_until
            if until:
                left = max(0, until - time.monotonic())
                text = f"Für alle sichtbar – noch {int(left // 60)}:{int(left % 60):02d}"
            else:
                text = "Für alle sichtbar, bis du es ausschaltest."
        else:
            text = "Niemand kann dir gerade etwas senden."
        if self.receive_left.text() != text:
            self.receive_left.setText(text)

    # ---- appearance and the clipboard ------------------------------------------

    def _theme_key(self):
        key = self.settings.value("theme", "auto")
        return key if key in THEME_KEYS else "auto"

    def _theme_picked(self, index):
        key = THEME_KEYS[index]
        self.settings.setValue("theme", key)
        ui.set_mode(QApplication.instance(), key)

    def _to_clipboard(self, files=(), urls=()):
        """Put what arrived on the clipboard, if the user wants that: files as
        files (paste in Explorer or a file manager), a single picture also as
        an image (paste in a chat or a document), links as text."""
        if not self.clipboard_switch.isChecked() or not (files or urls):
            return False
        from PySide6.QtCore import QMimeData
        from PySide6.QtGui import QImage
        mime = QMimeData()
        if files:
            qurls = [QUrl.fromLocalFile(f) for f in files]
            mime.setUrls(qurls)
            # GNOME's file manager pastes files only from its own format.
            mime.setData("x-special/gnome-copied-files",
                         ("copy\n" + "\n".join(u.toString() for u in qurls)).encode("utf-8"))
            if len(files) == 1 and _kind(files[0]) == "photo":
                img = QImage(files[0])
                if not img.isNull():
                    mime.setImageData(img)
        else:
            mime.setText("\n".join(urls))
        QApplication.clipboard().setMimeData(mime)
        return True

    # ---- receiving -------------------------------------------------------------

    def _visibility_picked(self, index):
        _label, secs = VISIBILITY[index]
        self._receiving_ui(bool(index), secs)
        if index:
            run_bg(self.session.set_receiving, True, secs, on_error=self._log)
        else:
            run_bg(self.session.set_receiving, False, on_error=self._log)

    def _receiving_ui(self, on, until):
        index = 0 if not on else (1 if until else 2)
        if self.visibility.index() != index:
            self.visibility.set_index(index)
        if self.tray:
            self.tray_receive.setText("Nicht mehr sichtbar" if on
                                      else "Für alle sichtbar (10 Minuten)")
        self._tick()

    def _on_ask(self, ask, reply):
        # Hidden (autostart, tray): only the question comes up, on its own;
        # the window stays where it is.
        hidden = not self.isVisible()
        self._notify(f"{ask.sender} möchte {what_text(ask.files, ask.urls)} teilen")
        dlg = AskDialog(None if hidden else self, ask, reply, self.bridge.ask_timeout)
        if hidden:
            dlg.setWindowIcon(ui.app_icon())
        self._ask_dialogs.append(dlg)
        dlg.finished.connect(lambda _r, d=dlg: self._ask_dialogs.remove(d)
                             if d in self._ask_dialogs else None)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        self._last_ask_dialog = dlg            # for tests

    def _receive_row(self, info):
        phase = info["phase"]
        ask = info.get("ask")
        if phase == "asked":
            if not info.get("accepted"):
                self._log(f"abgelehnt: {ask.summary() if ask else '?'}")
                return
            if ask and ask.urls and not ask.files:    # a link: no upload follows
                copied = self._to_clipboard(urls=ask.urls)
                self._notify(f"Link von {ask.sender}" + (" – in der Zwischenablage"
                                                         if copied else ""))
            self._recv_n += 1
            row = ui.TransferRow("receive", ", ".join(ask.files or ask.urls) if ask else "?",
                                 ask.sender if ask else "?")
            row.set_state("Wird empfangen …", running=True)
            row.open.connect(lambda r=row: self._open_files(r.files))
            self.transfers.add(row)
            self._rows[("recv", self._recv_n)] = row
            self._recv_row = row
        elif phase == "receiving" and self._recv_row is not None:
            got, total = info["received"], info.get("total")
            text = f"{human(got)} von {human(total)}" if total else human(got)
            self._recv_row.set_state(text, running=True,
                                     progress=min(1.0, got / total) if total else None)
        elif phase == "received":
            row = self._recv_row
            self._recv_row = None
            if row is None:
                return
            files = info.get("files") or []
            if info.get("ok"):
                names = ", ".join(os.path.basename(f) for f in files)
                row.set_title(names)
                row.set_state(f"Empfangen · {human(info.get('size', 0))}", files=files,
                              tone="done")
                copied = self._to_clipboard(files=files)
                self._notify(f"Empfangen: {names}" + (" – in der Zwischenablage"
                                                      if copied else " – klicken zum Öffnen"),
                             files)
            else:
                row.set_state("Unvollständig",
                              files=files + ([info["raw"]] if info.get("raw") else []),
                              tone="bad")

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

    def _name(self):
        """The name others see: what the user typed, else this PC's name."""
        return self.name_edit.text().strip() or env.device_name()

    def _name_changed(self):
        name = self._name()
        self.settings.setValue("name", self.name_edit.text().strip())
        if name == self.session.name:
            return
        run_bg(self.session.set_name, name, on_error=self._log)

    # ---- sending ---------------------------------------------------------------

    def _search(self):
        self.search_btn.setEnabled(False)
        run_bg(self.session.browse, 60, on_error=self._log)

    def _tile_clicked(self, rid):
        tile = self.devices.tiles.get(rid)
        files, _ = QFileDialog.getOpenFileNames(
            self, f"An {tile.name if tile else 'das Gerät'} senden")
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
        row = self._rows.get(key)
        if row is None:
            row = ui.TransferRow("send", ", ".join(os.path.basename(f) for f in t.files),
                                 t.receiver.label)
            row.cancel.connect(lambda tid=t.id: run_bg(self.session.cancel, tid))
            row.open.connect(lambda r=row: self._open_files(r.files))
            self.transfers.add(row)
            self._rows[key] = row
        state = info["state"]
        text = SEND_STATES.get(state, state)
        if state == "failed" and info.get("error"):
            text += f": {info['error']}"
        running = state in ("new", "asking", "uploading")
        tone = "done" if state == "done" else "bad" if state in ("declined", "failed") else None
        row.set_state(text, running=running,
                      progress=info.get("progress") if state == "uploading" else None,
                      files=None if running else t.files, tone=tone)
        tile = self.devices.tiles.get(t.receiver.id)
        if tile:
            tile.set_phase(TILE_PHASE.get(state, "idle"), info.get("progress") or 0.0)

    # ---- settings --------------------------------------------------------------

    def _pick_port(self):
        from .transport import candidates
        ports = ["automatisch"] + candidates()
        current = self.port or "automatisch"
        port, ok = QInputDialog.getItem(self, APP, "Serieller Port des ESP:", ports,
                                        ports.index(current) if current in ports else 0, True)
        if not ok:
            return
        self.port = None if port == "automatisch" else port
        self.settings.setValue("port", self.port or "")
        receiving, until = self.session.receiving, self.session.receive_until
        secs = None if not until else max(1, int(until - time.monotonic()))
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
    """The user's Downloads folder itself (Windows' known folder, XDG on Linux)."""
    from PySide6.QtCore import QStandardPaths
    d = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
    if d:
        return os.path.normpath(d)
    home = os.path.expanduser("~")
    downloads = os.path.join(home, "Downloads")
    return downloads if os.path.isdir(downloads) else home


def _set_app_id():
    """Windows groups windows by app id; without our own the taskbar shows them
    as pythonw.exe with Python's icon instead of ours."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("EspDrop.EspDrop")
        except Exception:                      # noqa: BLE001 - cosmetic only
            pass


def _desktop_setup(settings):
    """Icon file for the desktop, then autostart and the file manager entry:
    on by default at the very first start, kept pointing at this app after.
    ESPDROP_NO_DESKTOP leaves the desktop alone (a test run of a build)."""
    if os.environ.get("ESPDROP_NO_DESKTOP"):
        return
    try:
        integration.ICON = ui.write_icon_file(integration.data_dir())
    except OSError:
        integration.ICON = None
    if not settings.value("defaults_applied", False, type=bool):
        integration.apply_defaults()           # synchronous: the switches show the result
        settings.setValue("defaults_applied", True)
    else:
        run_bg(integration.refresh)


def main(port=None, background=False, send=None):
    """send: None (normal start) or the files of --send (may be empty)."""
    _set_app_id()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP)
    app.setApplicationVersion(__version__)
    if sys.platform.startswith("linux"):
        app.setDesktopFileName("espdrop")      # integration.set_launcher()
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(ui.app_icon())
    ui.apply(app)
    files = [os.path.abspath(f) for f in (send or [])]
    # Already running (autostart, an earlier start)? Hand over and leave.
    if send_to_running({"files": files, "send": send is not None,
                        "show": send is None and not background}):
        return 0
    server = InstanceServer()
    settings = QSettings(APP, APP)
    _desktop_setup(settings)
    w = MainWindow(port=port, settings=settings, background=background)
    server.message.connect(w.handle_message)
    if send is not None:
        w.open_send_dialog(files)
    elif not background or not w.tray:     # no tray to live in: show the window
        w.show()
    rc = app.exec()
    server.close()
    return rc
