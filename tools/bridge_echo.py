#!/usr/bin/env python3
# EspDrop - M2.0 host-side echo test for the USB bridge transport.
#
# Flash the firmware in bridge mode (menuconfig -> EspDrop -> Bridge mode, i.e.
# CONFIG_ESPDROP_BRIDGE_MODE=y), then run this against the device's USB-Serial/
# JTAG port. It sends COBS-framed ECHO messages and checks that the device
# returns them byte-for-byte, which validates the framing and USB transport
# before any AWDL data flows.
#
#   sudo tools/bridge_echo.py /dev/ttyACM0            # one echo, incl. zeros
#   sudo tools/bridge_echo.py /dev/ttyACM0 --count 500 --size 1024   # throughput
#
# The COBS codec here mirrors firmware/spikes/components/awdl/awdl_cobs.c; a
# mismatch would show up immediately as a failed echo.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import argparse
import os
import select
import sys
import termios
import time
import tty

MSG_ECHO = 1
MSG_ECHO_REPLY = 2
MSG_LOG = 3


def cobs_encode(data: bytes) -> bytes:
    out = bytearray()
    code_i = len(out)
    out.append(0)  # placeholder for the first code byte
    code = 1
    for b in data:
        if b == 0:
            out[code_i] = code
            code_i = len(out)
            out.append(0)
            code = 1
        else:
            out.append(b)
            code += 1
            if code == 0xFF:
                out[code_i] = code
                code_i = len(out)
                out.append(0)
                code = 1
    out[code_i] = code
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        code = data[i]
        i += 1
        if code == 0:
            raise ValueError("zero byte inside frame")
        for _ in range(code - 1):
            if i >= n:
                raise ValueError("truncated")
            out.append(data[i])
            i += 1
        if code != 0xFF and i < n:
            out.append(0)
    return bytes(out)


def frame(msg_type: int, payload: bytes) -> bytes:
    return cobs_encode(bytes([msg_type]) + payload) + b"\x00"


class Reader:
    """Accumulates bytes; feed() returns completed (type, payload) frames.

    feed() consumes the whole chunk and returns a list (it must NOT be a
    generator): the caller breaks out of its read loop as soon as it sees the
    reply it wants, and a suspended generator would (a) never run the buffer
    reset that follows a yield, so every later frame would be decoded merged
    onto the previous ones, and (b) drop any bytes after the delimiter in the
    current chunk. Returning a list keeps self.buf correct no matter when the
    caller stops reading.
    """
    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes):
        frames = []
        for b in data:
            if b == 0:
                if self.buf:
                    frame_bytes = bytes(self.buf)
                    self.buf = bytearray()  # reset BEFORE handing the frame out
                    try:
                        dec = cobs_decode(frame_bytes)
                        if dec:
                            frames.append((dec[0], dec[1:]))
                    except ValueError:
                        pass
            else:
                self.buf.append(b)
        return frames


def open_raw(port: str) -> int:
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        tty.setraw(fd)
    except termios.error:
        pass
    return fd


def read_for(fd: int, reader: Reader, seconds: float):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        r, _, _ = select.select([fd], [], [], max(0.0, end - time.monotonic()))
        if not r:
            continue
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            continue
        if data:
            for fr in reader.feed(data):
                yield fr


def main() -> int:
    ap = argparse.ArgumentParser(description="EspDrop bridge echo test")
    ap.add_argument("port")
    ap.add_argument("--count", type=int, default=1, help="echo messages to send")
    ap.add_argument("--size", type=int, default=32, help="payload size per echo")
    ap.add_argument("--timeout", type=float, default=2.0, help="per-echo reply timeout")
    args = ap.parse_args()

    fd = open_raw(args.port)
    reader = Reader()

    # Drain and print any startup LOG lines first.
    for t, p in read_for(fd, reader, 0.3):
        if t == MSG_LOG:
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
        os.write(fd, frame(MSG_ECHO, payload))
        got = False
        for t, p in read_for(fd, reader, args.timeout):
            if t == MSG_LOG:
                print(f"[device] {p.decode('utf-8', 'replace')}")
            elif t == MSG_ECHO_REPLY:
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
