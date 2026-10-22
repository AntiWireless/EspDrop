#!/usr/bin/env python3
# EspDrop - watch OpenDrop's own zeroconf browse for AirDrop receivers.
#
# tools/mdns_probe.py shows whether mDNS on awdl0 reaches a program at all.
# This goes one step further: it runs the zeroconf library from the OpenDrop
# venv the way `opendrop find` sets it up (the interface's first IPv6 address,
# IPv6 only, a browser for _airdrop._tcp plus the patched 1.5 s repeat query)
# and reports what zeroconf itself received on each of its sockets, whether it
# parsed it, and whether the browser reported a receiver. A plain "witness"
# socket listens alongside WITHOUT joining the mDNS group itself (Linux hands a
# wildcard-bound socket every group packet the interface accepted), so it sees
# exactly what zeroconf's own membership lets in and changes nothing. It also
# lists the group memberships on the interface and every socket on UDP 5353,
# since a second mDNS stack of the same user (a leftover `opendrop`, a browser)
# can take unicast answers meant for this one.
#
# Run it with the venv's python while the bridge is up and the target is ready
# (AirDrop Everyone, unlocked, share sheet OPEN, tools/airdrop_wake.py running):
#
#   ~/opendrop-venv/bin/python tools/zeroconf_probe.py
#   ~/opendrop-venv/bin/python tools/zeroconf_probe.py -t 60 -i awdl0
#
# The output names devices and addresses; redact them before sharing it.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import argparse
import logging
import os
import select
import socket
import struct
import sys
import threading
import time

try:
    import ifaddr
    import zeroconf
    from zeroconf import DNSOutgoing, DNSQuestion, IPVersion, ServiceBrowser, Zeroconf, const
except ImportError as e:
    sys.exit(f"error: {e.name} not found; run this with the OpenDrop venv's python:\n"
             f"  ~/opendrop-venv/bin/python {sys.argv[0]}")

MDNS_ADDR = "ff02::fb"
MDNS_PORT = 5353
SERVICE = "_airdrop._tcp.local."
IPV6_RECVPKTINFO = getattr(socket, "IPV6_RECVPKTINFO", 49)
IPV6_PKTINFO = getattr(socket, "IPV6_PKTINFO", 50)
IF_FLAGS = {0x01: "temporary", 0x08: "dadfailed", 0x20: "deprecated", 0x40: "tentative"}

lock = threading.Lock()
t0 = time.monotonic()


def say(msg: str) -> None:
    with lock:
        print(f"{time.monotonic() - t0:6.1f}s  {msg}", flush=True)


def udp_5353_sockets() -> list:
    """Every UDP socket on port 5353: (family, local addr, uid, inode, rx_queue, drops)."""
    out = []
    for fam, path in (("v6", "/proc/net/udp6"), ("v4", "/proc/net/udp")):
        try:
            lines = open(path).read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            hexaddr, hexport = f[1].split(":")
            if int(hexport, 16) != MDNS_PORT:
                continue
            raw = bytes.fromhex(hexaddr)
            if sys.byteorder == "little":  # the kernel prints 32-bit words in host order
                raw = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
            addr = socket.inet_ntop(socket.AF_INET6 if fam == "v6" else socket.AF_INET, raw)
            out.append((fam, addr, int(f[7]), f[9], int(f[4].split(":")[1], 16), int(f[-1])))
    return out


def socket_owners() -> dict:
    """inode -> "pid name" for the processes we may look into."""
    owners = {}
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            name = open(f"/proc/{pid}/comm").read().strip()
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace").strip()
            for fd in os.listdir(f"/proc/{pid}/fd"):
                link = os.readlink(f"/proc/{pid}/fd/{fd}")
                if link.startswith("socket:["):
                    owners[link[8:-1]] = f"pid {pid} {name}: {cmd[:70]}"
        except OSError:
            continue
    return owners


def print_sockets(title: str, mine: set) -> None:
    socks = udp_5353_sockets()
    owners = socket_owners()
    me = os.getuid()
    print(f"{title}:")
    if not socks:
        print("  (none)")
    for fam, addr, uid, inode, rxq, drops in socks:
        who = "this probe" if inode in mine else owners.get(inode, "process not visible (other user?)")
        same = " SAME USER" if uid == me and inode not in mine else ""
        print(f"  udp{fam} [{addr}]:5353 uid {uid}{same} rxq {rxq} drops {drops}  {who}")


class Stats:
    def __init__(self):
        self.by_src = {}        # (source, socket) -> [packets, AirDrop responses, invalid]
        self.dups = 0
        self.witness = {}       # source -> [AirDrop responses multicast, unicast]
        self.found = []


class ZeroconfLog(logging.Handler):
    """Turns zeroconf's debug log into per-packet facts."""

    def __init__(self, st: Stats):
        super().__init__(logging.DEBUG)
        self.st = st

    def emit(self, rec: logging.LogRecord) -> None:
        try:
            self._emit(rec)
        except Exception as e:  # never let a diagnostic kill zeroconf's loop
            say(f"(log handler: {e!r})")

    def _emit(self, rec):
        tmpl, a = str(rec.msg), rec.args or ()
        if tmpl.startswith("Received from") and len(a) == 6:     # valid packet
            addr, _port, sock, msg, n, data = a
            self._count(addr, sock, not msg.is_query(), data, valid=True, msg=msg, n=n)
        elif tmpl.startswith("Received from") and len(a) == 5:   # failed to parse
            addr, _port, sock, n, data = a
            self._count(addr, sock, bool(data[2] & 0x80) if len(data) > 2 else False, data, valid=False, n=n)
        elif tmpl.startswith("Ignoring duplicate"):
            self.st.dups += 1
        elif tmpl.startswith(("Received invalid packet", "Unable to parse", "Record for")):
            text = rec.getMessage().split(" while unpacking")[0][:160]
            err = f" ({rec.exc_info[1]})" if rec.exc_info and rec.exc_info[1] else ""
            say(f"zeroconf parse problem: {text}{err}")
        elif tmpl.startswith(("Creating new socket", "Adding %r", "Configuring socket")):
            say("zeroconf setup: " + rec.getMessage()[:150])
        elif rec.levelno >= logging.WARNING:
            say(f"zeroconf {rec.levelname}: {rec.getMessage()[:200]}")

    def _count(self, addr, sock, is_resp, data, valid, n, msg=None):
        # sock is "<fd> (<getsockname()>)": the wildcard one is the listen socket.
        kind = "listen" if "('::'," in str(sock) else "respond"
        key = (addr, f"{kind} socket fd {str(sock).split()[0]}")
        st = self.st.by_src.setdefault(key, [0, 0, 0])
        st[0] += 1
        st[2] += not valid
        if not (is_resp and b"_airdrop" in data):
            return
        st[1] += 1
        if not valid:
            say(f"zeroconf got an AirDrop response from {addr} on its {kind} socket "
                f"({n} bytes) but could NOT parse it")
            return
        recs = [r for r in msg.answers() if "_airdrop" in r.name.lower()]
        desc = ", ".join(sorted({f"{const._TYPES.get(r.type, r.type)}"
                                 + (f"->{r.alias}" if hasattr(r, "alias") else "")
                                 + (f":{r.port}" if hasattr(r, "port") else "")
                                 for r in recs}))
        say(f"zeroconf got AirDrop response from {addr} on its {kind} socket ({n} bytes): {desc or 'no _airdrop records'}")


def group_members(iface: str) -> str:
    """How many sockets joined ff02::fb on iface (/proc/net/igmp6), as text."""
    try:
        for line in open("/proc/net/igmp6"):
            f = line.split()
            if len(f) >= 4 and f[1] == iface and f[2] == "ff0200000000000000000000000000fb":
                return f"{iface} is a member of {MDNS_ADDR} ({int(f[3])} joins)"
    except OSError:
        return "/proc/net/igmp6 not readable"
    return f"{iface} is NOT a member of {MDNS_ADDR} - group answers are dropped before any socket"


def witness(st: Stats, stop: threading.Event, ready: list) -> None:
    """A plain socket next to zeroconf's: what the kernel hands to programs."""
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.bind(("::", MDNS_PORT))
        # No IPV6_JOIN_GROUP on purpose (see the top of the file).
        s.setsockopt(socket.IPPROTO_IPV6, IPV6_RECVPKTINFO, 1)
    except OSError as e:
        say(f"witness socket failed: {e}")
        return
    ready.append(str(os.fstat(s.fileno()).st_ino))
    while not stop.is_set():
        r, _, _ = select.select([s], [], [], 0.3)
        if not r:
            continue
        data, anc, _flags, src = s.recvmsg(9000, socket.CMSG_SPACE(20))
        if len(data) < 12 or not (data[2] & 0x80) or b"_airdrop" not in data:
            continue
        dst = "?"
        for lvl, typ, val in anc:
            if lvl == socket.IPPROTO_IPV6 and typ == IPV6_PKTINFO and len(val) >= 16:
                dst = socket.inet_ntop(socket.AF_INET6, val[:16])
        w = st.witness.setdefault(src[0], [0, 0])
        w[0 if dst.startswith("ff") else 1] += 1
        say(f"witness got AirDrop response from {src[0]} to {dst} ({len(data)} bytes)")
    s.close()


class Listener:
    def __init__(self, st: Stats):
        self.st = st

    def _report(self, what, zc, type_, name):
        t = time.monotonic()
        info = zc.get_service_info(type_, name)      # as OpenDrop does, in the browser thread
        took = time.monotonic() - t
        if info is None:
            say(f"browser: {what} {name} - get_service_info gave nothing after {took:.1f}s")
            return
        self.st.found.append(name)
        say(f"browser: {what} {name} -> {info.server} port {info.port} "
            f"addresses {info.parsed_addresses()} (resolved in {took:.1f}s)")

    def add_service(self, zc, type_, name):
        self._report("ADD", zc, type_, name)

    def update_service(self, zc, type_, name):
        self._report("update", zc, type_, name)

    def remove_service(self, zc, type_, name):
        say(f"browser: remove {name}")


def main() -> int:
    ap = argparse.ArgumentParser(description="watch OpenDrop's zeroconf browse on an interface")
    ap.add_argument("-i", "--iface", default="awdl0")
    ap.add_argument("-t", "--time", type=float, default=45.0, help="seconds to browse")
    args = ap.parse_args()

    adapter = next((a for a in ifaddr.get_adapters() if a.name == args.iface), None)
    if adapter is None:
        print(f"error: no interface {args.iface}", file=sys.stderr)
        return 1
    v6 = [ip.ip for ip in adapter.ips if ip.is_IPv6]
    print(f"zeroconf {zeroconf.__version__}, python {sys.version.split()[0]}, {args.iface} index {adapter.index}")
    flags = {}
    try:
        for line in open("/proc/net/if_inet6"):
            f = line.split()
            if f[5] == args.iface:
                flags[socket.inet_ntop(socket.AF_INET6, bytes.fromhex(f[0]))] = int(f[4], 16)
    except OSError:
        pass
    for i, (addr, _flow, scope) in enumerate(v6):
        fl = [n for b, n in IF_FLAGS.items() if flags.get(addr, 0) & b]
        print(f"  IPv6 {addr} scope {scope}{' [' + ','.join(fl) + ']' if fl else ''}"
              f"{'   <- what OpenDrop uses (the first one)' if i == 0 else ''}")
    if not v6:
        print(f"error: {args.iface} has no IPv6 address", file=sys.stderr)
        return 1
    if len(v6) > 1:
        print("  note: more than one IPv6 address; OpenDrop binds its sender to the first only")
    print_sockets("other UDP 5353 sockets before we start", set())
    print("before:", group_members(args.iface))

    st = Stats()
    zlog = logging.getLogger("zeroconf")
    zlog.addHandler(ZeroconfLog(st))
    zlog.setLevel(logging.DEBUG)
    zlog.propagate = False

    stop = threading.Event()
    mine = []
    threading.Thread(target=witness, args=(st, stop, mine), daemon=True).start()

    print(f"\nbrowsing {SERVICE} for {args.time:.0f}s like `opendrop find` ...")
    zc = Zeroconf(interfaces=[v6[0][0]], ip_version=IPVersion.V6Only)
    browser = ServiceBrowser(zc, SERVICE, Listener(st))
    say("with zeroconf: " + group_members(args.iface))

    def repeat():  # the patched OpenDrop's steady query (opendrop-mdns-repeat)
        while not stop.wait(1.5):
            try:
                out = DNSOutgoing(const._FLAGS_QR_QUERY)
                out.add_question(DNSQuestion(SERVICE, const._TYPE_PTR, const._CLASS_IN))
                zc.send(out)
            except Exception as e:
                say(f"repeat query failed: {e}")
    threading.Thread(target=repeat, daemon=True).start()

    end = time.monotonic() + args.time
    next_status = time.monotonic() + 10
    try:
        while time.monotonic() < end:
            time.sleep(0.2)
            if time.monotonic() >= next_status:
                next_status += 10
                zr = sum(v[1] for v in st.by_src.values())
                wr = sum(sum(v) for v in st.witness.values())
                say(f"status: zeroconf AirDrop responses {zr}, witness {wr}, receivers found {len(set(st.found))}")
    except KeyboardInterrupt:
        pass
    stop.set()

    print("\nzeroconf received (source / socket: packets, AirDrop responses, invalid):")
    if not st.by_src:
        print("  nothing at all - not even our own looped-back queries")
    for (src, sock), (n, na, bad) in sorted(st.by_src.items()):
        print(f"  {src} / {sock}: {n} packets, {na} AirDrop responses, {bad} invalid")
    if st.dups:
        print(f"  ({st.dups} duplicate packets ignored by zeroconf, normal)")
    print("witness socket (AirDrop responses: multicast / unicast):")
    if not st.witness:
        print("  none")
    for src, (mc, uc) in st.witness.items():
        print(f"  {src}: {mc} / {uc}")
    for t in zc.engine.readers:  # mark zeroconf's own sockets in the list
        try:
            mine.append(str(os.fstat(t.fileno).st_ino))
        except (OSError, TypeError):
            pass
    print_sockets("UDP 5353 sockets at the end", set(mine))

    zr = sum(v[1] for v in st.by_src.values())
    zbad = sum(v[2] for v in st.by_src.values())
    wr = sum(sum(v) for v in st.witness.values())
    print("\nverdict:", end=" ")
    if st.found:
        print("zeroconf FOUND the receiver. Browsing works here; compare with `opendrop find`.")
    elif zr and zbad:
        print("zeroconf received AirDrop answers but could not parse some (see 'parse problem').")
    elif zr:
        print("zeroconf received and parsed AirDrop answers, yet the browser reported nothing.")
    elif wr:
        print("the witness got AirDrop answers that zeroconf never saw: its sockets miss\n"
              "  what the kernel delivers (see the socket list: another mDNS stack of the same user?).")
    else:
        print("no AirDrop answer reached any socket. If tcpdump on the interface showed\n"
              "  answers from the device during this run, the group membership is missing\n"
              "  (see the member line above); if not, the device did not answer.")

    # zeroconf.close() can hang on this link (see opendrop find); do not wait for it.
    threading.Thread(target=lambda: (browser.cancel(), zc.close()), daemon=True).start()
    time.sleep(1)
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
