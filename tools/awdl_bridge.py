#!/usr/bin/env python3
# EspDrop - M2.1 host bridge: expose the C3's AWDL link as a Linux TAP (awdl0).
#
# Creates a TAP interface and shuttles Ethernet frames between it and the device
# over the USB bridge link (BRIDGE_MSG_FRAME). The Linux IP stack rides on awdl0;
# the C3 does the Ethernet<->AWDL translation and the radio (wired up in M2.2+).
# The TAP's hardware address is set to the C3's own Wi-Fi MAC so the host and the
# device present one consistent interface to the AWDL peer.
#
# M2.1 scope is the plumbing and the interface - there is no radio yet, so frames
# the host sends on awdl0 are counted by the device and dropped. To exercise the
# device->host->TAP direction without a radio, type `testframe`: the C3 emits one
# synthetic Ethernet frame (ethertype 0x88b5) that shows up on awdl0.
#
#   sudo tools/awdl_bridge.py /dev/ttyACM0
#   # in another terminal:
#   ip -d link show awdl0
#   sudo tcpdump -i awdl0 -e      # then type `testframe` at the bridge prompt
#
# Neighbour table: every IPv6 packet received from a peer carries its real
# link-local source address and its MAC, so this tool learns that pair from the
# traffic and installs a permanent awdl0 neighbour entry for it. The host can then
# reach the peer without NDP over the lossy link. The address is NOT derived from
# the MAC (EUI-64): current iOS uses non-MAC-based link-local addresses, so a
# guessed address would belong to nobody. Peer add/remove events from the device
# are only logged. Suppressed with --no-config.
#
# Interactive stdin commands (forwarded to the device):
#   mac | stats | radio | tx | node | chan [n|auto] | seq [mirror|verbatim] | wake [secs|off] | mode [l2|ip] | ip | ping6 <addr> [n] | checks | usbtest [kib] | ping | testframe | resync
#
# Control socket: other tools (tools/espdrop_cmd.py, opendrop_send.sh for the
# Bluetooth wake) send device commands through /run/espdrop/<dev>.sock, one
# command per connection, and get the reply line back. It belongs to the user
# who ran sudo (mode 0600), so that user's scripts can use it and nobody else.
#
# Needs root: creating a TAP and running `ip link` both require CAP_NET_ADMIN.
# The interface is not persistent - it disappears when this process exits.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import argparse
import fcntl
import os
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import espdrop_link as link

# From <linux/if_tun.h>. TUNSETIFF's encoding is the same across common Linux
# architectures (dir=write, type='T', nr=202, size=4 bytes).
TUNSETIFF = 0x400454CA
IFF_TAP   = 0x0002
IFF_NO_PI = 0x1000

MAC_RE = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
MAX_LEARNED = 64     # neighbour entries kept; the oldest is dropped beyond that

CTL_DIR = "/run/espdrop"
# Device commands the control socket passes on (no usbtest: it floods the link).
CTL_COMMANDS = {"wake", "mac", "stats", "radio", "tx", "node", "chan", "seq", "checks", "ping",
                "mode", "ip", "ping6", "power"}
CTL_TIMEOUT = 3.0    # seconds to wait for the device's reply to a socket command


def open_ctl(dev: str):
    """Listening control socket /run/espdrop/<dev>.sock, or (None, None)."""
    path = os.path.join(CTL_DIR, f"{dev}.sock")
    try:
        os.makedirs(CTL_DIR, mode=0o755, exist_ok=True)
        try:
            os.unlink(path)          # left over from a bridge that did not exit cleanly
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(path)
        os.chmod(path, 0o600)
        uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
        if uid and uid.isdigit():
            os.chown(path, int(uid), int(gid) if gid and gid.isdigit() else -1)
        s.listen(4)
        s.setblocking(False)
        return s, path
    except OSError as e:
        print(f"warning: no control socket ({e}); tools cannot reach the device",
              file=sys.stderr)
        return None, None


def open_tap(name: str) -> int:
    fd = os.open("/dev/net/tun", os.O_RDWR)
    ifr = struct.pack("16sH", name.encode(), IFF_TAP | IFF_NO_PI)
    fcntl.ioctl(fd, TUNSETIFF, ifr)
    return fd


def ip(*argv) -> None:
    subprocess.run(["ip", *argv], check=True)


def mac_str(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


def learn_neighbor(eth: bytes, own_mac=None):
    """(link-local source address, source MAC) of a received IPv6 frame, or None.

    Only unicast source MACs and fe80::/10 source addresses qualify; anything else
    (multicast sender, global/unspecified source, non-IPv6) teaches us nothing.
    With own_mac, only frames addressed to us or to a group address count: the
    device forwards every AWDL data frame it hears, also those between others.
    """
    if len(eth) < 14 + 40 or eth[12:14] != b"\x86\xdd":
        return None
    if own_mac and not (eth[0] & 0x01) and eth[0:6] != own_mac:
        return None                  # unicast to someone else
    src_mac = eth[6:12]
    if src_mac[0] & 0x01:            # group address cannot be a neighbour
        return None
    src_ip = eth[22:38]              # IPv6 source: Ethernet hdr (14) + 8
    if src_ip[0] != 0xfe or (src_ip[1] & 0xc0) != 0x80:
        return None                  # not link-local
    return socket.inet_ntop(socket.AF_INET6, src_ip), bytes(src_mac)


def ipv6_sysctl(dev: str, key: str, value: str) -> None:
    """Set net.ipv6.conf.<dev>.<key>; warn instead of failing if unavailable."""
    path = f"/proc/sys/net/ipv6/conf/{dev}/{key}"
    try:
        with open(path, "w") as f:
            f.write(value)
    except OSError as e:
        print(f"warning: could not set {path} = {value} ({e})", file=sys.stderr)


def usable_link_local(dev: str):
    """The interface's link-local address once it is usable (not tentative), else None."""
    try:
        out = subprocess.run(["ip", "-6", "-o", "addr", "show", "dev", dev, "scope", "link"],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    for line in out.splitlines():
        parts = line.split()
        if "inet6" in parts and "tentative" not in parts:
            return parts[parts.index("inet6") + 1].split("/")[0]
    return None


def query_mac(usb_fd: int, reader: link.Reader, timeout: float = 2.0, tries: int = 3):
    """Ask the device for its Wi-Fi MAC; print any LOG lines seen meanwhile.

    Each try starts with a bare 0x00 delimiter: bytes left in the device's COBS
    reader from an earlier session would otherwise corrupt the first command
    (the device ignores the empty frame). Several tries also cover a device that
    is still booting, e.g. right after flashing.
    """
    for attempt in range(1, tries + 1):
        try:
            link.write_all(usb_fd, b"\x00" + link.frame(link.MSG_CMD, b"mac"))
        except (OSError, TimeoutError) as e:
            print(f"warning: could not query device MAC ({e})", file=sys.stderr)
            return None
        for t, p in link.read_frames(usb_fd, reader, timeout):
            text = p.decode("utf-8", "replace").strip()
            if t == link.MSG_LOG:
                print(f"[device] {text}")
            elif t == link.MSG_CMD_REPLY and text.startswith("mac "):
                return text[4:].strip()
        if attempt < tries:
            print(f"no MAC reply from the device yet (try {attempt}/{tries}), retrying ...")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="EspDrop M2.1 TAP bridge for awdl0")
    ap.add_argument("port", help="device serial port, e.g. /dev/ttyACM0")
    ap.add_argument("--dev", default="awdl0", help="TAP interface name (default awdl0)")
    ap.add_argument("--mtu", type=int, default=1460,
                    help="interface MTU (default 1460 = 1500 - 40 B AWDL framing, "
                         "the most the device can transmit in one data frame)")
    ap.add_argument("--mac", default=None,
                    help="interface MAC (default: query the C3)")
    ap.add_argument("--no-config", action="store_true",
                    help="create the TAP but do not run `ip link` (for debugging)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("this tool needs root (TAP creation + `ip link`); re-run with sudo",
              file=sys.stderr)
        return 1

    # kill / a closed terminal: leave like Ctrl-C, so the control socket goes too.
    def _stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGHUP, _stop)

    from_tap = 0  # host -> device frames read from awdl0
    to_tap = 0    # device -> host frames written to awdl0
    usb_fd = link.open_serial_raw(args.port)
    reader = link.Reader()
    tap_fd = None
    ctl = ctl_path = None

    try:
        # Learn the device MAC (and drain boot chatter) before bringing the link up.
        mac = args.mac or query_mac(usb_fd, reader)
        if mac and not MAC_RE.match(mac):
            print(f"warning: ignoring malformed MAC {mac!r}", file=sys.stderr)
            mac = None
        # awdl0 must carry the device's own Wi-Fi MAC: Apple peers tie a peer's
        # IPv6 link-local to the MAC it transmits from, and ignore traffic whose
        # source address belongs to some other MAC (a kernel-assigned TAP MAC
        # made an iPad never answer our AirDrop queries).
        if not mac and not args.no_config:
            print("error: the device did not report its MAC, so awdl0 would get a random\n"
                  "one that Apple devices do not answer. Replug the device and retry, or\n"
                  "pass it with --mac (console `info` shows it).", file=sys.stderr)
            return 1

        tap_fd = open_tap(args.dev)
        os.set_blocking(tap_fd, False)

        have_ipv6 = os.path.isdir("/proc/sys/net/ipv6")
        if not have_ipv6:
            print("warning: IPv6 is disabled in this kernel (ipv6.disable=1 on the "
                  f"kernel command line?); AirDrop needs it, {args.dev} will carry no IP",
                  file=sys.stderr)
        if not args.no_config:
            if mac:
                try:
                    ip("link", "set", "dev", args.dev, "address", mac)
                except subprocess.CalledProcessError as e:
                    print(f"warning: could not set MAC {mac}: {e}", file=sys.stderr)
            if have_ipv6:
                # Before bringing the link up. IPv6 on even where the host turns it
                # off by default (a new interface inherits conf/default), and the
                # link-local derived from the MAC (EUI-64) like on Apple's awdl0.
                # No DAD (only we live on this link, and an address still
                # "tentative" makes OpenDrop's zeroconf announce into nothing), and
                # no router solicitations/RA (no routers on AWDL; every frame we
                # send costs scarce channel-6 airtime).
                ipv6_sysctl(args.dev, "disable_ipv6", "0")
                ipv6_sysctl(args.dev, "accept_dad", "0")
                ipv6_sysctl(args.dev, "accept_ra", "0")
                ipv6_sysctl(args.dev, "router_solicitations", "0")
                ipv6_sysctl(args.dev, "addr_gen_mode", "0")
            ip("link", "set", "dev", args.dev, "mtu", str(args.mtu), "up")

        print(f"{args.dev} up (mtu {args.mtu}, mac {mac or 'kernel-assigned'}); "
              f"bridging {args.port}")
        print("commands: mac | stats | radio | tx | node | chan [n|auto] | seq [mirror|verbatim] | wake [secs|off] | mode [l2|ip] | ip | ping6 <addr> [n] | checks | usbtest [kib] | ping | testframe | resync"
              "   (Ctrl-C to quit)")
        ctl, ctl_path = open_ctl(args.dev)
        # The one socket client whose command is with the device: its conn,
        # partial input, whether the command went out, and when to give up.
        ctl_client = None

        # The device keeps running across host sessions (its USB port does not
        # reset the chip): put it back into L2 in case an M4 host app (or `mode
        # ip`) left it in IP mode, where no frames would reach awdl0. Firmware
        # without HELLO ignores it. Then ask it to re-emit its current peer set
        # so the [peer+] log is complete.
        try:
            link.write_all(usb_fd, link.hello(link.MODE_L2))
            link.write_all(usb_fd, link.frame(link.MSG_CMD, b"resync"))
        except (OSError, TimeoutError) as e:
            print(f"warning: could not request peer resync ({e})", file=sys.stderr)

        stdin_fd = sys.stdin.fileno()
        stdin_buf = bytearray()
        stdin_open = True
        read_cap = args.mtu + 64  # one Ethernet frame plus slack
        learned = {}              # link-local address -> MAC, oldest first
        own_mac = bytes.fromhex(mac.replace(":", "")) if mac else None
        # Our usable link-local, polled every 0.25 s until it shows up (or never,
        # with --no-config / no IPv6); a hint follows if it takes too long.
        probe_ll = have_ipv6 and not args.no_config
        ready_ll = None
        t_up = time.monotonic()
        next_probe = t_up
        hinted = False

        # Single-threaded select pump. Writes use a bounded 1s timeout so one
        # direction stalling on USB backpressure cannot block the other for long
        # (M2.1 traffic is light; a queued non-blocking design is for M2.3+).
        def ctl_finish(text: str) -> None:
            nonlocal ctl_client
            try:
                ctl_client["conn"].sendall(text.encode("utf-8", "replace") + b"\n")
            except OSError:
                pass
            ctl_client["conn"].close()
            ctl_client = None

        while True:
            watch = [usb_fd, tap_fd]
            if stdin_open:
                watch.append(stdin_fd)
            if ctl is not None:
                watch.append(ctl)
            if ctl_client is not None and not ctl_client["sent"]:
                watch.append(ctl_client["conn"])   # until its command line is in
            r, _, _ = select.select(watch, [], [],
                                    0.25 if probe_ll or ctl_client is not None else None)
            if ctl_client is not None and time.monotonic() > ctl_client["deadline"]:
                ctl_finish("error: no reply from the device")
            if probe_ll and time.monotonic() >= next_probe:
                next_probe = time.monotonic() + (1.0 if hinted else 0.25)
                ready_ll = usable_link_local(args.dev)
                if ready_ll:
                    probe_ll = False
                    print(f"[ready] {args.dev} has {ready_ll}%{args.dev} - "
                          f"start OpenDrop now (tools/opendrop_receive.sh)")
                elif not hinted and time.monotonic() - t_up > 5:
                    hinted = True
                    print(f"warning: {args.dev} still has no usable IPv6 link-local "
                          f"address; check `ip -6 addr show dev {args.dev}` and "
                          f"`sysctl net.ipv6.conf.{args.dev}`", file=sys.stderr)

            if usb_fd in r:
                try:
                    data = os.read(usb_fd, 65536)
                except BlockingIOError:
                    data = None  # spurious readiness; try again next iteration
                except OSError as e:
                    print(f"device disconnected (usb read: {e})", file=sys.stderr)
                    break
                if data == b"":
                    print("device disconnected (EOF on usb)", file=sys.stderr)
                    break
                if data:
                    for t, p in reader.feed(data):
                        if t == link.MSG_FRAME:
                            # Learn the sender before handing the frame to the
                            # kernel, so a reply (e.g. a SYN-ACK) already finds
                            # its permanent neighbour entry instead of NDP.
                            nb = learn_neighbor(p, own_mac)
                            if nb and learned.get(nb[0]) != nb[1]:
                                ll, nmac = nb
                                learned.pop(ll, None)   # re-learned: now newest
                                learned[ll] = nmac
                                print(f"[neigh] {ll}%{args.dev} is {mac_str(nmac)}"
                                      f" (learned from traffic)")
                                if not args.no_config:
                                    try:
                                        ip("-6", "neigh", "replace", ll, "lladdr",
                                           mac_str(nmac), "dev", args.dev,
                                           "nud", "permanent")
                                    except subprocess.CalledProcessError as e:
                                        print(f"neigh add failed: {e}", file=sys.stderr)
                                # Bounded: permanent entries never expire on their
                                # own (peers rotate their AWDL address over time).
                                if len(learned) > MAX_LEARNED:
                                    old = next(iter(learned))
                                    del learned[old]
                                    if not args.no_config:
                                        subprocess.run(["ip", "-6", "neigh", "del", old,
                                                        "dev", args.dev],
                                                       stderr=subprocess.DEVNULL)
                            try:
                                link.write_all(tap_fd, p, timeout=1.0)
                                to_tap += 1
                            except (OSError, TimeoutError) as e:
                                print(f"tap write failed: {e}", file=sys.stderr)
                        elif t == link.MSG_LOG:
                            print(f"[device] {p.decode('utf-8', 'replace')}")
                        elif t == link.MSG_HELLO_REPLY:
                            h = link.parse_hello_reply(p)
                            if h:
                                print(f"[hello] protocol {h['version']}, mode "
                                      f"{'ip' if h['mode'] == link.MODE_IP else 'l2'}, caps "
                                      f"{h['caps']:#04x}")
                        elif t == link.MSG_CMD_REPLY:
                            reply = p.decode('utf-8', 'replace')
                            print(f"[reply] {reply}")
                            # Unnumbered: the next reply after a socket command
                            # is its answer (a command typed meanwhile can race).
                            if ctl_client is not None and ctl_client["sent"]:
                                ctl_finish(reply)
                        elif t == link.MSG_PEER_ADD and len(p) == 6:
                            print(f"[peer+] {mac_str(p)}")
                        elif t == link.MSG_PEER_REMOVE and len(p) == 6:
                            # Logged only: peers flap on the sparse 2.4 GHz link, and
                            # deleting the learned neighbour would just force NDP.
                            # Stale entries are harmless and vanish with awdl0.
                            print(f"[peer-] {mac_str(p)}")

            if tap_fd in r:
                try:
                    pkt = os.read(tap_fd, read_cap)
                except (BlockingIOError, OSError):
                    pkt = b""
                if pkt:
                    try:
                        link.write_all(usb_fd, link.frame(link.MSG_FRAME, pkt), timeout=1.0)
                        from_tap += 1
                    except (OSError, TimeoutError) as e:
                        print(f"usb write failed: {e}", file=sys.stderr)

            if ctl is not None and ctl in r:
                try:
                    conn, _ = ctl.accept()
                except OSError:
                    conn = None
                if conn is not None and ctl_client is not None:
                    conn.sendall(b"error: busy, retry\n")
                    conn.close()
                elif conn is not None:
                    conn.setblocking(False)
                    ctl_client = {"conn": conn, "buf": b"", "sent": False,
                                  "deadline": time.monotonic() + CTL_TIMEOUT}

            if ctl_client is not None and not ctl_client["sent"] and ctl_client["conn"] in r:
                try:
                    chunk = ctl_client["conn"].recv(256)
                except (BlockingIOError, InterruptedError):
                    chunk = None
                except OSError:
                    chunk = b""
                if chunk == b"":
                    ctl_client["conn"].close()     # client gave up before sending
                    ctl_client = None
                elif chunk:
                    ctl_client["buf"] += chunk
                    if b"\n" in ctl_client["buf"] or len(ctl_client["buf"]) > 200:
                        cmd = ctl_client["buf"].split(b"\n", 1)[0].strip()
                        word = cmd.split(b" ", 1)[0].decode("ascii", "replace")
                        if word not in CTL_COMMANDS:
                            ctl_finish(f"error: {word!r} is not a socket command")
                        else:
                            print(f"[ctl] {cmd.decode('ascii', 'replace')}")
                            try:
                                link.write_all(usb_fd, link.frame(link.MSG_CMD, cmd))
                                ctl_client["sent"] = True
                            except (OSError, TimeoutError) as e:
                                ctl_finish(f"error: command send failed: {e}")

            if stdin_open and stdin_fd in r:
                try:
                    chunk = os.read(stdin_fd, 4096)
                except BlockingIOError:
                    chunk = None
                if chunk == b"":
                    stdin_open = False  # EOF (Ctrl-D or piped input exhausted)
                elif chunk:
                    stdin_buf += chunk
                    while b"\n" in stdin_buf:
                        line, _, rest = stdin_buf.partition(b"\n")
                        stdin_buf = bytearray(rest)
                        cmd = line.strip()
                        if cmd:
                            try:
                                link.write_all(usb_fd, link.frame(link.MSG_CMD, bytes(cmd)))
                            except (OSError, TimeoutError) as e:
                                print(f"command send failed: {e}", file=sys.stderr)
    except KeyboardInterrupt:
        print()
    finally:
        if ctl is not None:
            ctl.close()
            try:
                os.unlink(ctl_path)
            except OSError:
                pass
        if tap_fd is not None:
            os.close(tap_fd)  # removes the non-persistent interface
        os.close(usb_fd)
        print(f"stopped. host->device frames: {from_tap}, device->host frames: {to_tap}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
