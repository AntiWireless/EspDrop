#!/usr/bin/env python3
# EspDrop - shared host-side library for the USB bridge link.
#
# One COBS-framed, type-tagged message stream over the device's USB-Serial/JTAG,
# mirroring firmware/spikes/main/bridge.{h,c} and the COBS codec in
# firmware/spikes/components/awdl/awdl_cobs.c. The device and this module must
# agree byte-for-byte, so keep the wire format here in sync with those files.
#
# Wire format per message: COBS(type_byte || payload) followed by a 0x00
# delimiter. COBS guarantees the body carries no 0x00, so the delimiter is
# unambiguous and the stream resynchronises after any stray bytes.
#
# Stdlib only; no external dependencies. POSIX only (termios).
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import os
import select
import struct
import sys
import termios
import time
import tty

# Framing and message formats live in host/espdrop/link.py (shared with the M4
# host app, which must also run on Windows); this module adds the POSIX serial
# helpers the Linux tools use.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "host"))
from espdrop.link import (  # noqa: E402  (path set up just above)
    CAP_BLE, CAP_IP, MODE_IP, MODE_L2, MSG_CMD, MSG_CMD_REPLY, MSG_ECHO,
    MSG_ECHO_REPLY, MSG_FRAME, MSG_HELLO, MSG_HELLO_REPLY, MSG_LOG, MSG_PEER_ADD,
    MSG_PEER_REMOVE, PROTO_VERSION, Reader, cobs_decode, cobs_encode, frame, hello,
    parse_hello_reply,
)


def open_serial_raw(port: str) -> int:
    """Open a serial/USB-CDC port as a raw binary fd (non-blocking)."""
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        tty.setraw(fd)
    except termios.error as e:
        # A cooked tty mangles binary (echo, CR/LF translation), so this is not
        # silently ignorable - warn loudly rather than corrupt the stream.
        print(f"warning: could not set {port} to raw mode ({e}); "
              f"binary framing may be corrupted", file=sys.stderr)
    return fd


def write_all(fd: int, data: bytes, timeout: float = 5.0) -> None:
    """Write every byte, looping over short writes and EAGAIN on a non-blocking fd."""
    mv = memoryview(data)
    deadline = time.monotonic() + timeout
    while mv:
        try:
            n = os.write(fd, mv)
            if n > 0:
                mv = mv[n:]
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"write of {len(data)} bytes stalled for {timeout}s")
            select.select([], [fd], [], remaining)


def read_frames(fd: int, reader: Reader, seconds: float):
    """Yield (type, payload) frames seen on fd within the given time window."""
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
