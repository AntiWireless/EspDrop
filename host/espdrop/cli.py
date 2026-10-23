# EspDrop - command line of the M4 host app (docs/M4_ENTWURF.md, section 5).
#
#   espdrop find [-t SECS] [--no-wake] [-v]   AirDrop receivers on the AWDL link
#   espdrop status                            device, interface, counters
#   espdrop cmd TEXT...                       one device command (as in the bridge)
#
# The device is switched into IP mode (HELLO); it stays there until the next
# session (tools/awdl_bridge.py switches it back to L2 itself).
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import argparse
import os
import sys
import time

from . import link
from .device import Device, DeviceError


def _open(args, transport):
    from .transport import open_port
    port = transport or open_port(args.port)
    verbose = getattr(args, "verbose", False)

    def on_log(text):
        if verbose or not text.startswith("ping6:"):
            print(f"[device] {text}", flush=True)
    return Device(port, on_log=on_log)


def _hello(dev):
    h = dev.hello(link.MODE_IP)
    print(f"device {h['mac']}, protocol {h['version']}, IP mode, Bluetooth wake: "
          f"{'yes' if h['caps'] & link.CAP_BLE else 'no'}", flush=True)
    return h


def cmd_find(args, transport=None):
    from .mdns import AIRDROP, Browser
    dev = _open(args, transport)
    woke = False
    try:
        h = _hello(dev)
        if not args.no_wake:
            if h["caps"] & link.CAP_BLE:
                secs = min(600, int(args.time) + 10)
                reply = dev.cmd(f"wake {secs}")
                woke = reply.startswith("wake on")
                print(f"BLE wake from the ESP: {reply}", flush=True)
            else:
                print("no Bluetooth wake in this firmware: the target must already be "
                      "awake (or use --no-wake and wake it otherwise)", flush=True)

        def found(r):
            print(f"  found  {r.id}  [{r.parsed_addresses()[0]}]:{r.port}  flags {r.flags}"
                  f"  host {r.server}", flush=True)

        def gone(r):
            print(f"  gone   {r.id}", flush=True)

        def packet(src, mcast, what):
            print(f"  mdns   {src or '-'} {'multicast' if mcast else 'unicast'}: {what}",
                  flush=True)

        b = Browser(dev, on_found=found, on_gone=gone,
                    on_packet=packet if args.verbose else None)
        print(f"browsing {AIRDROP} on the ESP's AWDL link for {args.time:.0f}s "
              f"(target: AirDrop Everyone, unlocked, share sheet OPEN) ...", flush=True)
        b.start()
        try:
            end = time.monotonic() + args.time
            while time.monotonic() < end:
                time.sleep(0.2)
                if dev.error:
                    raise DeviceError(f"link to the device lost: {dev.error}")
        except KeyboardInterrupt:
            print()
        b.stop()
        rs = b.receivers()
        print(f"{len(rs)} receiver(s); {b.queries_sent} queries sent, "
              f"{b.packets} mDNS messages received, {b.invalid} unreadable", flush=True)
        if not rs:
            print("  - nothing: is the target on AirDrop Everyone with its share sheet open?\n"
                  "    `espdrop status` shows whether the ESP hears it at all (peers, rx).",
                  flush=True)
        return 0 if rs else 1
    finally:
        if woke:
            try:
                dev.cmd("wake off")
            except DeviceError:
                pass
        dev.close()


def cmd_send(args, transport=None):
    from .airdrop import find_receiver, send_files
    for f in args.file:
        if not os.path.isfile(f):
            print(f"error: no such file: {f}", file=sys.stderr)
            return 1
    dev = _open(args, transport)
    woke = False
    try:
        h = _hello(dev)
        if not args.no_wake and h["caps"] & link.CAP_BLE:
            secs = min(600, int(args.time) + 120)   # also covers Accept and the upload
            reply = dev.cmd(f"wake {secs}")
            woke = reply.startswith("wake on")
            print(f"BLE wake from the ESP: {reply}", flush=True)
        elif not args.no_wake:
            print("no Bluetooth wake in this firmware: wake the target otherwise", flush=True)

        def packet(src, mcast, what):
            print(f"  mdns   {src or '-'}: {what}", flush=True)

        print(f"looking for {args.receiver or 'a receiver'} "
              f"(target: AirDrop Everyone, unlocked, share sheet OPEN) ...", flush=True)
        r = find_receiver(dev, timeout=args.time, want=args.receiver,
                          on_packet=packet if args.verbose else None)
        if r is None:
            print("no AirDrop receiver found; `espdrop find -v` shows what the browse saw",
                  file=sys.stderr)
            return 1
        print(f"sending {len(args.file)} file(s) to {r.id} [{r.parsed_addresses()[0]}]:{r.port} "
              f"... (tap Accept on the device)", flush=True)
        rc = send_files(dev, r, args.file, name=args.name)
        print("sent." if rc == 0 else "send failed.", flush=True)
        return rc
    finally:
        if woke:
            try:
                dev.cmd("wake off")
            except DeviceError:
                pass
        dev.close()


def cmd_status(args, transport=None):
    dev = _open(args, transport)
    try:
        _hello(dev)
        for c in ("node", "ip", "stats", "wake"):
            print(f"{c}: {dev.cmd(c)}", flush=True)
        return 0
    finally:
        dev.close()


def cmd_cmd(args, transport=None):
    dev = _open(args, transport)
    try:
        _hello(dev)
        print(dev.cmd(" ".join(args.text), timeout=args.timeout), flush=True)
        if args.text and args.text[0] == "ping6":   # replies come as log lines
            time.sleep(args.wait)
        return 0
    finally:
        dev.close()


def main(argv=None, transport=None):
    ap = argparse.ArgumentParser(prog="espdrop", description="EspDrop host app (M4)")
    ap.add_argument("-p", "--port", help="serial port (default: the only ESP found; "
                                         "Linux /dev/ttyACM0, Windows COM5)")
    sub = ap.add_subparsers(dest="action", required=True)
    f = sub.add_parser("find", help="list AirDrop receivers on the AWDL link")
    f.add_argument("-t", "--time", type=float, default=30.0, help="seconds to browse")
    f.add_argument("--no-wake", action="store_true", help="do not send the BLE wake")
    f.add_argument("-v", "--verbose", action="store_true", help="show every AirDrop mDNS message")
    f.set_defaults(name="EspDrop")
    s = sub.add_parser("send", help="send files to an AirDrop receiver")
    s.add_argument("file", nargs="+")
    s.add_argument("-r", "--receiver", help="receiver id or host name (default: the first found)")
    s.add_argument("-n", "--name", default="EspDrop", help="sender name shown on the device")
    s.add_argument("-t", "--time", type=float, default=30.0, help="seconds to look for the receiver")
    s.add_argument("--no-wake", action="store_true", help="do not send the BLE wake")
    s.add_argument("-v", "--verbose", action="store_true")

    sub.add_parser("status", help="device, interface and counters")
    c = sub.add_parser("cmd", help="send one device command")
    c.add_argument("text", nargs="+")
    c.add_argument("--timeout", type=float, default=3.0)
    c.add_argument("--wait", type=float, default=12.0, help="ping6: seconds to show replies")
    c.set_defaults(verbose=True)
    args = ap.parse_args(argv)
    handlers = {"find": cmd_find, "send": cmd_send, "status": cmd_status, "cmd": cmd_cmd}
    try:
        return handlers[args.action](args, transport)
    except (DeviceError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        if isinstance(e, PermissionError):
            print("  (Linux: add yourself to the 'dialout' group - 'uucp' on Arch - and "
                  "log in again)", file=sys.stderr)
        return 1
