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

    def window(self, fake):
        from espdrop.session import Session

        def factory(w):
            return Session(transport_factory=lambda: fake, name="MeinPC", receive=False,
                           download_dir=self.dl, on_event=w.bridge.on_event,
                           on_ask=w.bridge.on_ask, ask_timeout=w.bridge.ask_timeout)
        self.w = self.gui.MainWindow(settings=self.settings, session_factory=factory,
                                     ask_timeout=20.0, tray=False)
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
            w.search_btn.click()
            self.assertTrue(self.pump(lambda: w.receivers_list.count() == 1
                                      and "TestPad" in w.receivers_list.item(0).text()))
            os.chdir(pad)                      # the loopback receiver writes into cwd
            w._send_to(w._selected_receiver(), [src])
            row = lambda: w.transfers.topLevelItem(0)              # noqa: E731
            self.assertTrue(self.pump(lambda: row() and row().text(3) in
                                      ("gesendet", "fehlgeschlagen", "abgelehnt")))
            self.assertEqual(row().text(3), "gesendet")
            self.assertEqual((row().text(1), row().text(2)), ("TestPad", "foto.jpg"))
        finally:
            rx.stop()
        self.assertIn("foto.jpg", os.listdir(pad))

    def answer_incoming(self, accept):
        fake = DuplexBridgeDevice()
        w = self.window(fake)
        w.receive_btn.click()                  # empfangsbereit (10 Minuten)
        self.assertTrue(self.pump(lambda: fake.listening == 8771))
        self.assertTrue(w.session.receiving)
        self.assertIsNotNone(w.session.receive_until)
        src = self.file("bild.jpg", b"incoming via the dialog " * 300)
        result = {}
        th = threading.Thread(target=lambda: result.setdefault("ok", incoming(fake, src)))
        th.start()
        self.assertTrue(self.pump(lambda: getattr(w, "_last_ask_dialog", None) is not None))
        dlg = w._last_ask_dialog
        self.assertIn("Sender", dlg.findChildren(type(w.device_label))[0].text()
                      + "".join(lbl.text() for lbl in dlg.findChildren(type(w.device_label))))
        (dlg.yes if accept else dlg.no).click()
        self.assertTrue(self.pump(lambda: not th.is_alive(), 20))
        return w, result.get("ok")

    def test_receive_through_the_dialog(self):
        w, ok = self.answer_incoming(True)
        self.assertTrue(ok)
        row = lambda: w.transfers.topLevelItem(0)                  # noqa: E731
        self.assertTrue(self.pump(lambda: row() and row().text(3).startswith("empfangen")))
        self.assertEqual(row().text(2), "bild.jpg")
        self.assertIn("bild.jpg", os.listdir(self.dl))

    def test_declined_in_the_dialog(self):
        w, ok = self.answer_incoming(False)
        self.assertFalse(ok)
        self.assertEqual(os.listdir(self.dl), [])
        self.assertEqual(w.transfers.topLevelItemCount(), 0)

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
