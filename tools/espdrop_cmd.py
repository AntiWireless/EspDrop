#!/usr/bin/env python3
# EspDrop - send one command to the device through a running awdl_bridge.py.
#
# The bridge owns the serial port; other tools reach the device through its
# control socket /run/espdrop/<iface>.sock (owned by the user who started the
# bridge with sudo). One command per call, the device's reply is printed.
#
#   tools/espdrop_cmd.py node
#   tools/espdrop_cmd.py wake 120          # AirDrop wake from the ESP's Bluetooth
#   tools/espdrop_cmd.py wake off
#   tools/espdrop_cmd.py --appleid you@icloud.com wake 120   # contacts-only target
#
# For `wake <secs>`, --appleid / --phone / --email / --email2 are hashed as in
# tools/airdrop_wake.py (first 2 bytes of SHA-256 each) and sent along.
# Exit status: 0 when the device answered, 1 when it reported an error or could
# not be reached.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import argparse
import hashlib
import os
import socket
import sys

CTL_DIR = "/run/espdrop"


def hash2(identifier: str) -> bytes:
    """First 2 bytes of SHA-256(identifier), or 0x0000 for an empty string
    (the same as tools/airdrop_wake.py)."""
    if not identifier:
        return b"\x00\x00"
    return hashlib.sha256(identifier.encode()).digest()[:2]


def main() -> int:
    ap = argparse.ArgumentParser(description="send a command to the EspDrop device")
    ap.add_argument("-i", "--iface", default="awdl0", help="bridge interface (default awdl0)")
    ap.add_argument("-t", "--timeout", type=float, default=5.0, help="seconds to wait")
    ap.add_argument("--appleid", default="", help="Apple ID to hash into `wake`")
    ap.add_argument("--phone", default="", help="phone number to hash into `wake`")
    ap.add_argument("--email", default="", help="email address to hash into `wake`")
    ap.add_argument("--email2", default="", help="second email address to hash into `wake`")
    ap.add_argument("command", nargs="+", help="device command, e.g. node, wake 120")
    args = ap.parse_args()

    words = args.command
    ids = (args.appleid, args.phone, args.email, args.email2)
    if words[0] == "wake" and len(words) == 2 and words[1] != "off" and any(ids):
        words = words + [b"".join(hash2(x) for x in ids).hex()]
    cmd = " ".join(words)

    path = os.path.join(CTL_DIR, f"{args.iface}.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(args.timeout)
    try:
        s.connect(path)
        try:
            s.sendall(cmd.encode() + b"\n")
        except (BrokenPipeError, ConnectionResetError):
            pass   # a busy bridge answers and closes at once; its reply is still readable
        reply = b""
        while not reply.endswith(b"\n"):
            chunk = s.recv(512)
            if not chunk:
                break
            reply += chunk
    except FileNotFoundError:
        print(f"error: no bridge control socket {path}; is "
              f"'sudo tools/awdl_bridge.py /dev/ttyACM0' running?", file=sys.stderr)
        return 1
    except PermissionError:
        print(f"error: {path} belongs to another user; start the bridge with sudo "
              f"from this user's session", file=sys.stderr)
        return 1
    except OSError as e:   # timeout, refused (bridge gone)
        print(f"error: {path}: {e}", file=sys.stderr)
        return 1
    finally:
        s.close()

    text = reply.decode("utf-8", "replace").strip()
    print(text)
    failed = (not text or text.startswith("error") or text.startswith("wake error")
              or text.startswith("wake unavailable") or text.startswith("wake: "))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
