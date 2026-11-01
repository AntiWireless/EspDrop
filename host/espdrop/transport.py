# EspDrop - the serial port to the device, on Windows and Linux.
#
# pyserial when it is installed (needed on Windows: COM ports); otherwise a raw
# POSIX tty, so Linux needs no extra package. Both give the same small API.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import glob
import os
import sys

ESPRESSIF_VID = 0x303A   # USB-Serial/JTAG of the ESP32-C3/C5


class PosixPort:
    """A tty in raw mode (no echo, no CR/LF translation)."""

    def __init__(self, name: str):
        import termios
        import tty
        self.name = name
        self.fd = os.open(name, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            tty.setraw(self.fd)
        except termios.error as e:
            os.close(self.fd)
            raise OSError(f"cannot put {name} into raw mode: {e}") from e

    def read(self, timeout: float) -> bytes:
        import select
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return b""
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return b""
        if data == b"":
            raise EOFError(f"{self.name} closed (device unplugged?)")
        return data

    def write(self, data: bytes, timeout: float = 5.0) -> None:
        import select
        import time
        mv = memoryview(data)
        deadline = time.monotonic() + timeout
        while mv:
            try:
                n = os.write(self.fd, mv)
                mv = mv[n:]
            except BlockingIOError:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f"write to {self.name} stalled for {timeout}s")
                select.select([], [self.fd], [], left)

    def close(self) -> None:
        os.close(self.fd)


class PySerialPort:
    """pyserial, for Windows (COMx) and wherever it is installed."""

    def __init__(self, name: str):
        import serial
        self.name = name
        ser = serial.Serial(timeout=0.2, write_timeout=5.0)      # not opened yet
        ser.port = name
        if os.name == "nt":
            # The USB-Serial/JTAG holds the chip in reset while RTS is on and
            # DTR off, and Windows' usbser.sys does not always pass DTR on: with
            # pyserial's default (both on) the chip can end up held in reset and
            # every write times out. Both off: nothing here can reset it. (Linux
            # keeps the default; its open raises both lines at once.)
            ser.dtr = False
            ser.rts = False
        ser.open()
        self.ser = ser
        # Setting a timeout reconfigures the port on Windows (SetCommState,
        # SetCommTimeouts), so only when it changes, not on every call.
        self._rt, self._wt = 0.2, 5.0

    def read(self, timeout: float) -> bytes:
        if timeout != self._rt:
            self.ser.timeout = self._rt = timeout
        n = max(1, self.ser.in_waiting)
        return self.ser.read(n)

    def write(self, data: bytes, timeout: float = 5.0) -> None:
        if timeout != self._wt:
            self.ser.write_timeout = self._wt = timeout
        self.ser.write(data)

    def close(self) -> None:
        self.ser.close()


def candidates():
    """Serial ports that look like an ESP USB-Serial/JTAG, best first."""
    try:
        from serial.tools import list_ports
        found = [p.device for p in list_ports.comports() if p.vid == ESPRESSIF_VID]
        if found:
            return found
    except ImportError:
        pass
    if sys.platform.startswith("linux"):
        by_id = sorted(glob.glob("/dev/serial/by-id/*Espressif*"))
        if by_id:
            return [os.path.realpath(p) for p in by_id]
        return sorted(glob.glob("/dev/ttyACM*"))
    return []


def open_port(name=None):
    """Open the given port, or the only ESP found. Raises OSError with advice."""
    if not name:
        found = candidates()
        if not found:
            raise OSError("no ESP found; plug it in or name the port with -p "
                          "(Linux /dev/ttyACM0, Windows COM5)")
        if len(found) > 1:
            raise OSError(f"several ports found ({', '.join(found)}); pick one with -p")
        name = found[0]
    try:
        import serial  # noqa: F401
        return PySerialPort(name)
    except ImportError:
        if os.name != "posix":
            raise OSError("pyserial is needed on this system: pip install pyserial")
        return PosixPort(name)
