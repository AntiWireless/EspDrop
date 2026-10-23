# EspDrop tests - mDNS responses shaped like the ones an iPad sends for AirDrop.
#
# Modelled on a real capture (docs/SPIKES.md, M3 debugging): one big response
# with 14 answers (TXT flags, PTRs for _airdrop and two pairing services, AAAA,
# reverse PTR, three SRVs) plus NSEC and an EDNS0 OPT record, and the small
# ~209-byte answer to a PTR query. Names and addresses here are made up.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import ipaddress
import struct

PAD_IP = ipaddress.IPv6Address("fe80::1234:56ff:fe78:9abc")
PAD_ID = "0123456789ab"
PAD_NAME = "TestPad"
HOST = "00000000-1111-2222-3333-444444444444.local."


class Msg:
    """Tiny DNS encoder with name compression, enough for these fixtures."""

    def __init__(self):
        self.buf = bytearray(12)
        self.names = {}

    def name(self, n):
        labels = n.rstrip(".").split(".")
        for i in range(len(labels)):
            suffix = ".".join(labels[i:]).lower()
            if suffix in self.names:
                self.buf += struct.pack("!H", 0xC000 | self.names[suffix])
                return
            self.names[suffix] = len(self.buf)
            b = labels[i].encode()
            self.buf += bytes([len(b)]) + b
        self.buf += b"\0"

    def rr(self, owner, typ, flush, ttl, rdata):
        self.name(owner)
        self.buf += struct.pack("!HHI", typ, 1 | (0x8000 if flush else 0), ttl)
        at = len(self.buf)
        self.buf += b"\0\0"
        rdata(self)
        struct.pack_into("!H", self.buf, at, len(self.buf) - at - 2)


def _txt(*items):
    def f(m):
        for it in items:
            b = it.encode()
            m.buf.extend(bytes([len(b)]) + b)
    return f


def _name(n):
    return lambda m: m.name(n)


def _srv(port):
    def f(m):
        m.buf.extend(struct.pack("!HHH", 0, 0, port))
        m.name(HOST)
    return f


def _aaaa(m):
    m.buf.extend(PAD_IP.packed)


def _nsec(owner, types):
    def f(m):
        m.name(owner)
        bitmap = bytearray(32)
        for t in types:
            bitmap[t // 8] |= 0x80 >> (t % 8)
        n = max(i for i, b in enumerate(bitmap) if b) + 1
        m.buf.extend(bytes([0, n]) + bitmap[:n])
    return f


def response(kind="small", pad_id=PAD_ID, flags="111611", ttl_ptr=4500, port=8770):
    """kind: 'small' (PTR answer + TXT/SRV/AAAA/NSEC additionals), 'full' (the big
    announcement), 'goodbye' (PTR with TTL 0), 'other' (no AirDrop at all)."""
    ad = f"{pad_id}._airdrop._tcp.local."
    asp = f"{PAD_NAME}._applicationServicePairing._tcp.local."
    m = Msg()
    if kind == "small":
        ans = [("_airdrop._tcp.local.", 12, False, ttl_ptr, _name(ad))]
        add = [(ad, 16, True, 4500, _txt(f"flags={flags}")),
               (ad, 33, True, 120, _srv(port)),
               (HOST, 28, True, 120, _aaaa),
               (ad, 47, True, 4500, _nsec(ad, [16, 33])),
               (HOST, 47, True, 120, _nsec(HOST, [28]))]
    elif kind == "full":
        ans = [(ad, 16, True, 4500, _txt(f"flags={flags}")),
               ("_services._dns-sd._udp.local.", 12, False, 4500, _name("_airdrop._tcp.local.")),
               ("_airdrop._tcp.local.", 12, False, ttl_ptr, _name(ad)),
               (asp, 16, True, 4500, _txt("sn=com.apple.sharingd.AirDrop", "at=000000000000")),
               ("_services._dns-sd._udp.local.", 12, False, 4500,
                _name("_applicationServicePairing._tcp.local.")),
               ("_applicationServicePairing._tcp.local.", 12, False, 4500, _name(asp)),
               (HOST, 28, True, 120, _aaaa),
               (ad, 33, True, 120, _srv(port)),
               (asp, 33, True, 120, _srv(49842))]
        add = [(ad, 47, True, 4500, _nsec(ad, [16, 33])),
               (asp, 47, True, 4500, _nsec(asp, [16, 33])),
               (HOST, 47, True, 120, _nsec(HOST, [28]))]
    elif kind == "goodbye":
        ans = [("_airdrop._tcp.local.", 12, False, 0, _name(ad))]
        add = []
    elif kind == "other":
        ans = [("_companion-link._tcp.local.", 12, False, 4500,
                _name(f"{PAD_NAME}._companion-link._tcp.local."))]
        add = []
    else:
        raise ValueError(kind)
    for r in ans + add:
        m.rr(*r)
    n_add = len(add)
    if kind == "full":   # EDNS0 OPT with an Apple "Owner" option, as sleep-proxy hosts add
        m.buf += b"\0" + struct.pack("!HHIH", 41, 1440, 0x1194, 14) + struct.pack("!HH", 4, 10) \
            + bytes(10)
        n_add += 1
    struct.pack_into("!6H", m.buf, 0, 0, 0x8400, 0, len(ans), 0, n_add)
    return bytes(m.buf)
