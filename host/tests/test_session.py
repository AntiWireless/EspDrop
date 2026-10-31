# EspDrop tests - the long-running Session (the GUI's core) on simulated devices:
# browse with names from /Discover, send with progress and cancel, receive with
# the accept prompt, reconnect after the device is unplugged.
#
#   ~/opendrop-venv/bin/python -m unittest discover -s host/tests
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from apple_mdns import PAD_ID  # noqa: E402
from espdrop import link  # noqa: E402
from espdrop.session import STAGING, Session  # noqa: E402
from fake_device import DuplexBridgeDevice  # noqa: E402


def listing(folder):
    """What the user sees in a download folder (not the staging folder)."""
    return sorted(f for f in os.listdir(folder) if f != STAGING)


class Events:
    def __init__(self):
        self.items = []
        self.cv = threading.Condition()

    def __call__(self, kind, **info):
        with self.cv:
            self.items.append((kind, info))
            self.cv.notify_all()

    def wait(self, pred, secs=15.0):
        with self.cv:
            ok = self.cv.wait_for(lambda: any(pred(k, i) for k, i in self.items), secs)
        if not ok:
            raise AssertionError(f"event not seen; got {[k for k, _ in self.items]}")

    def kinds(self, kind):
        with self.cv:
            return [i for k, i in self.items if k == kind]


def device_state(state):
    return lambda k, i: k == "device" and i.get("state") == state


class SessionCase(unittest.TestCase):
    def setUp(self):
        self.cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        self.events = Events()
        self.session = None

    def tearDown(self):
        if self.session:
            self.session.stop()
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def file(self, name, data):
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as f:
            f.write(data)
        return path


class SendTest(SessionCase):
    def test_browse_names_and_send(self):
        from loopback_receiver import LoopbackReceiver
        payload = b"session send \x00\x01" * 3000                    # ~42 KB
        src = self.file("photo.bin", payload)
        dl = os.path.join(self.tmp.name, "pad")
        os.makedirs(dl)
        os.chdir(dl)                              # the loopback receiver extracts into cwd
        rx = LoopbackReceiver(name="TestPad")
        port = rx.start()
        try:
            fake = DuplexBridgeDevice(target=("127.0.0.1", port), mdns_port=port,
                                      answers={1: ["small"]})
            self.session = s = Session(transport_factory=lambda: fake, receive=False,
                                       on_event=self.events)
            s.start()
            self.events.wait(device_state("connected"))
            s.browse(10)
            self.events.wait(lambda k, i: k == "receiver" and i["receiver"].name == "TestPad")
            (r,) = s.receivers()
            self.assertEqual((r.id, r.label), (PAD_ID, "TestPad"))
            self.assertTrue(any(c.startswith("wake ") for c in fake.commands))
            t = s.send(r.id, [src])
            self.events.wait(lambda k, i: k == "send" and i["state"] in
                             ("done", "failed", "declined"))
            self.assertEqual(t.state, "done", t.error)
            self.assertEqual(t.progress(), 1.0)
            states = [i["state"] for i in self.events.kinds("send")]
            self.assertEqual([x for i, x in enumerate(states) if i == 0 or states[i - 1] != x],
                             ["asking", "uploading", "done"])
            s.stop_browse()
            self.assertEqual(fake.commands[-2:], ["wake off", "power release"])
            self.assertIn("power hold 90", fake.commands)
        finally:
            rx.stop()
        got = [f for f in os.listdir(dl) if open(os.path.join(dl, f), "rb").read() == payload]
        self.assertEqual(got, ["photo.bin"])

    def test_cancel_while_asking(self):
        # A receiver that accepts the connection and never answers: the send
        # hangs in the TLS handshake until it is cancelled.
        lst = socket.socket()
        lst.bind(("127.0.0.1", 0))
        lst.listen(4)
        held = []
        threading.Thread(target=lambda: held.append(lst.accept()), daemon=True).start()
        port = lst.getsockname()[1]
        try:
            fake = DuplexBridgeDevice(target=("127.0.0.1", port), mdns_port=port,
                                      answers={1: ["small"]})
            self.session = s = Session(transport_factory=lambda: fake, receive=False,
                                       on_event=self.events)
            s.start()
            self.events.wait(device_state("connected"))
            s.browse(10)
            self.events.wait(lambda k, i: k == "receiver" and i["action"] == "found")
            t = s.send(PAD_ID, [self.file("a.txt", b"x" * 100)])
            self.events.wait(lambda k, i: k == "send" and i["state"] == "asking")
            time.sleep(0.3)
            s.cancel(t.id)
            self.events.wait(lambda k, i: k == "send" and i["state"] == "cancelled")
        finally:
            lst.close()


class ReceiveTest(SessionCase):
    def incoming(self, fake, name, data, sender="Sender"):
        """A real OpenDrop sender dialling in through the fake device."""
        from espdrop.receive import eui64_linklocal
        from opendrop import client as odc
        from opendrop.client import AirDropClient
        from opendrop.config import AirDropConfig
        import ipaddress
        src = self.file(name, data)
        sock = fake.open_incoming()
        client = AirDropClient(AirDropConfig(computer_name=sender),
                               (str(ipaddress.IPv6Address(eui64_linklocal(fake.mac))), 8771))
        base = odc.HTTPSConnectionAWDL.__init__

        def init(self, *a, **kw):
            base(self, *a, **kw)
            self._create_connection = lambda *a, **kw: sock
        odc.HTTPSConnectionAWDL.__init__ = init
        try:
            if not client.send_ask([src]):
                return False
            return client.send_upload([src])
        finally:
            odc.HTTPSConnectionAWDL.__init__ = base
            if client.http_conn is not None:
                client.http_conn.close()
            sock.close()

    def start(self, on_ask, timeout=60.0):
        fake = DuplexBridgeDevice()
        dl = os.path.join(self.tmp.name, "downloads")
        self.session = s = Session(transport_factory=lambda: fake, receive=True,
                                   on_event=self.events, on_ask=on_ask, ask_timeout=timeout,
                                   download_dir=dl, name="MeinPC")
        s.start()
        self.events.wait(device_state("connected"))
        for _ in range(100):
            if fake.listening == 8771:
                break
            time.sleep(0.05)
        self.assertEqual(fake.listening, 8771)
        return fake, dl

    def test_accepted_file_arrives(self):
        asks = []
        fake, dl = self.start(lambda ask: asks.append(ask) or True)
        payload = b"incoming \xff" * 2000
        self.assertTrue(self.incoming(fake, "bild.jpg", payload))
        self.events.wait(lambda k, i: k == "receive" and i["phase"] == "received")
        (ask,) = asks
        self.assertEqual((ask.sender, ask.files), ("Sender", ["bild.jpg"]))
        (done,) = [i for i in self.events.kinds("receive") if i["phase"] == "received"]
        self.assertTrue(done["ok"])
        self.assertEqual(done["files"], [os.path.join(os.path.realpath(dl), "bild.jpg")])
        with open(done["files"][0], "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_receive_window_closes_and_radio_sleeps(self):
        fake, dl = self.start(lambda ask: True)
        self.assertEqual(fake.commands.count("power hold 90"), 1)   # held while receiving
        self.session.set_receiving(True, for_secs=0.6)
        self.events.wait(lambda k, i: k == "receiving" and not i["on"], secs=5)
        self.assertFalse(self.session.receiving)
        for _ in range(40):
            if fake.commands[-1:] == ["power release"]:
                break
            time.sleep(0.05)
        self.assertEqual(fake.commands[-1], "power release")

    def test_listen_mode_when_the_esp_can_wake_itself(self):
        # A scan-capable ESP: receiving uses `power listen on` (the radio
        # sleeps and wakes on an AirDrop advert), not a continuous hold.
        fake = DuplexBridgeDevice(caps=link.CAP_BLE | link.CAP_IP | link.CAP_SCAN)
        dl = os.path.join(self.tmp.name, "listen")
        self.session = s = Session(transport_factory=lambda: fake, receive=True,
                                   on_event=self.events, on_ask=lambda ask: True,
                                   download_dir=dl, name="MeinPC")
        s.start()
        self.events.wait(device_state("connected"))
        for _ in range(100):
            if fake.listening == 8771:
                break
            time.sleep(0.05)
        self.assertEqual(fake.listening, 8771)
        self.assertIn("power listen on", fake.commands)
        self.assertNotIn("power hold 90", fake.commands)
        # A transfer still arrives (the firmware would have woken AWDL).
        payload = b"listen path \xff" * 500
        self.assertTrue(self.incoming(fake, "l.jpg", payload))
        self.events.wait(lambda k, i: k == "receive" and i["phase"] == "received")
        self.assertIn("l.jpg", os.listdir(dl))
        s.set_receiving(False)
        for _ in range(40):
            if "power listen off" in fake.commands:
                break
            time.sleep(0.05)
        self.assertIn("power listen off", fake.commands)

    def test_nothing_is_overwritten(self):
        # The download folder already has a file of that name, and the same
        # photo comes twice: the user's file stays, the new ones get numbers.
        fake, dl = self.start(lambda ask: True)
        with open(os.path.join(dl, "IMG_1.jpg"), "wb") as f:
            f.write(b"the user's own")
        for k in range(2):
            self.assertTrue(self.incoming(fake, "IMG_1.jpg", b"from the phone %d" % k * 100))
            self.events.wait(lambda kind, i, k=k: kind == "receive" and i["phase"] == "received"
                             and len([x for x in self.events.kinds("receive")
                                      if x["phase"] == "received"]) == k + 1)
        self.assertEqual(listing(dl), ["IMG_1 (1).jpg", "IMG_1 (2).jpg", "IMG_1.jpg"])
        with open(os.path.join(dl, "IMG_1.jpg"), "rb") as f:
            self.assertEqual(f.read(), b"the user's own")
        done = [i for i in self.events.kinds("receive") if i["phase"] == "received"]
        self.assertEqual([os.path.basename(i["files"][0]) for i in done],
                         ["IMG_1 (1).jpg", "IMG_1 (2).jpg"])
        self.assertEqual(os.listdir(os.path.join(dl, STAGING)), [])

    def test_move_out_keeps_what_cannot_move(self):
        from espdrop import receive
        staging = os.path.join(self.tmp.name, "staging")
        dest = os.path.join(self.tmp.name, "dest")
        os.makedirs(staging)
        os.makedirs(dest)
        for n in ("a.jpg", "locked.jpg"):
            open(os.path.join(staging, n), "wb").close()
        real = receive.shutil.move

        def move(src, dst):
            if src.endswith("locked.jpg"):
                raise PermissionError("in use")
            return real(src, dst)
        with mock.patch.object(receive.shutil, "move", move), \
                self.assertLogs("espdrop.receive", "WARNING"):
            got = receive.move_out(staging, ["a.jpg", "locked.jpg"], dest)
        self.assertEqual(got, [os.path.join(dest, "a.jpg"), os.path.join(staging, "locked.jpg")])
        self.assertEqual(os.listdir(staging), ["locked.jpg"])

    def test_leftovers_move_out_at_start(self):
        dl = os.path.join(self.tmp.name, "downloads")
        os.makedirs(os.path.join(dl, STAGING))
        with open(os.path.join(dl, STAGING, "halb.jpg"), "wb") as f:
            f.write(b"left by a crash")
        s = Session(transport_factory=lambda: DuplexBridgeDevice(), receive=False,
                    on_event=self.events, download_dir=dl)
        self.session = s
        s.start()
        self.assertEqual(listing(dl), ["halb.jpg"])
        self.assertEqual(os.path.realpath(os.getcwd()),
                         os.path.realpath(os.path.join(dl, STAGING)))

    def test_declined_and_unanswered(self):
        fake, dl = self.start(lambda ask: False)
        self.assertFalse(self.incoming(fake, "nein.txt", b"no"))
        self.assertEqual(listing(dl), [])
        asked = [i for i in self.events.kinds("receive") if i["phase"] == "asked"]
        self.assertEqual([i["accepted"] for i in asked], [False])

    def test_unanswered_ask_is_declined(self):
        fake, dl = self.start(lambda ask: time.sleep(5) or True, timeout=0.5)
        t = time.monotonic()
        self.assertFalse(self.incoming(fake, "spaet.txt", b"late"))
        self.assertLess(time.monotonic() - t, 4)
        self.assertEqual(listing(dl), [])


class ChoiceTest(SessionCase):
    """espdrop send picks the receiver by name (from /Discover) or id."""

    def test_match_receiver(self):
        from espdrop.airdrop import match_receiver

        class R:
            def __init__(self, id):
                self.id = id
        a, b = R("aaaa11112222"), R("bbbb33334444")
        found = [(a, {"name": "Bens iPad"}), (b, {"name": "Bens iPhone"})]
        self.assertEqual(match_receiver(found, "bens ipad"), [found[0]])     # exact, any case
        self.assertEqual(match_receiver(found, "BBBB33334444"), [found[1]])  # id
        self.assertEqual(match_receiver(found, "iPh"), [found[1]])           # part of a name
        self.assertEqual(len(match_receiver(found, "Bens")), 2)              # ambiguous
        self.assertEqual(match_receiver(found, "Anna"), [])

    def test_send_by_name(self):
        import contextlib
        import io
        from espdrop import cli
        from fake_device import TcpBridgeDevice
        from loopback_receiver import LoopbackReceiver
        src = self.file("doc.txt", b"by name" * 100)
        dl = os.path.join(self.tmp.name, "pad")
        os.makedirs(dl)
        os.chdir(dl)
        rx = LoopbackReceiver(name="Annas iPad")
        port = rx.start()
        out = io.StringIO()
        try:
            fake = TcpBridgeDevice(target=("127.0.0.1", port), mdns_port=port,
                                   answers={1: ["small"]})
            with contextlib.redirect_stdout(out):
                rc = cli.main(["send", "-t", "5", "--no-wake", "-r", "annas ipad", src],
                              transport=fake)
        finally:
            rx.stop()
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("found 'Annas iPad'", out.getvalue())
        self.assertIn("doc.txt", os.listdir(dl))

    def test_several_without_terminal(self):
        from espdrop import cli
        with contextlib_stdin_closed():
            self.assertIsNone(cli._pick([(type("R", (), {"id": "x1"})(), {"name": "A"}),
                                         (type("R", (), {"id": "x2"})(), {})]))


class contextlib_stdin_closed:
    """stdin that is not a terminal, for _pick."""

    def __enter__(self):
        import io
        self.old, sys.stdin = sys.stdin, io.StringIO("")
        self.err, sys.stderr = sys.stderr, io.StringIO()
        self.out, sys.stdout = sys.stdout, io.StringIO()

    def __exit__(self, *exc):
        sys.stdin, sys.stderr, sys.stdout = self.old, self.err, self.out


class ReconnectTest(SessionCase):
    def test_unplug_and_replug(self):
        devices = [DuplexBridgeDevice(), DuplexBridgeDevice(mac=bytes.fromhex("020000000002"))]
        made = []

        def factory():
            if not devices:
                raise OSError("no ESP found")
            made.append(devices.pop(0))
            return made[-1]
        self.session = s = Session(transport_factory=factory, receive=True,
                                   on_event=self.events)
        s.start()
        self.events.wait(device_state("connected"))
        made[0].unplug()
        self.events.wait(device_state("lost"))
        self.events.wait(lambda k, i: k == "device" and i.get("state") == "connected"
                         and i["info"]["mac"] == "02:00:00:00:00:02")
        self.assertEqual(made[1].mode, link.MODE_IP)
        for _ in range(100):
            if made[1].listening == 8771:
                break
            time.sleep(0.05)
        self.assertEqual(made[1].listening, 8771, "receiving should restart on the new device")


class FirmwareTest(SessionCase):
    def connected_info(self, fake):
        events = Events()
        s = Session(transport_factory=lambda: fake, receive=False, on_event=events)
        s.start()
        try:
            events.wait(device_state("connected"))
            return [i for i in events.kinds("device") if i["state"] == "connected"][0]["info"]
        finally:
            s.stop()

    def test_version_on_connect(self):
        from espdrop.version import __version__
        fw = self.connected_info(DuplexBridgeDevice())["firmware"]
        self.assertEqual((fw["version"], fw["chip"]), (__version__, "esp32c5"))
        # Firmware from before the command: connected all the same, version unknown.
        self.assertIsNone(self.connected_info(DuplexBridgeDevice(fw_version=None))["firmware"])

    def test_an_esp_that_does_not_answer(self):
        # Another firmware (here: another protocol) fails every HELLO; `silent`
        # counts them, so the app can offer to flash ours. No ESP at all: 0.
        with mock.patch("espdrop.session.RECONNECT_SECS", 0.05):
            self.session = s = Session(transport_factory=lambda: DuplexBridgeDevice(version=99),
                                       receive=False, on_event=self.events)
            s.start()
            self.events.wait(lambda k, i: k == "device" and i.get("silent", 0) >= 3)
            s.stop()

            def absent():
                raise OSError("no ESP found")
            events = Events()
            self.session = s = Session(transport_factory=absent, receive=False, on_event=events)
            s.start()
            events.wait(lambda k, i: len([x for x in events.kinds("device")
                                          if x["state"] == "lost"]) >= 3)
            s.stop()
        self.assertEqual({i["silent"] for i in events.kinds("device") if i["state"] == "lost"}, {0})


if __name__ == "__main__":
    unittest.main()
