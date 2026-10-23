# EspDrop tests - a simulated ESP for the host app, no hardware needed.
#
# Behaves like the bridge firmware's IP mode at the USB-message level
# (docs/M4_ENTWURF.md, section 4): answers HELLO and text commands, opens UDP
# ports and, when the host sends mDNS queries for _airdrop._tcp, answers with
# canned iPad responses (apple_mdns.py) as UDP_RECV messages.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import queue
import struct
import threading

from espdrop import link

try:
    from apple_mdns import PAD_IP, response
except ImportError:  # imported as tests.fake_device
    from .apple_mdns import PAD_IP, response


class FakeDevice:
    """A transport (read/write/close) with a device behind it."""

    def __init__(self, answers=None, caps=link.CAP_BLE | link.CAP_IP, version=link.PROTO_VERSION,
                 ip_ok=True, mac=bytes.fromhex("020000000001")):
        # answers: {n: [kind, ...]} - after the n-th AirDrop query (1-based) the
        # device delivers these apple_mdns.response() kinds from the iPad.
        self.answers = answers or {}
        self.mdns_port = 8770          # SRV port in the canned iPad answers
        self.caps, self.version, self.ip_ok, self.mac = caps, version, ip_ok, mac
        self.mode = link.MODE_L2
        self.udp_ports = set()
        self.commands = []
        self.queries = []          # payloads of AirDrop queries the host sent
        self.sent = []             # (dst16, dport, data) of every UDP_SEND
        self._reader = link.Reader()
        self._out = queue.Queue()
        self._lock = threading.Lock()
        self.closed = False

    # ---- transport API -----------------------------------------------------

    def read(self, timeout):
        try:
            return self._out.get(timeout=timeout)
        except queue.Empty:
            return b""

    def write(self, data, timeout=5.0):
        with self._lock:
            for t, p in self._reader.feed(data):
                self._handle(t, p)

    def close(self):
        self.closed = True

    # ---- device behaviour --------------------------------------------------

    def _emit(self, t, payload):
        self._out.put(link.frame(t, payload))

    def _handle(self, t, p):
        if t == link.MSG_HELLO:
            if len(p) >= 2 and p[0] == self.version and (p[1] == link.MODE_L2 or self.ip_ok):
                self.mode = p[1]
            else:
                self.mode = link.MODE_L2
            self.udp_ports.clear()
            self._emit(link.MSG_HELLO_REPLY, bytes([self.version, self.mode]) + self.mac
                       + struct.pack("<BHB", 0, 1400, self.caps))
        elif t == link.MSG_CMD:
            text = p.decode()
            self.commands.append(text)
            if text.startswith("wake "):
                arg = text.split()[1]
                reply = "wake off" if arg == "off" else f"wake on {arg} s left"
            elif text == "wake":
                reply = "wake off"
            else:
                reply = f"ok {text}"
            self._emit(link.MSG_CMD_REPLY, reply.encode())
        elif t == link.MSG_UDP_OPEN:
            port = struct.unpack_from("<H", p)[0]
            err = 0 if self.mode == link.MODE_IP else -2
            if not err:
                self.udp_ports.add(port)
            self._emit(link.MSG_RESULT, struct.pack("<BHb", link.MSG_UDP_OPEN, port, err))
        elif t == link.MSG_UDP_CLOSE:
            self.udp_ports.discard(struct.unpack_from("<H", p)[0])
        elif t == link.MSG_UDP_SEND:
            port = struct.unpack_from("<H", p)[0]
            dst, dport, data = p[2:18], struct.unpack_from("<H", p, 18)[0], p[20:]
            self.sent.append((dst, dport, data))
            if port not in self.udp_ports:
                self._emit(link.MSG_RESULT, struct.pack("<BHb", link.MSG_UDP_SEND, port, -6))
                return
            is_query = len(data) >= 12 and not data[2] & 0x80
            if is_query and b"_airdrop" in data:
                self.queries.append(data)
                for kind in self.answers.get(len(self.queries), []):
                    self.deliver(response(kind, port=self.mdns_port))

    def deliver(self, data, src=PAD_IP.packed, sport=5353, multicast=True, port=5353):
        """Hand the host a datagram as if it had arrived over AWDL."""
        self._emit(link.MSG_UDP_RECV, struct.pack("<H", port) + src + struct.pack("<H", sport)
                   + bytes([0x01 if multicast else 0]) + data)


class TcpBridgeDevice(FakeDevice):
    """A FakeDevice that bridges TCP_CONNECT to a local (IPv4) address, so the
    host app's send path can run against a real OpenDrop receiver on loopback
    without IPv6 or hardware. Credits mimic the firmware's sockproxy."""

    def __init__(self, target, mdns_port=8770, **kw):
        super().__init__(**kw)
        self.target = target          # (host, port) of the loopback receiver
        self.mdns_port = mdns_port    # SRV port the browse learns (= receiver port)
        self._conns = {}              # conn id -> socket
        self._credit_each = 65536

    def _handle(self, t, p):
        import socket
        import struct
        import threading
        if t == link.MSG_TCP_CONNECT and len(p) >= 20:
            cid = struct.unpack_from("<H", p)[0]
            try:
                sk = socket.create_connection(self.target, timeout=5)
            except OSError:
                self._emit(link.MSG_TCP_CLOSE, struct.pack("<HBb", cid, 1, -1))
                return
            self._conns[cid] = sk
            # TCP_OPENED: peer (as asked), ports, not accepted, a send credit.
            self._emit(link.MSG_TCP_OPENED, struct.pack("<H", cid) + p[2:18]
                       + struct.pack("<HHBI", struct.unpack_from("<H", p, 18)[0],
                                     40000, 0, self._credit_each))
            threading.Thread(target=self._pump, args=(cid, sk), daemon=True).start()
        elif t == link.MSG_TCP_DATA and len(p) >= 2:
            cid = struct.unpack_from("<H", p)[0]
            sk = self._conns.get(cid)
            if sk:
                try:
                    sk.sendall(p[2:])
                    self._emit(link.MSG_TCP_CREDIT, struct.pack("<HI", cid, len(p) - 2))
                except OSError:
                    pass
        elif t == link.MSG_TCP_CREDIT:
            pass                      # downlink window: the loopback socket buffers
        elif t == link.MSG_TCP_CLOSE and len(p) >= 3:
            cid = struct.unpack_from("<H", p)[0]
            sk = self._conns.get(cid)
            if sk:
                try:
                    sk.shutdown(1) if p[2] == 0 else sk.close()
                except OSError:
                    pass
        else:
            super()._handle(t, p)

    def _pump(self, cid, sk):
        import struct
        try:
            while True:
                data = sk.recv(2048)
                if not data:
                    self._emit(link.MSG_TCP_CLOSE, struct.pack("<HBb", cid, 0, 0))
                    return
                self._emit(link.MSG_TCP_DATA, struct.pack("<H", cid) + data)
        except OSError:
            self._emit(link.MSG_TCP_CLOSE, struct.pack("<HBb", cid, 1, -1))
        finally:
            self._conns.pop(cid, None)
            try:
                sk.close()
            except OSError:
                pass
