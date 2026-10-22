#!/usr/bin/env python3
# EspDrop - check that mDNS on awdl0 actually reaches a program.
#
# tcpdump sees packets before the firewall and before any socket, so "it is in
# the capture" does not mean an application receives it. This probe binds the
# mDNS port like OpenDrop's zeroconf does, joins ff02::fb on the interface,
# asks for AirDrop receivers (_airdrop._tcp) every 1.5 s, and prints what
# arrives at the socket. Standard library only; run as your normal user while
# the bridge is up, with the target device in the state for sending:
#
#   tools/mdns_probe.py              # 30 s on awdl0
#   tools/mdns_probe.py -t 60 -i awdl0
#
# Reading the result: packets from other addresses arriving at all means the
# kernel delivers mDNS on awdl0 to programs; a response mentioning _airdrop
# means a receiver answered. If tcpdump shows such answers but this probe gets
# nothing from other hosts, something between (a firewall, a missing group
# membership) drops them.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import argparse
import select
import socket
import struct
import sys
import time

MDNS_ADDR = "ff02::fb"
MDNS_PORT = 5353
SERVICE = "_airdrop._tcp.local."


def ptr_query(name: str) -> bytes:
    """A one-question mDNS query: name PTR IN (multicast response requested)."""
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.rstrip(".").split(".")) + b"\x00"
    return struct.pack("!6H", 0, 0, 1, 0, 0, 0) + q + struct.pack("!2H", 12, 1)


def main() -> int:
    ap = argparse.ArgumentParser(description="mDNS reception check on an interface")
    ap.add_argument("-i", "--iface", default="awdl0")
    ap.add_argument("-t", "--time", type=float, default=30.0, help="seconds to listen")
    args = ap.parse_args()

    try:
        idx = socket.if_nametoindex(args.iface)
    except OSError:
        print(f"error: no interface {args.iface}", file=sys.stderr)
        return 1

    s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("::", MDNS_PORT))
    group = socket.inet_pton(socket.AF_INET6, MDNS_ADDR) + struct.pack("@I", idx)
    try:
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP, group)
    except OSError as e:
        print(f"error: joining {MDNS_ADDR} on {args.iface} failed: {e}", file=sys.stderr)
        return 1
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, struct.pack("@I", idx))
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 255)
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_LOOP, 0)

    query = ptr_query(SERVICE)
    print(f"listening on [{MDNS_ADDR}%{args.iface}]:{MDNS_PORT} for {args.time:.0f}s, "
          f"asking for {SERVICE} every 1.5 s ...")
    end = time.monotonic() + args.time
    next_q = 0.0
    seen = {}            # source -> [packets, responses, airdrop responses]
    while time.monotonic() < end:
        now = time.monotonic()
        if now >= next_q:
            try:
                s.sendto(query, (MDNS_ADDR, MDNS_PORT, 0, idx))
            except OSError as e:
                print(f"send failed: {e}", file=sys.stderr)
            next_q = now + 1.5
        r, _, _ = select.select([s], [], [], max(0.0, min(next_q, end) - now))
        if not r:
            continue
        data, addr = s.recvfrom(9000)
        src = addr[0]
        if len(data) < 12:
            continue
        is_resp = bool(struct.unpack("!H", data[2:4])[0] & 0x8000)
        airdrop = b"_airdrop" in data
        st = seen.setdefault(src, [0, 0, 0])
        st[0] += 1
        st[1] += is_resp
        st[2] += is_resp and airdrop
        if is_resp and airdrop:
            print(f"{time.strftime('%H:%M:%S')}  AirDrop RESPONSE from {src} ({len(data)} bytes)")
        elif st[0] == 1:
            print(f"{time.strftime('%H:%M:%S')}  first packet from {src} "
                  f"({'response' if is_resp else 'query'}, {len(data)} bytes)")

    print("\nsummary (per source: packets / responses / AirDrop responses):")
    if not seen:
        print("  nothing received at all - not even other devices' mDNS reached this socket")
    for src, (n, nr, na) in seen.items():
        print(f"  {src}: {n} / {nr} / {na}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
