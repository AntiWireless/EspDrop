# EspDrop - the USB link to the device: framing and message formats.
#
# One COBS-framed, type-tagged message stream over the device's USB-Serial/JTAG,
# mirroring firmware/spikes/main/bridge.{h,c} and the COBS codec in
# firmware/spikes/components/awdl/awdl_cobs.c. Device and host must agree byte
# for byte, so keep this in sync with those files (and docs/M4_ENTWURF.md,
# section 4).
#
# Wire format per message: COBS(type_byte || payload) followed by a 0x00
# delimiter. COBS guarantees the body carries no 0x00, so the delimiter is
# unambiguous and the stream resynchronises after any stray bytes.
#
# Pure Python, no OS dependencies (the serial port lives in transport.py), so
# it works on Windows and Linux and is shared by tools/espdrop_link.py.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import struct

# Message types - must match enum bridge_msg_type in firmware/spikes/main/bridge.h.
MSG_ECHO        = 1   # host -> device: payload echoed back
MSG_ECHO_REPLY  = 2   # device -> host: the echoed payload
MSG_LOG         = 3   # device -> host: UTF-8 log text
MSG_FRAME       = 4   # both: one Ethernet frame for awdl0 (L2 mode)
MSG_PEER_ADD    = 5   # device -> host: 6-byte peer MAC appeared
MSG_PEER_REMOVE = 6   # device -> host: 6-byte peer MAC gone
MSG_CMD         = 7   # host -> device: command text
MSG_CMD_REPLY   = 8   # device -> host: command output text
MSG_HELLO       = 9   # host -> device: u8 version, u8 mode (0 L2, 1 IP), u32 credit
MSG_HELLO_REPLY = 10  # device -> host: version, mode, mac[6], max_conns, max_chunk, caps
MSG_UDP_OPEN    = 24  # host -> device: u16 port, u8 flags (bit 0: ff02::fb)
MSG_UDP_RECV    = 25  # device -> host: u16 port, src[16], u16 src_port, u8 flags, data
MSG_UDP_SEND    = 26  # host -> device: u16 port, dst[16], u16 dst_port, data
MSG_UDP_CLOSE   = 27  # host -> device: u16 port
MSG_RESULT      = 28  # device -> host: u8 request type, u16 id, i8 err

PROTO_VERSION = 1     # BRIDGE_PROTO_VERSION
MODE_L2 = 0           # frames to/from the host's awdl0 (tools/awdl_bridge.py)
MODE_IP = 1           # the device's own IPv6 stack (this host app)

CAP_BLE = 0x01        # HELLO_REPLY caps: the BLE AirDrop wake (`wake`) works
CAP_IP  = 0x02        #                   IP mode is available

# RESULT error codes - enum sockproxy_err in firmware/spikes/main/sockproxy.h.
ERRORS = {
    0: "ok", -1: "lwIP refused", -2: "not in IP mode / interface down",
    -3: "no free socket", -4: "no memory on the device", -5: "malformed request",
    -6: "port not open",
}


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


# ---- message payloads ----------------------------------------------------

def hello(mode: int, credit: int = 0) -> bytes:
    """A HELLO message (docs/M4_ENTWURF.md, section 4)."""
    return frame(MSG_HELLO, struct.pack("<BBI", PROTO_VERSION, mode, credit))


def parse_hello_reply(p: bytes):
    """dict from a HELLO_REPLY payload, or None if it is too short."""
    if len(p) < 12:
        return None
    ver, mode = p[0], p[1]
    max_conns, max_chunk, caps = struct.unpack_from("<BHB", p, 8)
    return {"version": ver, "mode": mode, "mac": ":".join(f"{b:02x}" for b in p[2:8]),
            "max_conns": max_conns, "max_chunk": max_chunk, "caps": caps}


def udp_open(port: int, join_mdns: bool = True) -> bytes:
    return frame(MSG_UDP_OPEN, struct.pack("<HB", port, 0x01 if join_mdns else 0))


def udp_send(port: int, dst: bytes, dst_port: int, data: bytes) -> bytes:
    if len(dst) != 16:
        raise ValueError("dst must be a 16-byte IPv6 address")
    return frame(MSG_UDP_SEND, struct.pack("<H", port) + dst + struct.pack("<H", dst_port) + data)


def udp_close(port: int) -> bytes:
    return frame(MSG_UDP_CLOSE, struct.pack("<H", port))


def parse_udp_recv(p: bytes):
    """(port, src16, src_port, multicast, data) or None."""
    if len(p) < 21:
        return None
    port = struct.unpack_from("<H", p, 0)[0]
    src = p[2:18]
    sport = struct.unpack_from("<H", p, 18)[0]
    return port, src, sport, bool(p[20] & 0x01), p[21:]


def parse_result(p: bytes):
    """(request_type, id, err) or None."""
    if len(p) < 4:
        return None
    req, ident, err = struct.unpack_from("<BHb", p, 0)
    return req, ident, err
