# EspDrop tests - host app against the simulated device (no hardware).
#
#   ~/opendrop-venv/bin/python -m unittest discover -s host/tests -t host
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import contextlib
import io
import ipaddress
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from zeroconf import DNSIncoming  # noqa: E402

from apple_mdns import PAD_ID, PAD_IP, response  # noqa: E402
from espdrop import cli, link  # noqa: E402
from espdrop.device import Device, DeviceError  # noqa: E402
from espdrop.mdns import AIRDROP, MDNS_GROUP, Browser, txt_properties  # noqa: E402
from fake_device import FakeDevice  # noqa: E402


def wait_for(cond, secs=3.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class LinkTest(unittest.TestCase):
    def test_cobs_roundtrip(self):
        for data in (b"", b"\x00", b"\x00\x00", bytes(range(256)) * 3, b"\x11" * 600):
            self.assertEqual(link.cobs_decode(link.cobs_encode(data)), data)
            self.assertNotIn(0, link.cobs_encode(data))

    def test_reader_splits_and_resyncs(self):
        frames = link.frame(link.MSG_CMD, b"mac") + link.frame(link.MSG_LOG, b"x\x00y")
        # Garbage ended by a delimiter (what Device sends first): both survive,
        # also when the stream arrives in odd pieces.
        stream = b"garbage\x00" + frames
        r = link.Reader()
        got = r.feed(stream[:9]) + r.feed(stream[9:13]) + r.feed(stream[13:])
        self.assertEqual(got, [(link.MSG_CMD, b"mac"), (link.MSG_LOG, b"x\x00y")])
        # Without it the first frame is lost, and the stream resyncs at its end.
        got = link.Reader().feed(b"garbage" + frames)
        self.assertEqual(got[-1], (link.MSG_LOG, b"x\x00y"))
        self.assertNotIn((link.MSG_CMD, b"mac"), got)

    def test_udp_messages(self):
        dst = ipaddress.IPv6Address("ff02::fb").packed
        f = link.udp_send(5353, dst, 5353, b"hello")
        (t, p), = link.Reader().feed(f)
        self.assertEqual(t, link.MSG_UDP_SEND)
        self.assertEqual(p[2:18], dst)
        recv = (b"\xe9\x14" + PAD_IP.packed + b"\xe9\x14\x01" + b"data")
        self.assertEqual(link.parse_udp_recv(recv), (5353, PAD_IP.packed, 5353, True, b"data"))
        self.assertEqual(link.parse_result(bytes([24, 0xe9, 0x14, 0xfe])), (24, 5353, -2))


class DeviceTest(unittest.TestCase):
    def test_hello_and_cmd(self):
        fake = FakeDevice()
        dev = Device(fake)
        h = dev.hello(link.MODE_IP)
        self.assertEqual((h["mode"], h["mac"], h["max_chunk"]), (1, "02:00:00:00:00:01", 1400))
        self.assertEqual(dev.cmd("node"), "ok node")
        dev.close()

    def test_version_mismatch(self):
        dev = Device(FakeDevice(version=2))
        with self.assertRaises(DeviceError):
            dev.hello(link.MODE_IP)
        dev.close()

    def test_ip_mode_refused(self):
        dev = Device(FakeDevice(ip_ok=False))
        with self.assertRaises(DeviceError):
            dev.hello(link.MODE_IP)
        dev.close()

    def test_udp_open_needs_ip_mode(self):
        dev = Device(FakeDevice())
        dev.hello(link.MODE_L2)
        with self.assertRaises(DeviceError):
            dev.udp_open(5353, lambda *a: None)
        dev.close()


class BrowserTest(unittest.TestCase):
    def run_browser(self, answers, secs=1.0, interval=0.1):
        fake = FakeDevice(answers=answers)
        dev = Device(fake)
        dev.hello(link.MODE_IP)
        events = []
        b = Browser(dev, on_found=lambda r: events.append(("found", r.id)),
                    on_gone=lambda r: events.append(("gone", r.id)), interval=interval)
        b.start()
        time.sleep(secs)
        b.stop()
        dev.close()
        return fake, b, events

    def test_small_answer(self):
        fake, b, events = self.run_browser({2: ["small"]})
        self.assertEqual(events, [("found", PAD_ID)])
        (r,) = b.receivers()
        self.assertEqual((r.port, r.parsed_addresses(), r.flags, r.properties[b"flags"]),
                         (8770, [str(PAD_IP)], 111611, b"111611"))
        self.assertTrue(r.server.startswith("00000000-1111"))
        self.assertEqual(r.name, f"{PAD_ID}._airdrop._tcp.local.")

    def test_full_announcement_then_goodbye(self):
        fake, b, events = self.run_browser({1: ["full"], 3: ["goodbye"]})
        self.assertEqual(events, [("found", PAD_ID), ("gone", PAD_ID)])
        self.assertEqual(b.receivers(), [])

    def test_found_once_although_repeated(self):
        fake, b, events = self.run_browser({1: ["small"], 2: ["full"], 3: ["small"]})
        self.assertEqual(events, [("found", PAD_ID)])

    def test_other_services_ignored(self):
        fake, b, events = self.run_browser({1: ["other"]})
        self.assertEqual((events, b.receivers()), ([], []))
        self.assertEqual(b.invalid, 0)

    def test_garbage_counted_not_fatal(self):
        fake = FakeDevice()
        dev = Device(fake)
        dev.hello(link.MODE_IP)
        b = Browser(dev, interval=10)
        b.start()
        fake.deliver(b"\x00\x00\x84\x00\x00\x00\x00\x05\x00\x00\x00\x00\xff")   # truncated
        fake.deliver(response("small"))
        self.assertTrue(wait_for(lambda: b.receivers()))
        b.stop()
        dev.close()
        self.assertEqual(b.invalid, 1)

    def test_queries_repeat_and_are_valid(self):
        fake, b, events = self.run_browser({}, secs=0.55, interval=0.1)
        self.assertGreaterEqual(len(fake.queries), 4)
        dst, dport, _ = fake.sent[0]
        self.assertEqual((dst, dport), (MDNS_GROUP, 5353))
        q = DNSIncoming(fake.queries[0])
        self.assertTrue(q.valid and q.is_query())
        self.assertEqual([(x.name, x.type) for x in q.questions], [(AIRDROP, 12)])

    def test_txt_properties(self):
        self.assertEqual(txt_properties(b"\x0cflags=111611\x04a=b=\x00\x03key"),
                         {b"flags": b"111611", b"a": b"b=", b"key": b""})


class CliTest(unittest.TestCase):
    def run_cli(self, argv, fake):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv, transport=fake)
        return rc, out.getvalue(), err.getvalue()

    def test_find(self):
        fake = FakeDevice(answers={1: ["small"]})
        rc, out, err = self.run_cli(["find", "-t", "2"], fake)
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"found  {PAD_ID}  [{PAD_IP}]:8770  flags 111611", out)
        self.assertIn("wake 12", fake.commands)
        self.assertEqual(fake.commands[-1], "wake off")
        self.assertTrue(fake.closed)

    def test_find_nothing(self):
        fake = FakeDevice()
        rc, out, err = self.run_cli(["find", "-t", "1", "--no-wake"], fake)
        self.assertEqual(rc, 1)
        self.assertIn("0 receiver(s)", out)
        self.assertFalse([c for c in fake.commands if c.startswith("wake")])

    def test_find_without_bluetooth(self):
        fake = FakeDevice(caps=link.CAP_IP, answers={1: ["small"]})
        rc, out, err = self.run_cli(["find", "-t", "1"], fake)
        self.assertEqual(rc, 0)
        self.assertIn("no Bluetooth wake", out)
        self.assertFalse([c for c in fake.commands if c.startswith("wake")])

    def test_old_firmware(self):
        class Silent(FakeDevice):
            def _handle(self, t, p):
                pass
        rc, out, err = self.run_cli(["status"], Silent())
        self.assertEqual(rc, 1)
        self.assertIn("does not answer HELLO", err)


if __name__ == "__main__":
    unittest.main()
