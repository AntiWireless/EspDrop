# EspDrop - drive the patched OpenDrop over the device's socket proxy.
#
# OpenDrop (0.13.0 + the airdrop-mt7921 patches) does all of AirDrop: TLS, the
# /Discover, /Ask and /Upload requests, the dvzip/cpio archive. We reuse it
# unchanged and only replace how its HTTPS client reaches the receiver: instead
# of a kernel socket on awdl0 it gets one end of a TCP pair whose other end the
# device bridges to the AWDL peer (proxy.py). So the whole send path runs on
# Windows and Linux with no awdl0, no root and no firewall rule.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import ipaddress
import logging
import os
import sys

try:
    from opendrop.client import AirDropClient
    from opendrop.config import AirDropConfig
except ImportError as e:   # pragma: no cover - depends on the environment
    raise ImportError("OpenDrop is missing; run espdrop with the venv built by "
                      "tools/opendrop_install.sh (~/opendrop-venv/bin/python)") from e

from .mdns import Browser

logger = logging.getLogger(__name__)


def _addr16(text: str) -> bytes:
    """A receiver's IPv6 string (zone stripped) to 16 bytes for the proxy."""
    return ipaddress.IPv6Address(text.split("%", 1)[0]).packed


def find_receiver(dev, timeout=30.0, want=None, on_packet=None):
    """Browse for an AirDrop receiver on the device's AWDL link.

    Returns the first complete receiver (or the one whose id/name matches
    `want`), or None. The caller sends the BLE wake."""
    import time
    hit = {}
    done = __import__("threading").Event()

    def found(r):
        if want and want.lower() not in (r.id.lower(), (r.server or "").lower()):
            return
        hit.setdefault("r", r)
        done.set()

    b = Browser(dev, on_found=found, on_packet=on_packet)
    b.start()
    try:
        done.wait(timeout)
    finally:
        b.stop()
    return hit.get("r") or (b.receivers()[0] if b.receivers() and not want else None)


def _bind_client(client, dev, peer16, name=""):
    """Make `client`'s HTTPS connections go through the device's proxy."""
    def factory(address, timeout=None, source_address=None):
        port = address[1]
        conn = dev.tcp.connect(peer16, port, name=name or f"airdrop:{port}")
        if not conn.opened.wait(timeout or 30):
            raise OSError(f"no connection to [{ipaddress.IPv6Address(peer16)}]:{port} "
                          f"(the device did not open it)")
        if conn.error:
            raise OSError(f"connection to port {port} failed: {conn.error}")
        return conn.app

    # AirDropClient builds its HTTPSConnectionAWDL lazily inside send_POST, so we
    # cannot reach the instance beforehand. Wrap the class __init__ to install
    # our factory on every connection it opens; http.client calls
    # self._create_connection((host, port), timeout, src) in connect(), and
    # HTTPSConnection then wraps the returned socket in TLS itself. Restored on
    # teardown so nothing else in the process is affected.
    from opendrop import client as odc
    base_init = odc.HTTPSConnectionAWDL.__init__

    def init(self, *a, **kw):
        base_init(self, *a, **kw)
        self._create_connection = factory

    odc.HTTPSConnectionAWDL.__init__ = init
    client._restore_conn_init = lambda: setattr(odc.HTTPSConnectionAWDL, "__init__", base_init)


def send_files(dev, receiver, files, name="EspDrop", timeout=60.0):
    """Send files to a Receiver through the device. Returns 0 on success."""
    peer16 = _addr16(receiver.parsed_addresses()[0])
    config = AirDropConfig(computer_name=name, interface="awdl0")
    client = AirDropClient(config, (str(ipaddress.IPv6Address(peer16)), int(receiver.port)))
    _bind_client(client, dev, peer16, name=name)
    try:
        logger.info("Asking the receiver to accept ...")
        if not client.send_ask(files):
            logger.warning("the receiver declined")
            return 1
        logger.info("accepted; uploading ...")
        if not client.send_upload(files, reask=lambda: client.send_ask(files)):
            logger.warning("the upload failed")
            return 1
        logger.info("sent")
        return 0
    finally:
        restore = getattr(client, "_restore_conn_init", None)
        if restore:
            restore()
        if client.http_conn is not None:
            try:
                client.http_conn.close()
            except OSError:
                pass
