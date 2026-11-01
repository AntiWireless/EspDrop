# EspDrop - command line of the M4 host app (docs/M4_ENTWURF.md, section 5).
#
#   espdrop find [-t SECS] [--no-wake] [-v]   AirDrop receivers on the AWDL link
#   espdrop send FILE... [-r NAME] [-n NAME]  send files to a receiver (pick one if several)
#   espdrop receive [-n NAME] [-d DIR] [-y]   receive files, asking before each transfer
#   espdrop status                            device, interface, counters
#   espdrop cmd TEXT...                       one device command (as in the bridge)
#   espdrop check                             is this PC ready (no device needed)
#   espdrop gui [--background] [--send FILE...]  the desktop app (gui.py, PySide6)
#
# The device is switched into IP mode (HELLO); it stays there until the next
# session (tools/awdl_bridge.py switches it back to L2 itself).
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import argparse
import logging
import os
import sys
import threading
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


def _hold(dev, secs):
    """Keep the ESP's radio on for `secs` (0: let it sleep again). Firmware
    without a sleep mode answers `unknown cmd`: its radio is always on."""
    try:
        dev.cmd(f"power hold {int(secs)}" if secs else "power release")
    except DeviceError:
        pass


def cmd_find(args, transport=None):
    from .mdns import AIRDROP, Browser
    dev = _open(args, transport)
    woke = False
    try:
        h = _hello(dev)
        _hold(dev, args.time + 10)
        if not args.no_wake:
            if h["caps"] & link.CAP_BLE:
                secs = min(600, int(args.time) + 10)
                reply = dev.cmd(f"wake {secs}")
                woke = reply.startswith("wake on")
                print(f"BLE wake from the ESP: {reply}", flush=True)
            else:
                print("no Bluetooth wake in this firmware: the target must already be "
                      "awake (or use --no-wake and wake it otherwise)", flush=True)

        try:
            from .airdrop import discover    # needs OpenDrop; without it ids only
        except ImportError as e:
            discover = None
            print(f"(no names: {e})", flush=True)

        def ask_name(r):
            try:
                info = discover(dev, r)
            except Exception as e:           # noqa: BLE001
                print(f"  name   {r.id}  ? ({e})", flush=True)
                return
            if info:
                model = f" ({info['model']})" if info.get("model") else ""
                print(f"  name   {r.id}  {info['name']!r}{model}", flush=True)
            else:
                print(f"  name   {r.id}  ? (no name in its /Discover answer)", flush=True)

        def found(r):
            print(f"  found  {r.id}  [{r.parsed_addresses()[0]}]:{r.port}  flags {r.flags}"
                  f"  host {r.server}", flush=True)
            if discover:
                threading.Thread(target=ask_name, args=(r,), daemon=True).start()

        def gone(r):
            print(f"  gone   {r.id}", flush=True)

        def packet(src, mcast, what):
            print(f"  mdns   {src or '-'} {'multicast' if mcast else 'unicast'}: {what}",
                  flush=True)

        b = Browser(dev, on_found=found, on_gone=gone,
                    on_packet=packet if args.verbose else None)
        print(f"browsing {AIRDROP} on the ESP's AWDL link for {args.time:.0f}s "
              f"(target: AirDrop Everyone, unlocked) ...", flush=True)
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
            print("  - nothing: is the target unlocked and on AirDrop Everyone?\n"
                  "    `espdrop status` shows whether the ESP hears it at all (peers, rx).",
                  flush=True)
        return 0 if rs else 1
    finally:
        if woke:
            try:
                dev.cmd("wake off")
            except DeviceError:
                pass
        if not dev.error:
            _hold(dev, 0)
        dev.close()


def _label(r, info):
    model = f" ({info['model']})" if info.get("model") else ""
    return f"{info['name']!r}{model} [{r.id}]" if info.get("name") else f"[{r.id}] (no name)"


def _pick(hits):
    """Several receivers: let the user choose (only from a terminal)."""
    print(f"{len(hits)} receivers:", flush=True)
    for n, (r, info) in enumerate(hits, 1):
        print(f"  {n}) {_label(r, info)}", flush=True)
    if not sys.stdin.isatty():
        print("error: several receivers; pick one with -r NAME (or its id)", file=sys.stderr)
        return None
    while True:
        try:
            answer = input(f"send to [1-{len(hits)}, empty = cancel]: ").strip()
        except EOFError:
            return None
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(hits):
            return hits[int(answer) - 1]


def cmd_send(args, transport=None):
    from .airdrop import find_receivers, send_files
    for f in args.file:
        if not os.path.isfile(f):
            print(f"error: no such file: {f}", file=sys.stderr)
            return 1
    dev = _open(args, transport)
    woke = False
    try:
        h = _hello(dev)
        _hold(dev, min(3600, args.time + 600))     # browse, Accept and the upload
        if not args.no_wake and h["caps"] & link.CAP_BLE:
            secs = min(600, int(args.time) + 120)   # also covers Accept and the upload
            reply = dev.cmd(f"wake {secs}")
            woke = reply.startswith("wake on")
            print(f"BLE wake from the ESP: {reply}", flush=True)
        elif not args.no_wake:
            print("no Bluetooth wake in this firmware: wake the target otherwise", flush=True)

        def packet(src, mcast, what):
            print(f"  mdns   {src or '-'}: {what}", flush=True)

        t0 = time.monotonic()
        print(f"looking for {args.receiver or 'receivers'} "
              f"(target: AirDrop Everyone, unlocked) ...", flush=True)
        hits = find_receivers(dev, timeout=args.time, want=args.receiver, name=args.name,
                              on_packet=packet if args.verbose else None)
        if not hits:
            print(f"no AirDrop receiver {'matching ' + repr(args.receiver) + ' ' if args.receiver else ''}"
                  f"found; `espdrop find -v` shows what the browse saw", file=sys.stderr)
            return 1
        chosen = hits[0] if len(hits) == 1 else _pick(hits)
        if chosen is None:
            return 1
        r, info = chosen
        print(f"found {_label(r, info)} after {time.monotonic() - t0:.1f} s; "
              f"sending {len(args.file)} file(s)", flush=True)
        rc = send_files(dev, r, args.file, name=args.name)
        print("sent." if rc == 0 else "send failed.", flush=True)
        return rc
    finally:
        if woke:
            try:
                dev.cmd("wake off")
            except DeviceError:
                pass
        if not dev.error:
            _hold(dev, 0)
        dev.close()


class _Prompt:
    """Yes/no questions on the terminal while other threads run: one reader
    thread owns stdin, and a question that gets no answer in time is a no."""

    def __init__(self):
        import queue
        self._q = queue.Queue()
        self._lock = threading.Lock()
        threading.Thread(target=self._read, name="espdrop-stdin", daemon=True).start()

    def _read(self):
        for line in sys.stdin:
            self._q.put(line.strip())
        self._q.put(None)

    def ask(self, text, timeout):
        import queue
        with self._lock:                       # one question at a time
            while not self._q.empty():
                self._q.get_nowait()           # nothing typed earlier counts
            print(f"{text} [y/N, {timeout:.0f} s] ", end="", flush=True)
            try:
                answer = self._q.get(timeout=timeout)
            except queue.Empty:
                print("\n  (no answer: declined)", flush=True)
                return False
            return (answer or "").lower() in ("y", "yes", "j", "ja")


def cmd_receive(args, transport=None):
    from .receive import Receiver
    if args.dir:
        os.makedirs(args.dir, exist_ok=True)
        os.chdir(args.dir)                     # OpenDrop's server writes into cwd
    if args.yes:
        on_ask = None
        print("accepting every transfer without asking (--yes)", flush=True)
    elif sys.stdin.isatty():
        prompt = _Prompt()

        def on_ask(ask):
            return prompt.ask(f"accept from {ask.summary()}?", args.ask_timeout)
    else:
        print("stdin is not a terminal: every transfer will be declined "
              "(--yes accepts all)", flush=True)

        def on_ask(ask):
            print(f"declined (no terminal to ask): {ask.summary()}", flush=True)
            return False

    def on_event(kind, **info):
        if kind == "received":
            ask = info.get("ask")
            who = ask.sender if ask else "?"
            rate = info["size"] / 1e6 / max(info["secs"], 1e-3)
            if info["ok"]:
                print(f"received from {who}: {', '.join(info['files'])} "
                      f"({info['size'] / 1e6:.2f} MB, {rate:.2f} MB/s)", flush=True)
            else:
                print(f"transfer from {who} incomplete: files {info['files'] or 'none'}, "
                      f"raw archive kept: {info['raw'] or 'no'}", flush=True)

    dev = _open(args, transport)
    rx = Receiver(dev, name=args.name, on_ask=on_ask, on_event=on_event,
                  announce_interval=2.5)
    listen = False
    try:
        h = _hello(dev)
        listen = bool(h["caps"] & link.CAP_SCAN)
        rx.start()
        if listen:
            dev.cmd("power listen on")             # the ESP wakes itself on AirDrop adverts
            print("the ESP listens for AirDrop and keeps its Wi-Fi asleep until a device is "
                  "near.", flush=True)
        print(f"ready: on the iPhone/iPad, share a file and pick {args.name!r}; files go to "
              f"{os.getcwd()}. Ctrl+C to stop.", flush=True)
        try:
            renew = 0.0
            while not dev.error:
                if time.monotonic() >= renew:       # keep the host's requests from lapsing
                    _hold(dev, 90) if not listen else dev.cmd("power")
                    renew = time.monotonic() + 30
                time.sleep(0.3)
        except KeyboardInterrupt:
            print()
        return 0
    finally:
        rx.stop()
        if not dev.error:
            if listen:
                try:
                    dev.cmd("power listen off")
                except DeviceError:
                    pass
            _hold(dev, 0)
        dev.close()


def cmd_status(args, transport=None):
    from .flash import parse_version
    dev = _open(args, transport)
    try:
        _hello(dev)
        fw = parse_version(dev.cmd("version"))
        print(f"firmware: {fw['version']} ({fw.get('chip', '?')}, IDF {fw.get('idf', '?')})"
              if fw else "firmware: from before 0.1.0 (no `version` command)", flush=True)
        for c in ("node", "ip", "stats", "wake", "power"):
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


def cmd_flash(args, transport=None):
    """Put the EspDrop firmware on the ESP (esptool, no IDF needed)."""
    from . import flash
    path = args.image or flash.bundled()
    if not path:
        print("error: no firmware here: name an image (espdrop-esp32c5.bin from a release) "
              "or an ESP-IDF build folder of the bridge", file=sys.stderr)
        return 1
    try:
        fw = flash.load(path)
        port = args.port
        if not port:
            found = flash.ports()
            if len(found) != 1:
                print("error: " + ("no ESP found; plug it in" if not found else
                                   f"several ports ({', '.join(found)}); pick one: "
                                   f"espdrop -p PORT flash"), file=sys.stderr)
                return 1
            port = found[0]
        print(f"firmware {fw.version} for the {fw.chip} (built {fw.built}, IDF {fw.idf}, "
              f"{fw.size // 1024} KiB)\n  from {fw.source}\nport {port}", flush=True)
        state = {"bar": False}

        def progress(stage, fraction=None):
            if stage == "write":
                print(f"\r  writing {fraction * 100:5.1f} %", end="", flush=True)
                state["bar"] = True
                return
            if state["bar"]:
                print(flush=True)
                state["bar"] = False
            text = {"connect": "connecting to the ESP's ROM bootloader ...",
                    "verify": "verifying ...", "restart": "restarting the ESP ...",
                    "done": "done: the ESP runs the new firmware."}.get(stage)
            if text:
                print(text, flush=True)
        flash.flash(fw, port, on_progress=progress,
                    on_log=(lambda text: print(f"  {text}", flush=True)) if args.verbose else None)
        return 0
    except flash.FirmwareError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 1


def cmd_check(args, transport=None):
    from . import env
    print("EspDrop host app: checking this PC (no device needed)", flush=True)
    n = env.check(lambda text: print(text, flush=True))
    print("ready." if n == 0 else f"{n} problem(s).", flush=True)
    return 0 if n == 0 else 1


def cmd_gui(args, transport=None):
    try:
        from . import gui
    except ImportError as e:
        raise ImportError(f"the GUI needs PySide6 ({e}): pip install PySide6 into the venv, "
                          f"or run tools/opendrop_install.py again") from e
    if args.action == "uninstall":
        return gui.uninstall_main()
    return gui.main(port=args.port, background=args.background, send=args.send,
                    installed_from=args.installed_from)


def main(argv=None, transport=None):
    # Device and file names can be anything; a Windows console or a redirect in
    # its code page must not end the run over one character.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    from .env import device_name
    from .version import __version__
    ap = argparse.ArgumentParser(prog="espdrop", description="EspDrop host app (M4)")
    ap.add_argument("--version", action="version", version=f"espdrop {__version__}")
    ap.add_argument("-p", "--port", help="serial port (default: the only ESP found; "
                                         "Linux /dev/ttyACM0, Windows COM5)")
    sub = ap.add_subparsers(dest="action", required=True)
    f = sub.add_parser("find", help="list AirDrop receivers on the AWDL link")
    f.add_argument("-t", "--time", type=float, default=30.0, help="seconds to browse")
    f.add_argument("--no-wake", action="store_true", help="do not send the BLE wake")
    f.add_argument("-v", "--verbose", action="store_true", help="show every AirDrop mDNS message")
    f.set_defaults(name=device_name())
    s = sub.add_parser("send", help="send files to an AirDrop receiver")
    s.add_argument("file", nargs="+")
    s.add_argument("-r", "--receiver", help="receiver name or id (default: the only one found, "
                                            "else you pick from a list)")
    s.add_argument("-n", "--name", default=device_name(),
                   help="sender name shown on the device (default: this PC's name)")
    s.add_argument("-t", "--time", type=float, default=30.0, help="seconds to look for the receiver")
    s.add_argument("--no-wake", action="store_true", help="do not send the BLE wake")
    s.add_argument("-v", "--verbose", action="store_true")

    rv = sub.add_parser("receive", help="receive files from an Apple device")
    rv.add_argument("-n", "--name", default=device_name(),
                    help="name shown on the sender (default: this PC's name)")
    rv.add_argument("-v", "--verbose", action="store_true", help="debug log with times")
    rv.add_argument("-y", "--yes", action="store_true", help="accept every transfer without asking")
    rv.add_argument("-d", "--dir", help="folder for received files (default: the current one)")
    rv.add_argument("--ask-timeout", type=float, default=60.0,
                    help="seconds to answer before a transfer is declined")

    sub.add_parser("status", help="device, interface and counters")
    c = sub.add_parser("cmd", help="send one device command")
    c.add_argument("text", nargs="+")
    c.add_argument("--timeout", type=float, default=3.0)
    c.add_argument("--wait", type=float, default=12.0, help="ping6: seconds to show replies")
    c.set_defaults(verbose=True)
    sub.add_parser("check", help="is this PC ready: Python, pyserial, OpenDrop, libarchive, TLS")
    fl = sub.add_parser("flash", help="put the EspDrop firmware on the ESP (esptool)")
    fl.add_argument("image", nargs="?", help="firmware image (espdrop-esp32c5.bin) or ESP-IDF "
                                              "build folder (default: the one this app ships)")
    fl.add_argument("-v", "--verbose", action="store_true", help="show esptool's messages")
    g = sub.add_parser("gui", help="the desktop app (PySide6)")
    g.add_argument("--background", action="store_true",
                   help="tray only, receiving until switched off (what autostart runs)")
    g.add_argument("--send", nargs="*", metavar="FILE",
                   help="ask whom to send these files to (Explorer's \"Senden an\")")
    g.add_argument("--installed-from", help=argparse.SUPPRESS)   # install.py: the download
    sub.add_parser("uninstall", help="remove the installed program (asks first)")
    if argv is None:
        argv = sys.argv[1:]
        from .frozen import clean_old_unpacked, compiled
        if not argv and compiled():
            argv = ["gui"]                     # double-clicked: the desktop app
        if compiled():
            clean_old_unpacked()               # older builds' unpacked copies
    args = ap.parse_args(argv)
    # Our steps (connect, accept, upload rate) at INFO; OpenDrop's own only from
    # WARNING on, unless -v asks for everything, with milliseconds since start.
    verbose = getattr(args, "verbose", False) and args.action != "cmd"
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, force=True,
                        format="%(relativeCreated)8.0f ms %(name)s: %(message)s" if verbose
                        else "%(message)s")
    if not verbose:
        logging.getLogger("opendrop").setLevel(logging.WARNING)
    handlers = {"find": cmd_find, "send": cmd_send, "receive": cmd_receive,
                "status": cmd_status, "cmd": cmd_cmd, "check": cmd_check,
                "flash": cmd_flash, "gui": cmd_gui, "uninstall": cmd_gui}
    try:
        return handlers[args.action](args, transport)
    except ImportError as e:            # OpenDrop or libarchive (env.py says what to do)
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (DeviceError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        if isinstance(e, PermissionError):
            print("  (Linux: add yourself to the 'dialout' group - 'uucp' on Arch - and "
                  "log in again)", file=sys.stderr)
        elif os.name == "nt" and "denied" in str(e).lower():
            print("  (Windows: another program has the COM port open - a serial monitor, "
                  "idf.py monitor or a second espdrop; close it)", file=sys.stderr)
        elif isinstance(e, TimeoutError) or "Timeout" in type(e).__name__:
            print("  (the ESP takes no data over USB: it is not running the bridge "
                  "firmware, or it hangs or is held in reset; unplug it and plug it "
                  "back in)", file=sys.stderr)
        return 1
