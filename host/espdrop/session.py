# EspDrop - one long-running link to the device, for the GUI (and any UI).
#
# The CLI opens the port for each command; a GUI needs one owner of the port
# that stays: receiving is ready the whole time, sends run alongside, and when
# the ESP is unplugged the session waits and connects again. Session wraps the
# pieces the CLI uses (Device, Receiver, Browser, airdrop.send_files) in that
# shape and reports everything as events:
#
#   on_event(kind, **info), from worker threads (a GUI hands it to its own):
#     "device"    state="connecting"|"connected"|"lost"|"stopped", info, error
#     "power"     text=<the device's `power` reply>
#     "browse"    on=True|False
#     "receiving" on=True|False, until=<time.monotonic() deadline or None>
#     "receiver"  action="found"|"updated"|"gone", receiver=ReceiverInfo
#     "send"      transfer=Transfer, state, progress (0..1), error
#     "receive"   ask, phase="asked"|"receiving"|"received", plus Receiver's info
#     "log"       text
#   on_ask(IncomingAsk) -> bool, on the server thread while the sender waits;
#     None accepts everything. Unanswered after ask_timeout: declined.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import itertools
import logging
import os
import threading
import time

from . import link
from .device import Device, DeviceError

logger = logging.getLogger(__name__)

RECONNECT_SECS = 2.0
KEEPALIVE_SECS = 30.0
BROWSE_SECS = 60.0
HOLD_SECS = 90             # `power hold` per keepalive: the radio stays on while needed


class ReceiverInfo:
    """An AirDrop receiver seen by the browse, with its name once /Discover told it."""

    def __init__(self, mdns_receiver):
        self.mdns = mdns_receiver
        self.id = mdns_receiver.id
        self.name = None               # ReceiverComputerName from /Discover
        self.model = None
        self.discovered = False        # /Discover answered with a name
        self.present = True            # False after a goodbye or a new browse

    @property
    def label(self):
        return self.name or self.id

    def __repr__(self):
        return f"<ReceiverInfo {self.id} {self.name!r}>"


class Transfer:
    """One send, as the UI sees it."""
    _ids = itertools.count(1)

    def __init__(self, receiver, files, job):
        self.id = next(self._ids)
        self.receiver = receiver
        self.files = list(files)
        self.job = job
        self.started = time.monotonic()

    @property
    def state(self):
        return self.job.state

    @property
    def error(self):
        return self.job.error

    @property
    def size(self):
        return self.job.size

    def sent(self):
        return self.job.sent()

    def progress(self):
        return self.job.progress()


class Session:
    def __init__(self, port=None, name="EspDrop", receive=True, download_dir=None,
                 on_event=None, on_ask=None, ask_timeout=60.0, transport_factory=None):
        self.port_name = port
        self.name = name
        self.download_dir = download_dir
        self.on_event = on_event or (lambda kind, **info: None)
        self.on_ask = on_ask
        self.ask_timeout = ask_timeout
        self._factory = transport_factory     # tests: a callable returning a transport
        self._receive = receive
        self._receive_until = None            # end of a receive window (monotonic)
        self._held = None                     # last hold request sent: True hold, False release
        self._listening = None                # last listen request sent
        self._scan_ok = False                 # the ESP can wake itself on AirDrop adverts
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self.dev = None
        self.state = "stopped"
        self._receiver = None
        self._browser = None
        self._browse_until = 0.0
        self._receivers = {}                  # id -> ReceiverInfo
        self._transfers = {}                  # id -> Transfer
        self._wake_until = 0.0

    # ---- public API --------------------------------------------------------

    def start(self):
        if self._thread:
            return
        if self.download_dir:
            os.makedirs(self.download_dir, exist_ok=True)
            os.chdir(self.download_dir)       # OpenDrop's server writes into cwd
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="espdrop-session", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None

    @property
    def receiving(self):
        return self._receive

    def set_receiving(self, on, for_secs=None):
        """Receive (others see us and can send) or not. for_secs: a window that
        closes by itself, like Apple's "Everyone for 10 minutes"; the ESP's
        radio is only on while receiving, browsing or sending."""
        with self._lock:
            self._receive = bool(on)
            self._receive_until = time.monotonic() + for_secs if on and for_secs else None
            if self.state == "connected":
                if on and not self._receiver:
                    self._start_receiver()
                elif not on and self._receiver:
                    self._stop_receiver()
        self._emit("receiving", on=self._receive, until=self._receive_until)
        self._power()

    @property
    def receive_until(self):
        return self._receive_until

    def set_name(self, name):
        """The name others see; restarts receiving if it runs."""
        with self._lock:
            self.name = name
            if self._receiver:
                self._stop_receiver()
                self._start_receiver()

    def set_download_dir(self, path):
        """Where received files go (OpenDrop's server writes into the current
        directory, so this changes it for the whole process)."""
        os.makedirs(path, exist_ok=True)
        os.chdir(path)
        self.download_dir = path

    def receivers(self):
        with self._lock:
            return list(self._receivers.values())

    def browse(self, seconds=BROWSE_SECS):
        """Look for receivers for `seconds` (BLE wake from the ESP meanwhile)."""
        with self._lock:
            if self.state != "connected":
                raise DeviceError("no device connected")
            self._browse_until = time.monotonic() + seconds
            if self._browser:
                self._wake(seconds)
                return
            for r in self._receivers.values():
                r.present = False
            from .mdns import Browser
            self._browser = Browser(self.dev, on_found=self._found, on_gone=self._gone)
            self._wake(seconds)
        self._power()
        with self._lock:
            if self._browser:
                self._browser.start()
        self._emit("browse", on=True)

    def stop_browse(self):
        with self._lock:
            b, self._browser = self._browser, None
            self._browse_until = 0.0
        if b:
            b.stop()
            self._emit("browse", on=False)
            self._unwake()
            self._power()

    @property
    def browsing(self):
        return self._browser is not None

    def send(self, receiver_id, files):
        """Start sending files to a receiver from the list; returns a Transfer."""
        from .airdrop import SendJob
        files = [os.path.abspath(f) for f in files]
        for f in files:
            if not os.path.isfile(f):
                raise FileNotFoundError(f)
        with self._lock:
            r = self._receivers.get(receiver_id)
            if r is None:
                raise KeyError(f"no receiver {receiver_id!r}")
            if self.state != "connected":
                raise DeviceError("no device connected")
            t = Transfer(r, files, SendJob(files))
            self._transfers[t.id] = t
            dev = self.dev
        self._wake(180)                         # also covers Accept and the upload
        self._power()
        threading.Thread(target=self._send, args=(dev, t), name=f"espdrop-send{t.id}",
                         daemon=True).start()
        return t

    def cancel(self, transfer_id):
        t = self._transfers.get(transfer_id)
        if t:
            t.job.cancel()

    def transfers(self):
        return list(self._transfers.values())

    def command(self, text, timeout=3.0):
        """A device command (as in the bridge); returns its reply."""
        dev = self.dev
        if not dev or self.state != "connected":
            raise DeviceError("no device connected")
        return dev.cmd(text, timeout=timeout)

    # ---- the supervisor ------------------------------------------------------

    def _emit(self, kind, **info):
        try:
            self.on_event(kind, **info)
        except Exception:                      # noqa: BLE001 - a UI bug must not kill us
            logger.exception("event handler failed for %s", kind)

    def _set_state(self, state, **info):
        self.state = state
        self._emit("device", state=state, **info)

    def _open(self):
        if self._factory:
            return self._factory()
        from .transport import open_port
        return open_port(self.port_name)

    def _run(self):
        while not self._stop.is_set():
            self._set_state("connecting")
            try:
                dev = Device(self._open(), on_log=lambda text: self._emit("log", text=text))
                try:
                    info = dev.hello(link.MODE_IP)
                except Exception:
                    dev.close()
                    raise
            except Exception as e:             # noqa: BLE001 - not there yet: try again
                self._set_state("lost", error=str(e))
                self._stop.wait(RECONNECT_SECS)
                continue
            with self._lock:
                self.dev = dev
                self._scan_ok = bool(info.get("caps", 0) & link.CAP_SCAN)
                self._set_state("connected", info=info)
                if self._receive:
                    try:
                        self._start_receiver()
                    except Exception as e:     # noqa: BLE001
                        self._emit("log", text=f"receiving did not start: {e}")
            self._held = self._listening = None    # re-send on the new device
            self._power()
            self._poll_power()
            self._serve(dev)
            with self._lock:
                self._teardown(dev)
            if not self._stop.is_set():
                self._set_state("lost", error=str(dev.error or "closed"))
                self._stop.wait(RECONNECT_SECS)
        self._set_state("stopped")

    def _serve(self, dev):
        next_keepalive = time.monotonic() + KEEPALIVE_SECS
        while not self._stop.wait(0.25):
            if dev.error:
                return
            now = time.monotonic()
            if self._browser and now >= self._browse_until:
                self.stop_browse()
            if self._receive_until and now >= self._receive_until:
                self.set_receiving(False)               # the receive window closed
            for t in list(self._transfers.values()):
                if t.state == "uploading":
                    self._emit_send(t)                  # progress, 4 times a second
            if now >= next_keepalive:
                next_keepalive = now + KEEPALIVE_SECS
                self._held = None                       # renew the hold
                self._power()
                self._poll_power()

    def _poll_power(self):
        """The device's power state; also tells it the host is still there."""
        try:
            reply = self.dev.cmd("power")
        except Exception:                      # noqa: BLE001 - lost link
            return
        if not reply.startswith("unknown"):    # firmware without a sleep mode
            self._emit("power", text=reply)

    def _teardown(self, dev):
        b, self._browser = self._browser, None
        if b:
            b.stop()
            self._emit("browse", on=False)
        if self._receiver:
            self._stop_receiver()
        for t in self._transfers.values():
            if t.state in ("new", "asking", "uploading"):
                t.job.cancel()
        try:
            dev.close()
        except Exception:                      # noqa: BLE001
            pass
        self.dev = None

    # ---- receiving -----------------------------------------------------------

    def _start_receiver(self):
        from .receive import Receiver
        # Announce a little faster than OpenDrop's 5 s: when the radio wakes on
        # an AirDrop advert, the phone should discover us within its share-sheet
        # window (it also answers the phone's own queries once awake).
        self._receiver = Receiver(self.dev, name=self.name, on_ask=self._ask,
                                  on_event=self._on_receive, announce_interval=2.5)
        self._receiver.start()

    def _stop_receiver(self):
        rx, self._receiver = self._receiver, None
        if rx:
            rx.stop()

    def _ask(self, ask):
        if self.on_ask is None:
            return True
        result = {}
        done = threading.Event()

        def run():
            try:
                result["ok"] = bool(self.on_ask(ask))
            finally:
                done.set()
        threading.Thread(target=run, name="espdrop-ask", daemon=True).start()
        if not done.wait(self.ask_timeout):
            self._emit("log", text=f"no answer within {self.ask_timeout:.0f} s: declined "
                                   f"{ask.summary()}")
            return False
        return result.get("ok", False)

    def _on_receive(self, phase, **info):
        self._emit("receive", phase=phase, **info)

    # ---- browsing and sending ------------------------------------------------

    def _found(self, r):
        with self._lock:
            info = self._receivers.get(r.id)
            new = info is None
            if new:
                info = self._receivers[r.id] = ReceiverInfo(r)
            info.mdns, info.present = r, True
            dev = self.dev
        self._emit("receiver", action="found" if new else "updated", receiver=info)
        if not info.discovered and dev:
            threading.Thread(target=self._discover, args=(dev, info), daemon=True).start()

    def _gone(self, r):
        with self._lock:
            info = self._receivers.get(r.id)
            if info:
                info.present = False
        if info:
            self._emit("receiver", action="gone", receiver=info)

    def _discover(self, dev, info):
        from .airdrop import discover
        try:
            d = discover(dev, info.mdns, name=self.name)
        except Exception as e:                 # noqa: BLE001 - the list keeps the id
            self._emit("log", text=f"/Discover {info.id}: {e}")
            return
        if d:
            info.name, info.model, info.discovered = d["name"], d.get("model"), True
            self._emit("receiver", action="updated", receiver=info)

    def _send(self, dev, t):
        from .airdrop import send_files
        try:
            send_files(dev, t.receiver.mdns, t.files, name=self.name, job=t.job,
                       on_state=lambda job: self._emit_send(t))
        except Exception as e:                 # noqa: BLE001 - state says failed
            if t.job.state not in ("failed", "cancelled"):
                t.job.state, t.job.error = "failed", str(e)
                self._emit_send(t)
        finally:
            self._unwake()
            self._power()

    def _emit_send(self, t):
        # state and progress as of now: the Transfer itself keeps changing
        self._emit("send", transfer=t, state=t.state, progress=t.progress(), error=t.error)

    def _wake(self, seconds):
        """BLE wake from the ESP for at least `seconds` (sends and browsing)."""
        dev = self.dev
        if not dev or not (dev.hello_info or {}).get("caps", 0) & link.CAP_BLE:
            return
        until = time.monotonic() + seconds
        if until <= self._wake_until:
            return
        self._wake_until = until
        try:
            dev.cmd(f"wake {int(min(600, seconds))}")
        except Exception as e:                 # noqa: BLE001
            self._emit("log", text=f"wake: {e}")

    def _sending(self):
        return any(t.state in ("new", "asking", "uploading") for t in self._transfers.values())

    def _hold_needed(self):
        # The radio stays fully on while browsing or sending. For receiving it
        # only needs a hold on firmware that cannot wake itself on AirDrop
        # adverts; otherwise receiving uses listen (the radio sleeps between).
        return bool(self._browser or self._sending()
                    or (self._receive and not self._scan_ok))

    def _power(self):
        """Match the ESP's radio to what we are doing: a hold keeps it on
        (browse, send, or receive on firmware without BLE listen); listen lets
        it sleep and wake on an AirDrop advert (receive). Firmware without a
        sleep mode ignores both: its radio is always on."""
        dev = self.dev
        if not dev or self.state != "connected":
            return
        hold = self._hold_needed()
        if hold != self._held:
            self._held = hold
            self._power_cmd(dev, f"power hold {HOLD_SECS}" if hold else "power release")
        if self._scan_ok:
            listen = self._receive
            if listen != self._listening:
                self._listening = listen
                self._power_cmd(dev, f"power listen {'on' if listen else 'off'}")

    def _power_cmd(self, dev, text):
        try:
            dev.cmd(text)
        except Exception as e:                 # noqa: BLE001
            self._emit("log", text=f"{text}: {e}")

    def _unwake(self):
        """End the BLE wake once neither a browse nor a send needs it."""
        with self._lock:
            busy = self._browser or any(t.state in ("new", "asking", "uploading")
                                        for t in self._transfers.values())
            dev = self.dev
        if busy or not dev:
            return
        self._wake_until = 0.0
        try:
            dev.cmd("wake off")
        except Exception:                      # noqa: BLE001
            pass
