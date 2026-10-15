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
# Interactive stdin commands (forwarded to the device): mac | stats | ping | testframe
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
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import espdrop_link as link

# From <linux/if_tun.h>. TUNSETIFF's encoding is the same across common Linux
# architectures (dir=write, type='T', nr=202, size=4 bytes).
TUNSETIFF = 0x400454CA
IFF_TAP   = 0x0002
IFF_NO_PI = 0x1000

MAC_RE = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")


def open_tap(name: str) -> int:
    fd = os.open("/dev/net/tun", os.O_RDWR)
    ifr = struct.pack("16sH", name.encode(), IFF_TAP | IFF_NO_PI)
    fcntl.ioctl(fd, TUNSETIFF, ifr)
    return fd


def ip(*argv) -> None:
    subprocess.run(["ip", *argv], check=True)


def mac_str(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


def query_mac(usb_fd: int, reader: link.Reader, timeout: float = 2.0):
    """Ask the device for its Wi-Fi MAC; print any LOG lines seen meanwhile."""
    try:
        link.write_all(usb_fd, link.frame(link.MSG_CMD, b"mac"))
    except (OSError, TimeoutError) as e:
        print(f"warning: could not query device MAC ({e})", file=sys.stderr)
        return None
    for t, p in link.read_frames(usb_fd, reader, timeout):
        text = p.decode("utf-8", "replace").strip()
        if t == link.MSG_LOG:
            print(f"[device] {text}")
        elif t == link.MSG_CMD_REPLY and text.startswith("mac "):
            return text[4:].strip()
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="EspDrop M2.1 TAP bridge for awdl0")
    ap.add_argument("port", help="device serial port, e.g. /dev/ttyACM0")
    ap.add_argument("--dev", default="awdl0", help="TAP interface name (default awdl0)")
    ap.add_argument("--mtu", type=int, default=1484, help="interface MTU (default 1484)")
    ap.add_argument("--mac", default=None,
                    help="interface MAC (default: query the C3)")
    ap.add_argument("--no-config", action="store_true",
                    help="create the TAP but do not run `ip link` (for debugging)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("this tool needs root (TAP creation + `ip link`); re-run with sudo",
              file=sys.stderr)
        return 1

    from_tap = 0  # host -> device frames read from awdl0
    to_tap = 0    # device -> host frames written to awdl0
    usb_fd = link.open_serial_raw(args.port)
    reader = link.Reader()
    tap_fd = None

    try:
        # Learn the device MAC (and drain boot chatter) before bringing the link up.
        mac = args.mac or query_mac(usb_fd, reader)
        if mac and not MAC_RE.match(mac):
            print(f"warning: ignoring malformed MAC {mac!r}", file=sys.stderr)
            mac = None

        tap_fd = open_tap(args.dev)
        os.set_blocking(tap_fd, False)

        if not args.no_config:
            if mac:
                try:
                    ip("link", "set", "dev", args.dev, "address", mac)
                except subprocess.CalledProcessError as e:
                    print(f"warning: could not set MAC {mac}: {e}", file=sys.stderr)
            ip("link", "set", "dev", args.dev, "mtu", str(args.mtu), "up")

        print(f"{args.dev} up (mtu {args.mtu}, mac {mac or 'kernel-assigned'}); "
              f"bridging {args.port}")
        print("commands: mac | stats | ping | testframe   (Ctrl-C to quit)")

        stdin_fd = sys.stdin.fileno()
        stdin_buf = bytearray()
        stdin_open = True
        read_cap = args.mtu + 64  # one Ethernet frame plus slack

        # Single-threaded select pump. Writes use a bounded 1s timeout so one
        # direction stalling on USB backpressure cannot block the other for long
        # (M2.1 traffic is light; a queued non-blocking design is for M2.3+).
        while True:
            watch = [usb_fd, tap_fd]
            if stdin_open:
                watch.append(stdin_fd)
            r, _, _ = select.select(watch, [], [])

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
                            try:
                                link.write_all(tap_fd, p, timeout=1.0)
                                to_tap += 1
                            except (OSError, TimeoutError) as e:
                                print(f"tap write failed: {e}", file=sys.stderr)
                        elif t == link.MSG_LOG:
                            print(f"[device] {p.decode('utf-8', 'replace')}")
                        elif t == link.MSG_CMD_REPLY:
                            print(f"[reply] {p.decode('utf-8', 'replace')}")
                        elif t == link.MSG_PEER_ADD and len(p) == 6:
                            print(f"[peer+] {mac_str(p)}")
                        elif t == link.MSG_PEER_REMOVE and len(p) == 6:
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
        if tap_fd is not None:
            os.close(tap_fd)  # removes the non-persistent interface
        os.close(usb_fd)
        print(f"stopped. host->device frames: {from_tap}, device->host frames: {to_tap}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
