# EspDrop - talking to the device over the USB link (docs/M4_ENTWURF.md, 4).
#
# A Device owns the serial port: a reader thread decodes the COBS stream and
# hands each message to whoever waits for it (a command's reply, the HELLO
# reply, a RESULT) or to a handler (UDP datagrams per port, device log lines).
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import threading

from . import link
from .proxy import Manager


class DeviceError(Exception):
    pass


class Device:
    def __init__(self, port, on_log=None):
        """port: a transport (transport.open_port) with read/write/close."""
        self.port = port
        self.on_log = on_log or (lambda text: None)
        self.hello_info = None
        self._wlock = threading.Lock()      # one writer at a time
        self._cmd_lock = threading.Lock()   # one command in flight (replies are unnumbered)
        self._cmd_reply = None
        self._cmd_event = threading.Event()
        self._hello_event = threading.Event()
        self._results = {}                  # (request type, id) -> err, for waiters
        self._result_cond = threading.Condition()
        self._udp = {}                      # port -> handler(src16, sport, multicast, data)
        self.tcp = Manager(self)            # proxied TCP connections
        self._stop = threading.Event()
        self.error = None                   # set if the reader thread died
        self._reader = threading.Thread(target=self._read_loop, name="espdrop-link",
                                        daemon=True)
        self._reader.start()
        # A lone delimiter ends whatever partial frame boot chatter left behind.
        self._write(b"\x00")

    # ---- plumbing ----------------------------------------------------------

    def _write(self, data: bytes) -> None:
        with self._wlock:
            self.port.write(data)

    def _read_loop(self):
        reader = link.Reader()
        try:
            while not self._stop.is_set():
                data = self.port.read(0.2)
                if data:
                    for t, p in reader.feed(data):
                        self._dispatch(t, p)
        except Exception as e:   # unplugged, port closed: wake every waiter
            self.error = e
            self._cmd_event.set()
            self._hello_event.set()
            with self._result_cond:
                self._result_cond.notify_all()

    def _dispatch(self, t, p):
        if t == link.MSG_CMD_REPLY:
            self._cmd_reply = p.decode("utf-8", "replace")
            self._cmd_event.set()
        elif t == link.MSG_LOG:
            self.on_log(p.decode("utf-8", "replace"))
        elif t == link.MSG_HELLO_REPLY:
            self.hello_info = link.parse_hello_reply(p)
            self._hello_event.set()
        elif t == link.MSG_UDP_RECV:
            u = link.parse_udp_recv(p)
            if u:
                handler = self._udp.get(u[0])
                if handler:
                    handler(*u[1:])
        elif t in (link.MSG_TCP_OPENED, link.MSG_TCP_DATA, link.MSG_TCP_CREDIT,
                   link.MSG_TCP_CLOSE):
            self.tcp.dispatch(t, p)
        elif t == link.MSG_RESULT:
            r = link.parse_result(p)
            if r:
                req, ident, err = r
                with self._result_cond:
                    self._results[(req, ident)] = err
                    self._result_cond.notify_all()
                if err and req == link.MSG_UDP_SEND:
                    self.on_log(f"udp send on port {ident} failed: {link.ERRORS.get(err, err)}")

    def _check(self):
        if self.error:
            raise DeviceError(f"link to the device lost: {self.error}")

    # ---- API ---------------------------------------------------------------

    def hello(self, mode=link.MODE_IP, credit=65536, timeout=2.0):
        """Start a session in the given mode; returns the HELLO_REPLY dict."""
        self._hello_event.clear()
        self._write(link.hello(mode, credit))
        if not self._hello_event.wait(timeout):
            raise DeviceError("the device does not answer HELLO; is the firmware older "
                              "than M4.1, or is tools/awdl_bridge.py holding the port?")
        self._check()
        h = self.hello_info
        if not h or h["version"] != link.PROTO_VERSION:
            raise DeviceError(f"device speaks protocol {h and h['version']}, "
                              f"this host app {link.PROTO_VERSION}: update one of them")
        if h["mode"] != mode:
            raise DeviceError("the device did not switch mode (IP interface failed?)")
        return h

    def cmd(self, text: str, timeout=3.0) -> str:
        """Run a text command (as typed in the bridge window); returns the reply."""
        with self._cmd_lock:
            self._cmd_event.clear()
            self._cmd_reply = None
            self._write(link.frame(link.MSG_CMD, text.encode()))
            if not self._cmd_event.wait(timeout):
                raise DeviceError(f"no reply to {text!r}")
            self._check()
            return self._cmd_reply

    def _wait_result(self, req, ident, timeout):
        with self._result_cond:
            ok = self._result_cond.wait_for(
                lambda: (req, ident) in self._results or self.error, timeout)
            self._check()
            if not ok:
                raise DeviceError(f"no answer to request {req} for {ident}")
            return self._results.pop((req, ident))

    def udp_open(self, port, handler, join_mdns=True, timeout=2.0):
        """Open a UDP port on the AWDL interface; handler(src16, sport, mcast, data)."""
        self._udp[port] = handler
        with self._result_cond:
            self._results.pop((link.MSG_UDP_OPEN, port), None)
        self._write(link.udp_open(port, join_mdns))
        err = self._wait_result(link.MSG_UDP_OPEN, port, timeout)
        if err:
            del self._udp[port]
            raise DeviceError(f"UDP port {port}: {link.ERRORS.get(err, err)}")

    def udp_send(self, port, dst16: bytes, dst_port, data: bytes):
        self._check()
        self._write(link.udp_send(port, dst16, dst_port, data))

    def udp_close(self, port):
        self._udp.pop(port, None)
        self._write(link.udp_close(port))

    def listen(self, port, on_accept):
        """Accept AWDL connections to `port`; on_accept(conn) per connection."""
        self.tcp.listen(port, on_accept)

    # ---- used by proxy.py --------------------------------------------------

    def _send(self, framed: bytes):
        """Write one already-framed message (proxy TCP_* traffic)."""
        self._write(framed)

    def _conn_gone(self, cid):
        self.tcp.forget(cid)

    def close(self):
        self.tcp.close_all()
        self._stop.set()
        self._reader.join(timeout=1.0)
        self.port.close()
