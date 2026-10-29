# EspDrop tests - the PySide6 app, offscreen, on simulated devices: search and
# send, receive through the accept dialog, decline. Skipped without PySide6.
#
#   ~/opendrop-venv/bin/python -m unittest discover -s host/tests
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import ipaddress
import os
import sys
import tempfile
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

try:
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:                            # pragma: no cover - no PySide6 here
    HAVE_QT = False

from fake_device import DuplexBridgeDevice  # noqa: E402


def incoming(fake, path, sender="Sender"):
    """A real OpenDrop sender dialling in through the fake device."""
    from espdrop.receive import eui64_linklocal
    from opendrop import client as odc
    from opendrop.client import AirDropClient
    from opendrop.config import AirDropConfig
    sock = fake.open_incoming()
    client = AirDropClient(AirDropConfig(computer_name=sender),
                           (str(ipaddress.IPv6Address(eui64_linklocal(fake.mac))), 8771))
    base = odc.HTTPSConnectionAWDL.__init__

    def init(self, *a, **kw):
        base(self, *a, **kw)
        self._create_connection = lambda *a, **kw: sock
    odc.HTTPSConnectionAWDL.__init__ = init
    try:
        return client.send_ask([path]) and client.send_upload([path])
    finally:
        odc.HTTPSConnectionAWDL.__init__ = base
        if client.http_conn is not None:
            client.http_conn.close()
        sock.close()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class GuiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from espdrop import gui
        self.gui = gui
        self.cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        self.dl = os.path.join(self.tmp.name, "empfangen")
        self.settings = QSettings(os.path.join(self.tmp.name, "espdrop.ini"), QSettings.IniFormat)
        self.w = None

    def tearDown(self):
        if self.w:
            self.w.quit()
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def pump(self, cond, secs=15.0):
        end = time.monotonic() + secs
        while time.monotonic() < end:
            self.app.processEvents()
            if cond():
                return True
            time.sleep(0.02)
        return cond()

    def window(self, fake, background=False):
        from espdrop.session import Session

        def factory(w):
            return Session(transport_factory=lambda: fake, name="MeinPC", receive=False,
                           download_dir=self.dl, on_event=w.bridge.on_event,
                           on_ask=w.bridge.on_ask, ask_timeout=w.bridge.ask_timeout)
        self.w = self.gui.MainWindow(settings=self.settings, session_factory=factory,
                                     ask_timeout=20.0, tray=False, background=background)
        if not background:
            self.w.show()
        self.assertTrue(self.pump(lambda: self.w.device_label.text().startswith("ESP verbunden")),
                        self.w.device_label.text())
        return self.w

    def file(self, name, data):
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_search_and_send(self):
        from loopback_receiver import LoopbackReceiver
        pad = os.path.join(self.tmp.name, "pad")
        os.makedirs(pad)
        rx = LoopbackReceiver(name="TestPad")
        port = rx.start()
        try:
            fake = DuplexBridgeDevice(target=("127.0.0.1", port), mdns_port=port,
                                      answers={1: ["small"]})
            w = self.window(fake)
            src = self.file("foto.jpg", b"\xff\xd8 gui send " * 500)
            self.assertFalse(w.radar.isVisible())              # not searching yet
            w.search_btn.click()
            self.assertTrue(self.pump(lambda: w.devices.count() == 1 and next(
                iter(w.devices.tiles.values())).name == "TestPad"))
            (rid, tile), = w.devices.tiles.items()
            self.assertEqual(tile.caption(), "OpenDrop")        # its /Discover model
            os.chdir(pad)                      # the loopback receiver writes into cwd
            w._send_to(rid, [src])
            row = lambda: w.transfers.rows[0] if w.transfers.rows else None  # noqa: E731
            self.assertTrue(self.pump(lambda: row() and row().status_text() in
                                      ("Gesendet", "Fehlgeschlagen", "Abgelehnt")))
            self.assertEqual(row().status_text(), "Gesendet")
            self.assertEqual((row().device, row().title_text()), ("TestPad", "foto.jpg"))
            self.assertEqual(tile.phase, "sent")               # the avatar says so too
            self.assertTrue(row().open_btn.isVisibleTo(w))
        finally:
            rx.stop()
        self.assertIn("foto.jpg", os.listdir(pad))

    def answer_incoming(self, accept):
        fake = DuplexBridgeDevice()
        w = self.window(fake)
        w.visibility.select(1)                 # "10 Minuten", as a click
        self.assertTrue(self.pump(lambda: fake.listening == 8771))
        self.assertTrue(w.session.receiving)
        self.assertIsNotNone(w.session.receive_until)
        self.assertTrue(self.pump(lambda: w.receive_left.text().startswith(
            "Für alle sichtbar – noch")), w.receive_left.text())
        src = self.file("bild.jpg", b"incoming via the dialog " * 300)
        result = {}
        th = threading.Thread(target=lambda: result.setdefault("ok", incoming(fake, src)))
        th.start()
        self.assertTrue(self.pump(lambda: getattr(w, "_last_ask_dialog", None) is not None))
        dlg = w._last_ask_dialog
        texts = [lbl.text() for lbl in dlg.findChildren(type(w.device_label))]
        self.assertIn("„Sender“ möchte ein Foto teilen.", texts)
        (dlg.yes if accept else dlg.no).click()
        self.assertTrue(self.pump(lambda: not th.is_alive(), 20))
        return w, result.get("ok")

    def test_receive_through_the_dialog(self):
        w, ok = self.answer_incoming(True)
        self.assertTrue(ok)
        row = lambda: w.transfers.rows[0] if w.transfers.rows else None  # noqa: E731
        self.assertTrue(self.pump(lambda: row() and row().status_text().startswith("Empfangen")))
        self.assertEqual((row().title_text(), row().device), ("bild.jpg", "Sender"))
        self.assertIn("bild.jpg", os.listdir(self.dl))

    def test_declined_in_the_dialog(self):
        w, ok = self.answer_incoming(False)
        self.assertFalse(ok)
        self.assertEqual(os.listdir(self.dl), [])
        self.assertEqual(w.transfers.count(), 0)
        self.assertTrue(w.transfers.placeholder.isVisibleTo(w))

    def test_background_shows_only_the_question(self):
        # Autostart: no window, receiving until switched off; an incoming
        # transfer brings up its dialog on its own, the window stays hidden.
        fake = DuplexBridgeDevice()
        w = self.window(fake, background=True)
        self.assertFalse(w.isVisible())
        self.assertTrue(self.pump(lambda: fake.listening == 8771))
        self.assertTrue(w.session.receiving)
        self.assertIsNone(w.session.receive_until)            # dauerhaft
        self.assertEqual(w.visibility.index(), 2)              # "Für alle"
        src = self.file("hintergrund.jpg", b"arrives while hidden " * 200)
        result = {}
        th = threading.Thread(target=lambda: result.setdefault("ok", incoming(fake, src)))
        th.start()
        self.assertTrue(self.pump(lambda: getattr(w, "_last_ask_dialog", None) is not None))
        dlg = w._last_ask_dialog
        self.assertTrue(dlg.isVisible())
        self.assertIsNone(dlg.parent())                        # its own window
        self.assertFalse(w.isVisible())
        dlg.yes.click()
        self.assertTrue(self.pump(lambda: not th.is_alive(), 20))
        self.assertTrue(result.get("ok"))
        self.assertTrue(self.pump(lambda: w._notify_files is not None))
        self.assertEqual([os.path.basename(f) for f in w._notify_files], ["hintergrund.jpg"])
        self.assertFalse(w.isVisible())

    def test_send_dialog_from_the_share_menu(self):
        from loopback_receiver import LoopbackReceiver
        pad = os.path.join(self.tmp.name, "pad")
        os.makedirs(pad)
        rx = LoopbackReceiver(name="TestPad")
        port = rx.start()
        try:
            fake = DuplexBridgeDevice(target=("127.0.0.1", port), mdns_port=port,
                                      answers={1: ["small"]})
            w = self.window(fake, background=True)
            a = self.file("a.txt", b"first " * 100)
            b = self.file("b.txt", b"second " * 100)
            # Explorer's context menu starts one copy per file: both land in
            # the same dialog.
            w.handle_message({"files": [a], "send": True})
            w.handle_message({"files": [b], "send": True})
            (d,) = w._send_dialogs
            self.assertEqual([os.path.basename(f) for f in d.files], ["a.txt", "b.txt"])
            self.assertEqual(d.title.full_text(), "2 Dateien")
            self.assertTrue(self.pump(lambda: d.grid.count() == 1 and next(
                iter(d.grid.tiles.values())).name == "TestPad"))
            self.assertFalse(d.radar.isVisible())              # devices replace the radar
            (rid, tile), = d.grid.tiles.items()
            os.chdir(pad)                      # the loopback receiver writes into cwd
            d._pick(rid)
            self.assertIsNotNone(d.transfer_id)
            self.assertIn(tile.phase, ("waiting", "sending", "sent"))
            self.assertTrue(self.pump(lambda: d not in w._send_dialogs, 20),
                            d.status.text())
            self.assertFalse(w.isVisible())
        finally:
            rx.stop()
        self.assertEqual(sorted(f for f in os.listdir(pad) if f.endswith(".txt")),
                         ["a.txt", "b.txt"])

    def test_single_instance(self):
        name = f"espdrop-test-{os.getpid()}-{time.monotonic_ns()}"
        self.assertFalse(self.gui.send_to_running({"show": True}, name=name, timeout_ms=200))
        server = self.gui.InstanceServer(name)
        got = []
        server.message.connect(got.append)
        try:
            msg = {"files": ["/tmp/x y.jpg"], "send": True, "show": False}
            self.assertTrue(self.gui.send_to_running(msg, name=name))
            self.assertTrue(self.pump(lambda: got, 5))
            self.assertEqual(got, [msg])
        finally:
            server.close()

    def test_what_text(self):
        w = self.gui.what_text
        self.assertEqual(w(["a.jpg"]), "ein Foto")
        self.assertEqual(w(["a.JPG", "b.heic"]), "2 Fotos")
        self.assertEqual(w(["a.mov"]), "ein Video")
        self.assertEqual(w(["a.jpg", "b.pdf"]), "2 Dateien")
        self.assertEqual(w([], ["https://example.org"]), "einen Link")
        self.assertEqual(w([]), "etwas")

    def test_controls(self):
        from espdrop import ui
        seg = ui.Segmented(["Aus", "10 Minuten", "Für alle"])
        picked = []
        seg.changed.connect(picked.append)
        seg.set_index(2)                       # programmatic: no signal
        seg.select(1)                          # as a click
        seg.select(1)
        self.assertEqual((seg.index(), picked), (1, [1]))
        sw = ui.Switch()
        flips = []
        sw.toggled.connect(flips.append)
        sw.set_quiet(True)
        sw.click()
        self.assertEqual((sw.isChecked(), flips), (False, [False]))
        self.assertEqual(ui.pretty_model("iPad13,1"), "iPad")
        self.assertEqual(ui.device_kind("iPhone15,2", None), "phone")
        self.assertEqual(ui.device_kind(None, "MacBook von Anna"), "mac")
        self.assertEqual(ui.initials("Bens iPad"), "BI")
        for dark in (False, True):             # both looks build
            ui.T.clear()
            ui.T.update(ui.DARK if dark else ui.LIGHT)
            self.assertIn(ui.T["accent"], ui.qss())
        ui.refresh(self.app)

    def test_power_text(self):
        t = self.gui.power_text
        self.assertEqual(t("power auto asleep (none) hold=0s wake=0s"), "Funk aus (spart Strom)")
        self.assertEqual(t("power auto awake (hold) hold=80s"), "Funk an (für die App)")
        self.assertEqual(t("power on awake (on)"), "Funk immer an")
        self.assertEqual(t("power auto asleep (none) hold=0s wake=0s listen=on tcp=0"),
                         "Funk schläft (empfangsbereit)")
        self.assertEqual(t("power auto awake (advert) hold=0s wake=0s listen=on tcp=0"),
                         "Funk an (Gerät in der Nähe)")
        self.assertEqual(t("ok power"), "")


if __name__ == "__main__":
    unittest.main()
