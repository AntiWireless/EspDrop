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
import plistlib
import threading
import time

from . import env

env.load_opendrop()        # libarchive and the Windows fixes first; ImportError with advice
from opendrop import client as odc  # noqa: E402
from opendrop.client import AirDropClient  # noqa: E402
from opendrop.config import AirDropConfig  # noqa: E402

from .mdns import Browser  # noqa: E402

logger = logging.getLogger(__name__)


def _addr16(text: str) -> bytes:
    """A receiver's IPv6 string (zone stripped) to 16 bytes for the proxy."""
    return ipaddress.IPv6Address(text.split("%", 1)[0]).packed


def match_receiver(found, want):
    """The (receiver, info) entries of `found` that `want` names: the id or the
    name exactly (ignoring case), else the receivers whose name contains it."""
    w = want.lower()
    exact = [e for e in found if w in (e[0].id.lower(), (e[1].get("name") or "").lower())]
    return exact or [e for e in found if w in (e[1].get("name") or "").lower()]


def find_receivers(dev, timeout=30.0, want=None, settle=3.0, on_packet=None, name="EspDrop",
                   on_found=None):
    """Browse for AirDrop receivers on the device's AWDL link, asking each for
    its name (/Discover). Returns [(mdns Receiver, {"name", "model"} or {})].

    With `want` (an id or name): returns the matches as soon as there are any.
    Without: everything seen by `settle` seconds after the first receiver, so a
    second device nearby gets its chance. The caller sends the BLE wake.
    on_found(receiver, info) runs once a receiver's /Discover has answered."""
    found = {}                       # id -> (receiver, info or None while asking, since)
    ask_secs = 10.0                  # a /Discover still open after this does not hold us up
    lock = threading.Lock()
    changed = threading.Event()

    def ask_name(r):
        try:
            info = discover(dev, r, name=name)
        except Exception as e:       # noqa: BLE001 - keep it, by id
            logger.info("/Discover %s: %s", r.id, e)
            info = {}
        with lock:
            found[r.id] = (r, info, found[r.id][2])
        if on_found:
            on_found(r, info)
        changed.set()

    def on_mdns(r):
        with lock:
            if r.id in found:
                return
            found[r.id] = (r, None, time.monotonic())
        threading.Thread(target=ask_name, args=(r,), daemon=True).start()

    b = Browser(dev, on_found=on_mdns, on_packet=on_packet)
    b.start()
    end = time.monotonic() + timeout
    first = None
    try:
        # Short waits: a long one cannot be interrupted with Ctrl+C on Windows.
        while time.monotonic() < end:
            changed.wait(0.25)
            changed.clear()
            now = time.monotonic()
            with lock:
                done = [(r, i) for r, i, _ in found.values() if i is not None]
                pending = any(i is None and now - t < ask_secs for _, i, t in found.values())
            if want and match_receiver(done, want):
                return match_receiver(done, want)
            if done and first is None:
                first = time.monotonic()
            if not want and first is not None and not pending \
                    and time.monotonic() >= first + settle:
                break
    finally:
        b.stop()
    with lock:
        done = [(r, i or {}) for r, i, _ in found.values()]
    return match_receiver(done, want) if want else done


def find_receiver(dev, timeout=30.0, want=None, on_packet=None):
    """The first receiver found (or the one `want` names), or None."""
    hits = find_receivers(dev, timeout=timeout, want=want, settle=0.0, on_packet=on_packet)
    return hits[0][0] if hits else None


# ---- OpenDrop's HTTPS connections through the device -------------------------
#
# AirDropClient builds its HTTPSConnectionAWDL lazily inside send_POST (and
# again after a failed request), so there is no instance to configure
# beforehand. Its __init__ is wrapped once, for good: a connection to an address
# a device was registered for (_route) gets our connection factory; http.client
# calls self._create_connection((host, port), timeout, src) in connect() and
# then wraps the returned socket in TLS itself. Any other address is left to
# OpenDrop. Several sends and /Discovers can run at once (the registry, not a
# swapped class attribute, decides), and each thread's connections are
# collected in its SendJob for progress and cancelling.

_routes = {}                      # peer16 -> device
_tls = threading.local()          # .job: the SendJob of the calling thread, if any
_installed = False
_install_lock = threading.Lock()


def _connect_via(dev, peer16, address, timeout=None, source_address=None):
    port = address[1]
    conn = dev.tcp.connect(peer16, port, name=f"airdrop:{port}")
    job = getattr(_tls, "job", None)
    if job is not None:
        job.conns.append(conn)
        if job.cancelled:
            conn.abort()
    if not conn.opened.wait(timeout or 15):
        conn.abort()
        raise OSError(f"no connection to [{ipaddress.IPv6Address(peer16)}]:{port} "
                      f"(the device did not open it)")
    if conn.error:
        raise OSError(f"connection to port {port} failed: {conn.error}")
    return conn.app


def _install():
    global _installed
    with _install_lock:
        if _installed:
            return
        base_init = odc.HTTPSConnectionAWDL.__init__

        def init(self, *a, **kw):
            base_init(self, *a, **kw)
            try:
                peer16 = _addr16(self.host)
            except ValueError:
                return
            dev = _routes.get(peer16)
            if dev is None:
                return                                   # not one of ours
            self._create_connection = (
                lambda address, timeout=None, source_address=None:
                _connect_via(dev, peer16, address, timeout, source_address))
            connect = self.connect

            def timed_connect():
                t = time.monotonic()
                connect()
                logger.info("connected (TCP + TLS) in %.1f s", time.monotonic() - t)
            self.connect = timed_connect                 # http.client calls self.connect()

        odc.HTTPSConnectionAWDL.__init__ = init
        _installed = True


def _client(dev, receiver, name):
    """An AirDropClient for `receiver` whose connections go through `dev`."""
    _install()
    peer16 = _addr16(receiver.parsed_addresses()[0])
    _routes[peer16] = dev
    env.ensure_identity(name)          # before OpenDrop would call openssl
    config = AirDropConfig(computer_name=name, interface="awdl0")
    return AirDropClient(config, (str(ipaddress.IPv6Address(peer16)), int(receiver.port)))


def _close(client):
    if client.http_conn is not None:
        try:
            client.http_conn.close()
        except OSError:
            pass


def discover(dev, receiver, name="EspDrop"):
    """/Discover a receiver: {"name": ..., "model": ...} as it introduces itself,
    {} if it does not answer with a name (not discoverable to us)."""
    client = _client(dev, receiver, name)
    try:
        body = {}
        if client.config.record_data:
            body["SenderRecordData"] = client.config.record_data
        ok, resp = client.send_POST("/Discover", plistlib.dumps(body, fmt=plistlib.FMT_BINARY))
        answer = plistlib.loads(resp) if ok and resp else {}
    finally:
        _close(client)
    if not answer.get("ReceiverComputerName"):
        return {}
    return {"name": answer["ReceiverComputerName"], "model": answer.get("ReceiverModelName")}


class SendJob:
    """A send in progress: its connections, progress and a way to cancel it."""

    def __init__(self, files):
        self.files = list(files)
        self.size = sum(os.path.getsize(f) for f in self.files)
        self.conns = []
        self.state = "new"          # asking, uploading, done, declined, failed, cancelled
        self.error = None
        self.cancelled = False
        self._mark = 0               # bytes up when the upload began
        self.payload = None          # bytes of the /Upload body (the archive), once known

    def bytes_up(self):
        return sum(c.bytes_up for c in self.conns)

    def sent(self):
        """Bytes of the upload handed to the device so far."""
        return max(0, self.bytes_up() - self._mark) if self.state in ("uploading", "done") else 0

    def progress(self):
        """0..1 of the upload. Measured against the archive OpenDrop sends,
        which is compressed and so can be much smaller than the files."""
        if self.state == "done":
            return 1.0
        total = self.payload or self.size
        return min(1.0, self.sent() / total) if total else 0.0

    def cancel(self):
        self.cancelled = True
        for c in list(self.conns):
            c.abort()


def send_files(dev, receiver, files, name="EspDrop", job=None, on_state=None):
    """Send files to a Receiver through the device. Returns 0 on success.

    job: a SendJob to track (one is made if None); on_state(job) is called on
    every state change."""
    job = job or SendJob(files)
    notify = on_state or (lambda j: None)

    def state(s, error=None):
        job.state, job.error = s, error
        notify(job)

    client = _client(dev, receiver, name)
    post = client.send_POST

    def send_post(url, body, *a, **kw):
        # The archive's size, and the byte count where the upload starts (a
        # re-Ask may come before it), for progress.
        if url == "/Upload":
            job.payload = len(body) if isinstance(body, (bytes, bytearray)) else None
            job._mark = job.bytes_up()
        return post(url, body, *a, **kw)
    client.send_POST = send_post
    _tls.job = job
    try:
        t = time.monotonic()
        state("asking")
        logger.info("asking the receiver (tap Accept on the device) ...")
        if not client.send_ask(job.files):
            state("cancelled" if job.cancelled else "declined")
            logger.warning("the receiver declined")
            return 1
        logger.info("accepted after %.1f s; uploading %.2f MB ...", time.monotonic() - t,
                    job.size / 1e6)
        job._mark = job.bytes_up()          # until /Upload sets it exactly
        state("uploading")
        t = time.monotonic()
        if not client.send_upload(job.files, reask=lambda: client.send_ask(job.files)):
            state("cancelled" if job.cancelled else "failed", "the upload failed")
            logger.warning("the upload failed")
            return 1
        secs = time.monotonic() - t
        logger.info("uploaded %.2f MB in %.1f s (%.2f MB/s)", job.size / 1e6, secs,
                    job.size / 1e6 / max(secs, 1e-3))
        state("done")
        return 0
    except Exception as e:
        state("cancelled" if job.cancelled else "failed", str(e))
        raise
    finally:
        _tls.job = None
        _close(client)
