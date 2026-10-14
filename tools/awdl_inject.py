#!/usr/bin/env python3
# EspDrop - Spike 3 injector: send unicast AWDL data frames to a target MAC.
#
# The question Spike 3 answers (see docs/SPIKES.md): when a unicast 802.11 frame
# is addressed to the ESP32-C3's own station MAC, does the C3
#   (a) ACK it in hardware (SIFS later), and
#   (b) still deliver it to its promiscuous receive callback?
# A forum report claims some ESP-IDF versions ACK such frames but swallow them
# before the sniffer sees them. This tool lets you measure both on real hardware.
#
# It runs on the LAPTOP's monitor-mode Wi-Fi card (the same one you capture with)
# and injects frames over the air to the C3. The frame is built byte-for-byte as
# the C3 firmware's awdl_parse_data() expects (non-QoS data, AWDL LLC/SNAP, AWDL
# data header, IPv6 ethertype), so a delivered frame increments the C3's `rx_data`
# counter - that is the delivery signal. The ACK is read from a parallel capture.
#
# Usage (as root; monitor iface already up on channel 6):
#   sudo tools/awdl_inject.py wlan0mon --target 8c:d0:b2:bb:ca:57 --count 50
#   sudo tools/awdl_inject.py wlan0mon --target 8c:d0:b2:bb:ca:57 --dry-run   # just hexdump, no scapy/root
#
# Experiment design:
#   Variant A (AWDL addr == C3 iface MAC, the default case):
#     - On the C3: `info` to read the STA MAC; leave it as is; `reset`; `rx on`.
#     - Run this with --target <that MAC>.
#     - C3 `stats`: did `rx_data` rise?  (delivery)
#     - Parallel capture on ch6: is there an ACK with RA == C3 MAC after each
#       injected frame?  (hardware ACK)
#   Variant B (AWDL addr != C3 iface MAC):
#     - On the C3: `mac aa:bb:cc:dd:ee:01` (some other addr), `reset`, `rx on`.
#     - Run this with --target <the ORIGINAL AWDL addr, not the new iface MAC>.
#     - Expect: frames delivered to the callback, but NO hardware ACK.
#
# The authoritative ACK check is a parallel capture, because a card rarely sees
# the ACKs to its own injected frames. In another terminal:
#   sudo tcpdump -i wlan0mon -ne 'wlan[0] == 0xd4'      # ACK control frames
# and watch for RA == the C3 MAC. --listen here is a best-effort convenience.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import argparse
import struct
import sys
import time

AWDL_BSSID = bytes.fromhex("002500ff9473")
AWDL_OUI = bytes.fromhex("0017f2")
AWDL_LLC_PID = 0x0800
AWDL_DATA_HEAD = 0x0403
ETHERTYPE_IPV6 = 0x86DD


def parse_mac(s: str) -> bytes:
    parts = s.replace("-", ":").split(":")
    if len(parts) != 6:
        raise argparse.ArgumentTypeError(f"bad MAC: {s}")
    try:
        return bytes(int(p, 16) for p in parts)
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad MAC: {s}")


def build_awdl_data_frame(target: bytes, src: bytes, seq: int, payload: bytes) -> bytes:
    """A non-QoS AWDL data frame (MAC header .. payload), no radiotap, no FCS.

    Mirrors the firmware's awdl_build_data()/awdl_parse_data() exactly so that a
    frame delivered to the C3 increments its `rx_data` counter.
    """
    f = bytearray()
    # 802.11 non-QoS data header, ToDS=0 FromDS=0
    f += struct.pack("<H", 0x0008)        # frame control: type data, subtype 0
    f += struct.pack("<H", 0)             # duration
    f += target                           # addr1 = receiver (the C3)
    f += src                              # addr2 = transmitter (laptop)
    f += AWDL_BSSID                        # addr3 = AWDL BSSID
    f += struct.pack("<H", (seq & 0x0FFF) << 4)  # sequence control
    # LLC / SNAP
    f += bytes([0xAA, 0xAA, 0x03]) + AWDL_OUI + struct.pack(">H", AWDL_LLC_PID)
    # AWDL data header
    f += struct.pack("<H", AWDL_DATA_HEAD)
    f += struct.pack("<H", seq & 0xFFFF)
    f += struct.pack("<H", 0)             # pad
    f += struct.pack(">H", ETHERTYPE_IPV6)
    f += payload
    return bytes(f)


def hexdump(b: bytes) -> str:
    out = []
    for i in range(0, len(b), 16):
        row = b[i:i + 16]
        out.append(" ".join(f"{x:02x}" for x in row))
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="EspDrop Spike 3 AWDL unicast injector")
    ap.add_argument("iface", help="monitor-mode interface, e.g. wlan0mon")
    ap.add_argument("--target", type=parse_mac, help="destination MAC (the C3)")
    ap.add_argument("--src", type=parse_mac, default=parse_mac("02:45:53:50:44:52"),
                    help="source MAC (default locally-administered 02:ESPDR)")
    ap.add_argument("--count", type=int, default=20, help="frames to send")
    ap.add_argument("--interval", type=float, default=0.2, help="seconds between frames")
    ap.add_argument("--payload-len", type=int, default=16, help="dummy payload bytes")
    ap.add_argument("--listen", action="store_true",
                    help="best-effort: also sniff for ACKs with RA == target")
    ap.add_argument("--dry-run", action="store_true",
                    help="build and hexdump one frame, do not send (no scapy/root needed)")
    args = ap.parse_args()

    if not args.target and not args.dry_run:
        ap.error("--target is required unless --dry-run")

    target = args.target or parse_mac("8c:d0:b2:bb:ca:57")
    payload = bytes([0x11]) * args.payload_len
    frame0 = build_awdl_data_frame(target, args.src, 0, payload)

    if args.dry_run:
        print(f"frame length {len(frame0)} bytes (MAC header .. payload, no FCS):")
        print(hexdump(frame0))
        print("\ndry-run only; re-run without --dry-run (as root) to transmit.")
        return 0

    # scapy is only needed for actual transmit/sniff.
    try:
        from scapy.all import RadioTap, Raw, sendp, AsyncSniffer, Dot11
    except ImportError:
        print("scapy not installed. Try: pip install --user scapy", file=sys.stderr)
        return 1

    tgt_str = ":".join(f"{x:02x}" for x in target)

    sniffer = None
    acks = {"n": 0}
    if args.listen:
        def is_ack_to_target(pkt):
            return (pkt.haslayer(Dot11) and pkt.type == 1 and pkt.subtype == 13
                    and pkt.addr1 and pkt.addr1.lower() == tgt_str)
        def on_pkt(pkt):
            if is_ack_to_target(pkt):
                acks["n"] += 1
        sniffer = AsyncSniffer(iface=args.iface, prn=on_pkt, store=False)
        sniffer.start()
        time.sleep(0.2)

    print(f"injecting {args.count} AWDL data frames to {tgt_str} on {args.iface} "
          f"({len(frame0)} bytes each)")
    for i in range(args.count):
        frame = build_awdl_data_frame(target, args.src, i, payload)
        sendp(RadioTap() / Raw(load=frame), iface=args.iface, verbose=False)
        if args.interval:
            time.sleep(args.interval)

    if sniffer:
        time.sleep(0.3)
        sniffer.stop()
        print(f"saw {acks['n']} ACK(s) addressed to {tgt_str} on this card "
              f"(best-effort; trust a parallel capture for the real count)")

    print("done. Now read `stats` on the C3: did rx_data rise? "
          "And check the capture for ACKs with RA == the target.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
