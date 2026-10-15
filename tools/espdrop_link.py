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
# Stdlib only; no external dependencies.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import os
import select
import sys
import termios
import time
import tty

# Message types - must match enum bridge_msg_type in firmware/spikes/main/bridge.h.
MSG_ECHO        = 1   # host -> device: payload echoed back
MSG_ECHO_REPLY  = 2   # device -> host: the echoed payload
MSG_LOG         = 3   # device -> host: UTF-8 log text
MSG_FRAME       = 4   # both: one Ethernet frame for awdl0
MSG_PEER_ADD    = 5   # device -> host: 6-byte peer MAC appeared
MSG_PEER_REMOVE = 6   # device -> host: 6-byte peer MAC gone
MSG_CMD         = 7   # host -> device: command text
MSG_CMD_REPLY   = 8   # device -> host: command output text


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


def frame(msg_type: int, payload: bytes = b"") -> bytes:
    """Encode one message to its on-the-wire bytes (COBS body + 0x00 delimiter)."""
    return cobs_encode(bytes([msg_type]) + payload) + b"\x00"


class Reader:
    """Accumulates bytes; feed() returns completed (type, payload) frames.

    feed() consumes the whole chunk and returns a list (it must NOT be a
    generator): callers break out of their read loop as soon as they see the
    frame they want, and a suspended generator would (a) never run a buffer
    reset placed after a yield, so every later frame would decode merged onto
    the previous ones, and (b) drop any bytes after the delimiter in the
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
