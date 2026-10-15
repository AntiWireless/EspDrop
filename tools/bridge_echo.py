#!/usr/bin/env python3
# EspDrop - M2.0 host-side echo test for the USB bridge transport.
#
# Flash the firmware in bridge mode (CONFIG_ESPDROP_BRIDGE_MODE=y), then run this
# against the device's USB-Serial/JTAG port. It sends COBS-framed ECHO messages
# and checks that the device returns them byte-for-byte, which validates the
# framing and USB transport before any AWDL data flows.
#
#   sudo tools/bridge_echo.py /dev/ttyACM0            # one echo, incl. zeros
#   sudo tools/bridge_echo.py /dev/ttyACM0 --count 500 --size 1024   # throughput
#
# The COBS codec and framing live in espdrop_link.py, shared with the device.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import espdrop_link as link


def main() -> int:
    ap = argparse.ArgumentParser(description="EspDrop bridge echo test")
    ap.add_argument("port")
    ap.add_argument("--count", type=int, default=1, help="echo messages to send")
    ap.add_argument("--size", type=int, default=32, help="payload size per echo")
    ap.add_argument("--timeout", type=float, default=2.0, help="per-echo reply timeout")
    args = ap.parse_args()

    fd = link.open_serial_raw(args.port)
    reader = link.Reader()

    # Drain and print any startup LOG lines first.
    for t, p in link.read_frames(fd, reader, 0.3):
        if t == link.MSG_LOG:
            print(f"[device] {p.decode('utf-8', 'replace')}")

    # A payload that includes 0x00 bytes, to exercise COBS stuffing.
    base = bytes((i * 7 + 1) & 0xFF for i in range(args.size))
    payload = bytearray(base)
    for k in range(0, args.size, 5):
        payload[k] = 0  # scatter zeros through it
    payload = bytes(payload)

    ok = 0
    t0 = time.monotonic()
    total_bytes = 0
    for n in range(args.count):
        link.write_all(fd, link.frame(link.MSG_ECHO, payload))
        got = False
        for t, p in link.read_frames(fd, reader, args.timeout):
            if t == link.MSG_LOG:
                print(f"[device] {p.decode('utf-8', 'replace')}")
            elif t == link.MSG_ECHO_REPLY:
                if p == payload:
                    ok += 1
                    total_bytes += len(p)
                    got = True
                else:
                    print(f"echo {n}: MISMATCH ({len(p)} bytes back, {len(payload)} sent)")
                    got = True
                break
        if not got:
            print(f"echo {n}: no reply within {args.timeout}s")
    dt = time.monotonic() - t0
    os.close(fd)

    print(f"\n{ok}/{args.count} echoes OK")
    if ok and dt > 0:
        print(f"~{total_bytes / dt / 1000:.1f} kB/s payload round-tripped over {dt:.2f}s")
    return 0 if ok == args.count else 1


if __name__ == "__main__":
    sys.exit(main())
