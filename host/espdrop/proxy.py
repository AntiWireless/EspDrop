# EspDrop - TCP connections through the device as ordinary local sockets.
#
# A TcpConn is one connection the device holds to an AWDL peer. The application
# gets `app`, a normal TCP socket on 127.0.0.1 (a TCP pair rather than a Unix
# socketpair: http.client sets TCP_NODELAY and fails on anything else). Two
# threads move the bytes between the other end and the device, with the
# credit-based flow control of docs/M4_ENTWURF.md section 4.3:
#  - uplink (app -> peer): never more outstanding than the device granted
#    (TCP_OPENED credit, then TCP_CREDIT for every byte the peer ACKed);
#  - downlink (peer -> app): every byte written to the local socket is granted
#    back to the device (TCP_CREDIT), so a slow application slows the peer.
# FIN travels both ways as a half-close; RST/errors close the local socket.
#
# A Manager owns the connection table: it routes the device's TCP messages to
# the right TcpConn and hands out host-side connection ids (0x0001..0x7fff).
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import queue
import socket
import threading

from . import link

_FIN = object()
_RST = object()


def tcp_pair():
    """Two connected TCP sockets on the loopback interface."""
    lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lst.bind(("127.0.0.1", 0))
        lst.listen(1)
        a = socket.create_connection(lst.getsockname())
        b, _ = lst.accept()
    finally:
        lst.close()
    for s in (a, b):
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return a, b


class TcpConn:
    """One proxied connection. `app` is the local socket the caller uses; the
    class drives the other end of the pair against the device."""

    def __init__(self, dev, conn_id, max_chunk=1400, name=""):
        self.dev = dev
        self.id = conn_id
        self.name = name or f"tcp{conn_id}"
        self.max_chunk = max_chunk
        self.app, self._own = tcp_pair()
        self.opened = threading.Event()
        self.closed = threading.Event()
        self.error = None            # text, when the connection failed or was reset
        self.peer = None             # (addr16, port) once opened
        self._credit = 0             # bytes we may still send the device (uplink)
        self._cv = threading.Condition()
        self._down = queue.Queue()   # payloads / _FIN / _RST from the device
        self._dead = False
        self.bytes_up = self.bytes_down = 0

    # ---- device events (reader thread; must not block) ---------------------

    def on_opened(self, peer16, rport, credit):
        self.peer = (peer16, rport)
        with self._cv:
            self._credit = credit
        threading.Thread(target=self._uplink, name=f"{self.name}-up", daemon=True).start()
        threading.Thread(target=self._downlink, name=f"{self.name}-down", daemon=True).start()
        self.opened.set()

    def on_data(self, data):
        self._down.put(data)

    def on_credit(self, n):
        with self._cv:
            self._credit += n
            self._cv.notify_all()

    def on_close(self, kind, err):
        if kind == link.TCP_FIN:
            self._down.put(_FIN)
            return
        # RST or an lwIP error on the device side.
        self.error = self.error or f"reset (err {err}: {link.ERRORS.get(err, 'lwIP')})"
        with self._cv:
            self._dead = True
            self._cv.notify_all()
        if self.opened.is_set():
            self._down.put(_RST)
        else:
            self.opened.set()          # a failed connect: wake the waiter
            self._finish()

    # ---- pumps -------------------------------------------------------------

    def _uplink(self):
        """app -> device, respecting the credit the device granted."""
        try:
            while True:
                with self._cv:
                    self._cv.wait_for(lambda: self._credit > 0 or self._dead)
                    if self._dead:
                        return
                    want = min(self._credit, self.max_chunk)
                try:
                    data = self._own.recv(want)
                except OSError:
                    data = b""
                if not data:
                    break                       # the application closed its send side
                with self._cv:
                    self._credit -= len(data)
                self.dev._send(link.tcp_data(self.id, data))
                self.bytes_up += len(data)
        finally:
            if not self._dead:
                self.dev._send(link.tcp_close(self.id, link.TCP_FIN))

    def _downlink(self):
        """device -> app, granting credit back for whatever the app took."""
        try:
            while True:
                item = self._down.get()
                if item is _FIN:
                    try:
                        self._own.shutdown(socket.SHUT_WR)   # EOF to the application
                    except OSError:
                        pass
                    return
                if item is _RST:
                    return
                try:
                    self._own.sendall(item)
                except OSError:
                    return
                self.bytes_down += len(item)
                self.dev._send(link.tcp_credit(self.id, len(item)))
        finally:
            self._finish()

    def _finish(self):
        """Close the local sockets once; wake anyone waiting on the connection."""
        if self.closed.is_set():
            return
        self.closed.set()
        for s in (self._own, self.app):
            try:
                s.close()
            except OSError:
                pass
        self.dev._conn_gone(self.id)


class Manager:
    """Routes the device's TCP messages to connections and allocates ids."""

    def __init__(self, dev):
        self.dev = dev
        self._conns = {}
        self._lock = threading.Lock()
        self._next = 1               # host ids: 0x0001..0x7fff (device uses 0x8000+)
        self._on_accept = None       # called with each accepted TcpConn

    def connect(self, peer16: bytes, port: int, name="") -> TcpConn:
        """Open a connection to [peer]:port through the device; returns at once.
        Wait on conn.opened, then check conn.error."""
        with self._lock:
            cid = self._next
            self._next = self._next + 1 if self._next < 0x7fff else 1
            chunk = (self.dev.hello_info or {}).get("max_chunk", 1400)
            conn = TcpConn(self.dev, cid, max_chunk=chunk, name=name)
            self._conns[cid] = conn
        self.dev._send(link.tcp_connect(cid, peer16, port))
        return conn

    def listen(self, port, on_accept):
        """Ask the device to accept connections on `port`; on_accept(conn) runs
        for each (the peer opened it; conn.app is ready)."""
        self._on_accept = on_accept
        self.dev._send(link.tcp_listen(port))

    def forget(self, cid):
        with self._lock:
            self._conns.pop(cid, None)

    def close_all(self):
        with self._lock:
            conns = list(self._conns.values())
        for c in conns:
            if not c._dead:
                try:
                    self.dev._send(link.tcp_close(c.id, link.TCP_RST))
                except Exception:
                    pass
            c._finish()

    # ---- called by Device's reader thread ----------------------------------

    def dispatch(self, msg_type, payload):
        if msg_type == link.MSG_TCP_OPENED:
            o = link.parse_tcp_opened(payload)
            if o:
                cid, peer16, rport, _lport, accepted, credit = o
                c = self._conns.get(cid)
                if c is None and accepted:
                    # The device allocated this id (0x8000+) for a peer that
                    # connected to our listener.
                    chunk = (self.dev.hello_info or {}).get("max_chunk", 1400)
                    c = TcpConn(self.dev, cid, max_chunk=chunk, name=f"accept{cid:#x}")
                    with self._lock:
                        self._conns[cid] = c
                    c.on_opened(peer16, rport, credit)
                    if self._on_accept:
                        self._on_accept(c)
                elif c:
                    c.on_opened(peer16, rport, credit)
        elif msg_type == link.MSG_TCP_DATA:
            if len(payload) >= 2:
                c = self._conns.get(int.from_bytes(payload[:2], "little"))
                if c:
                    c.on_data(payload[2:])
        elif msg_type == link.MSG_TCP_CREDIT:
            if len(payload) >= 6:
                c = self._conns.get(int.from_bytes(payload[:2], "little"))
                if c:
                    c.on_credit(int.from_bytes(payload[2:6], "little"))
        elif msg_type == link.MSG_TCP_CLOSE:
            cc = link.parse_tcp_close(payload)
            if cc:
                cid, kind, err = cc
                c = self._conns.get(cid)
                if c:
                    c.on_close(kind, err)
