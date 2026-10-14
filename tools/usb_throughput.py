#!/usr/bin/env python3
# EspDrop - measure USB-Serial/JTAG throughput from the host side.
#
# Run this, then trigger `usbbench <kib>` on the device console. It counts the
# bytes that arrive and reports the sustained rate. Rough but honest: it is the
# host actually reading the CDC endpoint, not the device guessing.
#
# Usage: tools/usb_throughput.py /dev/ttyACM0 [seconds]
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import os
import sys
import time


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: usb_throughput.py <port> [seconds]", file=sys.stderr)
        return 2
    port = sys.argv[1]
    window = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0

    try:
        fd = os.open(port, os.O_RDONLY | os.O_NOCTTY)
    except OSError as e:
        print(f"cannot open {port}: {e}", file=sys.stderr)
        return 1

    print(f"reading {port} for {window:.0f}s; trigger `usbbench` on the device now ...")
    total = 0
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < window:
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if chunk:
                total += len(chunk)
    finally:
        os.close(fd)

    dt = time.monotonic() - t0
    rate = total / dt / 1000.0 if dt > 0 else 0.0
    print(f"{total} bytes in {dt:.2f}s = {rate:.1f} kB/s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
