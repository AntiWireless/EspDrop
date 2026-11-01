# EspDrop tests - a patched-OpenDrop AirDrop receiver on IPv4 loopback.
#
# The real AirDropServer binds AF_INET6 to an interface; this container has no
# IPv6. So we run OpenDrop's real request handler (TLS, /Discover, /Ask,
# /Upload, dvzip/cpio - every patch exercised) on an AF_INET ThreadingHTTPServer
# bound to 127.0.0.1, which is all the TCP-bridging fake device needs.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import socket
import threading
from http.server import ThreadingHTTPServer

from opendrop.config import AirDropConfig
from opendrop.server import AirDropServerHandler


class _V4Server(ThreadingHTTPServer):
    address_family = socket.AF_INET
    daemon_threads = True


class LoopbackReceiver:
    """Start/stop a loopback AirDrop receiver. `port` is valid after start()."""

    def __init__(self, name="TestPad"):
        # OpenDrop's handler extracts into os.getcwd(); the test chdirs there.
        self.config = AirDropConfig(computer_name=name, interface="lo")
        self.httpd = None
        self.port = None
        self._thread = None

    def start(self):
        handler = AirDropServerHandler
        handler.config = self.config
        self.httpd = _V4Server(("127.0.0.1", 0), handler)
        self.httpd.socket = self.config.get_ssl_context().wrap_socket(
            self.httpd.socket, server_side=True)
        self.port = self.httpd.socket.getsockname()[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
