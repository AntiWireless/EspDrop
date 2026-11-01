# EspDrop - receive files from an iPhone/iPad through the ESP (M4.4).
#
# The patched OpenDrop runs its real AirDrop server (TLS, /Discover, /Ask,
# /Upload, the archive handling) on an IPv4 loopback socket. The device accepts
# the AWDL connections the phone makes to us and, for each, we splice its bytes
# to a fresh loopback connection into that server. mDNS is announced over the
# device's UDP proxy (mdns.Responder). So receiving needs no awdl0, root or
# firewall rule either, on Windows and Linux.
#
# The server's TLS accept stays on its own listening socket (so it wraps each
# connection itself); we only move bytes, we do not re-implement TLS. A handler
# subclass asks our caller before a transfer is accepted (on_ask; OpenDrop on
# its own accepts every /Ask) and reports the upload's progress and the files it
# left in the current directory (on_event).
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import logging
import os
import plistlib
import shutil
import socket
import threading
import time
from http.server import ThreadingHTTPServer

from . import env

env.load_opendrop()        # libarchive and the Windows fixes first; ImportError with advice
from opendrop.config import AirDropConfig  # noqa: E402
from opendrop.server import AirDropServerHandler, ask_items  # noqa: E402

from . import mdns  # noqa: E402

logger = logging.getLogger(__name__)
AIRDROP_PORT = 8771
RAW_PREFIX = "airdrop-received-"     # OpenDrop keeps an archive it could not unpack under this


class _V4Server(ThreadingHTTPServer):
    address_family = socket.AF_INET
    daemon_threads = True
    allow_reuse_address = True


def eui64_linklocal(mac6: bytes) -> bytes:
    """The EUI-64 link-local the device uses on AWDL (matches awdl_netif.c)."""
    m = bytearray(mac6)
    iid = bytes([m[0] ^ 0x02, m[1], m[2], 0xff, 0xfe, m[3], m[4], m[5]])
    return b"\xfe\x80\x00\x00\x00\x00\x00\x00" + iid


class IncomingAsk:
    """What a sender's /Ask says: who, which files (or links), a thumbnail."""

    def __init__(self, body: bytes):
        try:
            req = plistlib.loads(body)
        except Exception:                 # noqa: BLE001 - ask anyway, with what we have
            req = {}
        self.sender = str(req.get("SenderComputerName") or "unknown sender")
        self.model = req.get("SenderModelName")
        self.files = [str(f.get("FileName", "?")) for f in (req.get("Files") or [])
                      if isinstance(f, dict)]
        self.urls = ask_items(req) if req else []
        icon = req.get("FileIcon")
        self.icon = bytes(icon) if isinstance(icon, (bytes, bytearray)) else None

    def summary(self):
        what = self.files or self.urls
        text = ", ".join(what[:3]) + (f" (+{len(what) - 3} more)" if len(what) > 3 else "")
        return f"{self.sender}: {text or 'nothing named'}"


class _Counting:
    """A request body reader that reports how many bytes went through."""

    def __init__(self, f, on_bytes):
        self._f, self._on = f, on_bytes

    def read(self, *a):
        b = self._f.read(*a)
        self._on(len(b))
        return b

    def readline(self, *a):
        b = self._f.readline(*a)
        self._on(len(b))
        return b

    def readinto(self, buf):
        n = self._f.readinto(buf)
        self._on(n or 0)
        return n

    def __getattr__(self, name):
        return getattr(self._f, name)


def unique_path(folder, name):
    """folder/name, or 'name (1).ext', 'name (2).ext' … if that is taken."""
    path = os.path.join(folder, name)
    stem, ext = os.path.splitext(name)
    n = 1
    while os.path.lexists(path):
        path = os.path.join(folder, f"{stem} ({n}){ext}")
        n += 1
    return path


APPLEDOUBLE_MAGIC = b"\x00\x05\x16\x07"


def is_appledouble(path):
    """A '._name' file of Apple's AppleDouble format: the metadata (extended
    attributes) iOS sends along with each file. Apple's own unpacking folds it
    into the file; elsewhere it would land beside it as a second file."""
    if not os.path.basename(path).startswith("._") or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as f:
            return f.read(4) == APPLEDOUBLE_MAGIC
    except OSError:
        return False


def drop_appledouble(folder, names):
    """Remove the AppleDouble companions among folder/names (in subfolders
    too); returns the names left."""
    left = []
    for name in names:
        path = os.path.join(folder, name)
        if is_appledouble(path):
            os.remove(path)
            continue
        if os.path.isdir(path):
            for sub, _dirs, files in os.walk(path):
                for f in files:
                    if is_appledouble(os.path.join(sub, f)):
                        os.remove(os.path.join(sub, f))
        left.append(name)
    return left


def move_out(staging, names, dest):
    """Move staging/name for each name into dest under a free name; returns
    the new paths. Nothing in dest is ever replaced. What cannot move (a file
    still locked on Windows) stays in staging, under its staging path, and
    moves out at the next start."""
    moved = []
    for name in names:
        src = os.path.join(staging, name)
        target = unique_path(dest, name)
        try:
            shutil.move(src, target)           # a rename; copy + remove across volumes
        except OSError as e:
            logger.warning("could not move %s to %s: %s", name, dest, e)
            target = src
        moved.append(target)
    return moved


class Receiver:
    """Receive over the device: mDNS responder, listener, local OpenDrop server.

    on_ask(IncomingAsk) -> bool decides each transfer (None accepts all, as
    OpenDrop does); it runs on the server thread, which the sender waits on.
    on_event(kind, **info) gets "asked" (ask, accepted), "receiving" (ask,
    received, total) and "received" (ask, files, ok, raw, size, secs).

    OpenDrop unpacks into the current directory and replaces what is there.
    With dest_dir set, the current directory is only a staging area: each
    transfer's files then move to dest_dir under free names ('IMG_1.JPG',
    'IMG_1 (1).JPG'), so nothing the user has gets overwritten. Without it,
    files stay in the current directory (the command line's -d)."""

    def __init__(self, dev, name="EspDrop", flags=0x3fb, on_ask=None, on_event=None,
                 announce_interval=5.0, dest_dir=None):
        self.dev = dev
        env.ensure_identity(name)      # before OpenDrop would call openssl
        self.config = AirDropConfig(computer_name=name)
        self.config.flags = flags
        self.announce_interval = announce_interval
        self.dest_dir = dest_dir
        self.on_ask = on_ask
        self.on_event = on_event or (lambda kind, **info: None)
        self.httpd = None
        self.loop_port = None
        self.responder = None
        self.accepting = True          # False: refuse new AWDL connections
        self._last_ask = None
        self._splices = []

    # ---- the local OpenDrop server -----------------------------------------

    def _handler(self):
        rx = self

        class Handler(AirDropServerHandler):
            config = rx.config

            def _ask_user(self, post_data):
                return rx._decide(post_data)

            def handle_upload(self):
                rx._upload(self, super().handle_upload)

        return Handler

    def _start_server(self):
        self.httpd = _V4Server(("127.0.0.1", 0), self._handler())
        self.httpd.socket = self.config.get_ssl_context().wrap_socket(
            self.httpd.socket, server_side=True)
        self.loop_port = self.httpd.socket.getsockname()[1]
        threading.Thread(target=self.httpd.serve_forever, name="espdrop-httpd",
                        daemon=True).start()

    def _decide(self, post_data):
        ask = IncomingAsk(post_data)
        try:
            ok = True if self.on_ask is None else bool(self.on_ask(ask))
        except Exception as e:          # noqa: BLE001 - an unaskable question is a no
            logger.warning("asking about %s failed (%s): declined", ask.summary(), e)
            ok = False
        logger.info("%s %s", "accepted" if ok else "declined", ask.summary())
        if ok:
            self._last_ask = ask
        self.on_event("asked", ask=ask, accepted=ok)
        return ok

    def _upload(self, handler, base):
        ask = self._last_ask
        try:
            total = int(handler.headers.get("TotalBytes") or 0) or None
        except ValueError:
            total = None
        got = [0]
        last = [0.0]

        def on_bytes(n):
            got[0] += n
            now = time.monotonic()
            if now - last[0] >= 0.25:
                last[0] = now
                self.on_event("receiving", ask=ask, received=got[0], total=total)

        here = os.getcwd()
        before = set(os.listdir(here))
        start = time.monotonic()
        rfile = handler.rfile
        handler.rfile = _Counting(rfile, on_bytes)
        try:
            base()
        finally:
            handler.rfile = rfile
            new = sorted(set(os.listdir(here)) - before)
            raw_names = [n for n in new if n.startswith(RAW_PREFIX)]
            names = drop_appledouble(here, [n for n in new if not n.startswith(RAW_PREFIX)])
            dest = self.dest_dir
            if dest and os.path.realpath(dest) != os.path.realpath(here):
                files = move_out(here, names, dest)
                raw = move_out(here, raw_names, dest)
            else:
                files = [os.path.join(here, n) for n in names]
                raw = [os.path.join(here, n) for n in raw_names]
            ok = bool(files) and not raw and not any(f.endswith(".partial") for f in files)
            self.on_event("received", ask=ask, files=files, ok=ok,
                          raw=raw[0] if raw else None,
                          size=got[0], secs=time.monotonic() - start)

    # ---- one accepted AWDL connection --------------------------------------

    def _on_accept(self, conn):
        if not self.accepting:
            conn.abort()
            return
        try:
            local = socket.create_connection(("127.0.0.1", self.loop_port))
        except OSError as e:
            logger.warning("could not reach the local server (%s)", e)
            conn.abort()
            return
        self._splice(conn.app, local)
        self._splice(local, conn.app)

    def _splice(self, src, dst):
        def pump():
            try:
                while True:
                    data = src.recv(4096)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                for s in (src, dst):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                # The partner pump sees EOF from the shutdown and returns; then
                # both sockets can be closed (idempotent, so either pump may).
                for s in (src, dst):
                    try:
                        s.close()
                    except OSError:
                        pass
        t = threading.Thread(target=pump, daemon=True)
        t.start()
        self._splices.append(t)

    # ---- lifecycle ---------------------------------------------------------

    def start(self):
        self._start_server()
        mac = bytes(int(b, 16) for b in self.dev.hello_info["mac"].split(":"))
        instance = f"{mac.hex()}"
        self.responder = mdns.Responder(self.dev, instance, AIRDROP_PORT,
                                        eui64_linklocal(mac), flags=self.config.flags,
                                        interval=self.announce_interval,
                                        host=f"{instance}.local.")
        self.responder.start()
        self.dev.listen(AIRDROP_PORT, self._on_accept)
        logger.info("receiving as %r on the ESP's AWDL link (port %d)",
                    self.config.computer_name, AIRDROP_PORT)

    def stop(self):
        self.accepting = False
        if self.responder:
            self.responder.stop()
            self.responder = None
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
