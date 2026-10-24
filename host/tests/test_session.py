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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from apple_mdns import PAD_ID  # noqa: E402
from espdrop import link  # noqa: E402
from espdrop.session import Session  # noqa: E402
from fake_device import DuplexBridgeDevice  # noqa: E402


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
            self.assertEqual(fake.commands[-1], "wake off")
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

    def test_declined_and_unanswered(self):
        fake, dl = self.start(lambda ask: False)
        self.assertFalse(self.incoming(fake, "nein.txt", b"no"))
        self.assertEqual(os.listdir(dl), [])
        asked = [i for i in self.events.kinds("receive") if i["phase"] == "asked"]
        self.assertEqual([i["accepted"] for i in asked], [False])

    def test_unanswered_ask_is_declined(self):
        fake, dl = self.start(lambda ask: time.sleep(5) or True, timeout=0.5)
        t = time.monotonic()
        self.assertFalse(self.incoming(fake, "spaet.txt", b"late"))
        self.assertLess(time.monotonic() - t, 4)
        self.assertEqual(os.listdir(dl), [])


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


if __name__ == "__main__":
    unittest.main()
