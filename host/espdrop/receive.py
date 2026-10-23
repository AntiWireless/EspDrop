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
# connection itself); we only move bytes, we do not re-implement TLS.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import logging
import os
import socket
import threading
from http.server import ThreadingHTTPServer

try:
    from opendrop.config import AirDropConfig
    from opendrop.server import AirDropServerHandler
except ImportError as e:   # pragma: no cover
    raise ImportError("OpenDrop is missing; run espdrop with the venv built by "
                      "tools/opendrop_install.sh") from e

from . import mdns

logger = logging.getLogger(__name__)
AIRDROP_PORT = 8771


class _V4Server(ThreadingHTTPServer):
    address_family = socket.AF_INET
    daemon_threads = True
    allow_reuse_address = True


def eui64_linklocal(mac6: bytes) -> bytes:
    """The EUI-64 link-local the device uses on AWDL (matches awdl_netif.c)."""
    m = bytearray(mac6)
    iid = bytes([m[0] ^ 0x02, m[1], m[2], 0xff, 0xfe, m[3], m[4], m[5]])
    return b"\xfe\x80\x00\x00\x00\x00\x00\x00" + iid


class Receiver:
    def __init__(self, dev, name="EspDrop", flags=0x3fb):
        self.dev = dev
        self.config = AirDropConfig(computer_name=name)
        self.config.flags = flags
        self.httpd = None
        self.loop_port = None
        self.responder = None
        self._splices = []

    # ---- the local OpenDrop server -----------------------------------------

    def _start_server(self):
        handler = AirDropServerHandler
        handler.config = self.config
        self.httpd = _V4Server(("127.0.0.1", 0), handler)
        self.httpd.socket = self.config.get_ssl_context().wrap_socket(
            self.httpd.socket, server_side=True)
        self.loop_port = self.httpd.socket.getsockname()[1]
        threading.Thread(target=self.httpd.serve_forever, name="espdrop-httpd",
                        daemon=True).start()

    # ---- one accepted AWDL connection --------------------------------------

    def _on_accept(self, conn):
        try:
            local = socket.create_connection(("127.0.0.1", self.loop_port))
        except OSError as e:
            logger.warning("could not reach the local server (%s)", e)
            conn.app.close()
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
                                        host=f"{instance}.local.")
        self.responder.start()
        self.dev.listen(AIRDROP_PORT, self._on_accept)
        logger.info("receiving as %r on the ESP's AWDL link (port %d); Ctrl+C to stop",
                    self.config.computer_name, AIRDROP_PORT)

    def stop(self):
        if self.responder:
            self.responder.stop()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
