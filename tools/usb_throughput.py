#!/usr/bin/env python3
# EspDrop - measure USB-Serial/JTAG throughput from the host side.
#
# Two ways to use it:
#   1. Self-triggering (recommended): the tool sends `usbbench <kib>` to the
#      device itself, then times the bytes that come back. Close `idf.py monitor`
#      first - only one program may read the port at a time.
#        sudo tools/usb_throughput.py /dev/ttyACM0 --trigger 2048
#   2. Passive: just time whatever arrives in a window; trigger `usbbench` by
#      hand in a monitor on another machine.
#        tools/usb_throughput.py /dev/ttyACM0 --seconds 10
#
# Timing is gap-based: the clock starts at the first byte and stops after a
# 0.5 s lull, so the rate reflects the actual burst, not idle wait time.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import argparse
import os
import sys
import termios
import time
import tty


def main() -> int:
    ap = argparse.ArgumentParser(description="EspDrop USB-Serial/JTAG throughput meter")
    ap.add_argument("port", help="serial device, e.g. /dev/ttyACM0")
    ap.add_argument("--trigger", type=int, metavar="KIB",
                    help="send `usbbench <KIB>` to the device before timing "
                         "(close idf.py monitor first)")
    ap.add_argument("--seconds", type=float, default=12.0,
                    help="maximum time to wait/read (default 12)")
    ap.add_argument("--gap", type=float, default=0.5,
                    help="end the measurement after this long with no data (default 0.5)")
    args = ap.parse_args()

    flags = os.O_RDWR if args.trigger else os.O_RDONLY
    try:
        fd = os.open(args.port, flags | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as e:
        print(f"cannot open {args.port}: {e}", file=sys.stderr)
        return 1

    # Raw mode so the command and the binary stream pass through untouched.
    try:
        tty.setraw(fd)
    except termios.error:
        pass  # not a tty (e.g. a pipe in testing): fine

    if args.trigger:
        os.write(fd, f"usbbench {args.trigger}\r\n".encode())

    print(f"timing {args.port} (up to {args.seconds:.0f}s, "
          f"stop after {args.gap:.1f}s idle) ...")
    total = 0
    deadline = time.monotonic() + args.seconds
    t_first = None
    t_last = None
    while time.monotonic() < deadline:
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            if t_last is not None and time.monotonic() - t_last > args.gap:
                break
            time.sleep(0.005)
            continue
        if chunk:
            now = time.monotonic()
            if t_first is None:
                t_first = now
            t_last = now
            total += len(chunk)
    os.close(fd)

    if t_first is None or t_last is None or t_last == t_first:
        print(f"{total} bytes; not enough data to time a rate "
              f"(did you trigger `usbbench`? is a monitor still holding the port?)")
        return 0
    dt = t_last - t_first
    print(f"{total} bytes in {dt:.2f}s = {total / dt / 1000.0:.1f} kB/s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
