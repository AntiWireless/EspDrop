# EspDrop - mDNS for AirDrop over the device's UDP proxy (docs/M4_ENTWURF.md 5.3).
#
# The Browser finds AirDrop receivers (_airdrop._tcp.local.) on the AWDL link:
# it asks every 1.5 s (as airdrop-mt7921's opendrop-mdns-repeat patch does: one
# lost multicast frame must not cost seconds of backoff), and assembles
# PTR -> instance, SRV -> host and port, TXT -> flags, AAAA -> address from the
# answer and additional sections of every response. A goodbye (TTL 0 PTR)
# removes the receiver again.
#
# Messages are read and written with python-zeroconf's DNSIncoming/DNSOutgoing
# (installed with OpenDrop); its socket layer is not used, the datagrams go
# through the device.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import ipaddress
import threading
import time

try:
    from zeroconf import DNSIncoming, DNSOutgoing, DNSQuestion, const
except ImportError as e:   # pragma: no cover - depends on the environment
    raise ImportError("python-zeroconf is missing; run espdrop with the OpenDrop venv's "
                      "python (~/opendrop-venv/bin/python)") from e

AIRDROP = "_airdrop._tcp.local."
MDNS_PORT = 5353
MDNS_GROUP = ipaddress.IPv6Address("ff02::fb").packed

T_PTR, T_TXT, T_AAAA, T_SRV = const._TYPE_PTR, const._TYPE_TXT, const._TYPE_AAAA, const._TYPE_SRV


def txt_properties(raw: bytes) -> dict:
    """b'\\x0cflags=111611...' -> {b'flags': b'111611', ...}"""
    props, i = {}, 0
    while i < len(raw):
        n = raw[i]
        item = raw[i + 1:i + 1 + n]
        i += 1 + n
        if item:
            k, _, v = item.partition(b"=")
            props[k] = v
    return props


class Receiver:
    """One AirDrop receiver; quacks like zeroconf's ServiceInfo where OpenDrop
    looks (name, server, port, properties, parsed_addresses())."""

    def __init__(self, instance: str):
        self.name = instance                       # 0123456789ab._airdrop._tcp.local.
        self.id = instance.split(".", 1)[0]        # 0123456789ab
        self.server = None
        self.port = None
        self.properties = {}
        self.addresses = []
        self.source = None                         # who sent the records
        self.first_seen = self.last_seen = time.monotonic()

    @property
    def flags(self):
        try:
            return int(self.properties.get(b"flags", b""))
        except ValueError:
            return None

    def parsed_addresses(self):
        return list(self.addresses) or ([self.source] if self.source else [])

    def complete(self):
        return self.port is not None and bool(self.parsed_addresses())

    def __repr__(self):
        return (f"Receiver({self.id}, [{(self.parsed_addresses() or ['?'])[0]}]:{self.port}, "
                f"flags={self.flags})")


class Browser:
    def __init__(self, device, on_found=None, on_gone=None, on_packet=None, interval=1.5):
        self.device = device
        self.on_found = on_found or (lambda r: None)
        self.on_gone = on_gone or (lambda r: None)
        self.on_packet = on_packet        # (src, multicast, summary) for -v
        self.interval = interval
        self._lock = threading.Lock()
        self._recv = {}                   # lower-case instance name -> Receiver
        self._reported = set()
        self._hosts = {}                  # lower-case server name -> [addresses]
        self._stop = threading.Event()
        self._thread = None
        self.queries_sent = 0
        self.packets = 0
        self.invalid = 0

    # ---- lifecycle ---------------------------------------------------------

    def start(self):
        self.device.udp_open(MDNS_PORT, self._on_udp, join_mdns=True)
        self._thread = threading.Thread(target=self._query_loop, name="espdrop-mdns",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        try:
            self.device.udp_close(MDNS_PORT, self._on_udp)
        except Exception:
            pass

    def receivers(self):
        with self._lock:
            return [r for r in self._recv.values() if r.complete()]

    # ---- queries -----------------------------------------------------------

    @staticmethod
    def query() -> bytes:
        out = DNSOutgoing(const._FLAGS_QR_QUERY)
        out.add_question(DNSQuestion(AIRDROP, T_PTR, const._CLASS_IN))
        return out.packets()[0]

    def _query_loop(self):
        q = self.query()
        while not self._stop.is_set():
            try:
                self.device.udp_send(MDNS_PORT, MDNS_GROUP, MDNS_PORT, q)
                self.queries_sent += 1
            except Exception as e:
                if self.on_packet:
                    self.on_packet(None, False, f"query not sent: {e}")
            self._stop.wait(self.interval)

    # ---- responses ---------------------------------------------------------

    def _on_udp(self, src16, sport, multicast, data):
        self.handle(str(ipaddress.IPv6Address(src16)), data, multicast)

    def handle(self, src: str, data: bytes, multicast=True):
        """Fold one received mDNS message in (public for tests)."""
        self.packets += 1
        msg = DNSIncoming(data, (src, MDNS_PORT))
        if not msg.valid:
            self.invalid += 1
            if self.on_packet:
                self.on_packet(src, multicast, f"invalid mDNS message ({len(data)} bytes)")
            return
        if msg.is_query():
            return
        records = msg.answers()
        found, gone = [], []
        with self._lock:
            # Addresses first: an SRV in the same message may need them.
            for r in records:
                if r.type == T_AAAA and len(r.address) == 16:
                    addrs = self._hosts.setdefault(r.name.lower(), [])
                    a = str(ipaddress.IPv6Address(r.address))
                    if r.ttl == 0:
                        if a in addrs:
                            addrs.remove(a)
                    elif a not in addrs:
                        addrs.append(a)
            for r in records:
                name = r.name.lower()
                if r.type == T_PTR and name == AIRDROP:
                    inst = r.alias.lower()
                    if r.ttl == 0:
                        old = self._recv.pop(inst, None)
                        if old and inst in self._reported:
                            self._reported.discard(inst)
                            gone.append(old)
                    else:
                        self._get(inst, r.alias, src)
                elif name.endswith("." + AIRDROP) and r.type in (T_SRV, T_TXT):
                    if r.ttl == 0:
                        continue
                    rec = self._get(name, r.name, src)
                    if r.type == T_SRV:
                        rec.server, rec.port = r.server, r.port
                    else:
                        rec.properties = txt_properties(r.text)
            for inst, rec in self._recv.items():
                if rec.server:
                    rec.addresses = list(self._hosts.get(rec.server.lower(), []))
                if rec.complete() and inst not in self._reported:
                    self._reported.add(inst)
                    found.append(rec)
        if self.on_packet:
            mine = sorted({f"{const._TYPES.get(r.type, r.type)}" for r in records
                           if AIRDROP in r.name.lower() or
                           (r.type == T_PTR and AIRDROP in getattr(r, "alias", "").lower())})
            if mine:
                self.on_packet(src, multicast, "AirDrop " + ",".join(mine))
        for r in found:
            self.on_found(r)
        for r in gone:
            self.on_gone(r)

    def _get(self, key, shown, src):
        rec = self._recv.get(key)
        if rec is None:
            rec = self._recv[key] = Receiver(shown)
        rec.source = src
        rec.last_seen = time.monotonic()
        return rec


class Responder:
    """Announce our AirDrop receiver on the AWDL link (docs/M4_ENTWURF.md 5.3).

    Builds our _airdrop._tcp records with python-zeroconf's ServiceInfo, sends
    an unsolicited announcement every `interval` seconds (as the
    opendrop-mdns-reannounce patch does: the iPhone only ever learns of us by
    catching an announce burst over a link we share for a fraction of each
    sequence), answers PTR queries, and sends a goodbye on stop."""

    def __init__(self, device, instance_id, port, our_addr16, flags=0x3fb,
                 interval=5.0, host=None):
        from zeroconf import ServiceInfo
        self.device = device
        self.addr16 = our_addr16
        self.server = host or f"{instance_id}.local."
        name = f"{instance_id}._airdrop._tcp.local."
        self.info = ServiceInfo(AIRDROP, name, port=port,
                                properties={b"flags": str(flags).encode()},
                                server=self.server, addresses=[bytes(our_addr16)])
        self.interval = interval
        self._stop = __import__("threading").Event()
        self._thread = None

    def _announce_packet(self):
        out = DNSOutgoing(const._FLAGS_QR_RESPONSE | const._FLAGS_AA)
        for rec in ([self.info.dns_pointer(), self.info.dns_service(),
                     self.info.dns_text()] + list(self.info.dns_addresses())):
            out.add_answer_at_time(rec, 0)
        return out.packets()[0]

    def start(self):
        self.device.udp_open(MDNS_PORT, self._on_udp, join_mdns=True)
        self._thread = __import__("threading").Thread(
            target=self._loop, name="espdrop-mdns-rsp", daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.device.udp_send(MDNS_PORT, MDNS_GROUP, MDNS_PORT, self._announce_packet())
            except Exception:
                pass
            self._stop.wait(self.interval)

    def _on_udp(self, src16, sport, multicast, data):
        msg = DNSIncoming(data, (str(ipaddress.IPv6Address(src16)), MDNS_PORT))
        if not msg.valid or not msg.is_query():
            return
        if any(q.name.lower() == AIRDROP and q.type in (T_PTR, const._TYPE_ANY)
               for q in msg.questions):
            pkt = self._announce_packet()
            # Answer a QU question by unicast too, then multicast.
            if not multicast:
                self.device.udp_send(MDNS_PORT, src16, sport, pkt)
            self.device.udp_send(MDNS_PORT, MDNS_GROUP, MDNS_PORT, pkt)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        try:
            out = DNSOutgoing(const._FLAGS_QR_RESPONSE | const._FLAGS_AA)
            self.info.dns_pointer().ttl = 0
            out.add_answer_at_time(self.info.dns_pointer(), 0)
            self.device.udp_send(MDNS_PORT, MDNS_GROUP, MDNS_PORT, out.packets()[0])
        except Exception:
            pass
        try:
            self.device.udp_close(MDNS_PORT, self._on_udp)
        except Exception:
            pass
