#!/usr/bin/env python3
# EspDrop - wake a nearby iPhone's AirDrop receiver with a Bluetooth LE advert.
#
# To SEND a file to an iPhone (PC -> iPhone) the phone must answer for its
# AirDrop service (_airdrop._tcp) over AWDL. It does that only while a nearby
# sender's Bluetooth LE "Continuity" advertisement says someone wants to send
# (airdrop-mt7921 FINDINGS §27: found only while the advert ran; the phone's
# own share sheet need not be open, as later tests with the ESP showed).
# This tool sends that advert from the PC's own Bluetooth, so the iPhone then
# appears to `opendrop find` on awdl0 and accepts an upload
# (tools/opendrop_send.sh).
#
# This is the receiver's side of Apple's own bootstrap: per Stute et al.
# (USENIX Security 2019) a receiver wakes its AWDL interface "if at least one
# contact match was found in contacts-only mode, or if it is discoverable by
# everyone". So with the target set to AirDrop -> Everyone the hash fields may
# be empty; for contacts-only, pass an identifier already in the target's
# address book (--appleid / --phone / --email) and it is hashed as Apple does.
#
# Wire format (furiousMAC/continuity, manufacturer 0x004C = Apple, type 0x05):
#   05 12  <8 zero bytes>  01  AA AA  PP PP  EE EE  EE EE  00
#   type/len, zero prefix, AirDrop version 1, then the first 2 bytes of
#   SHA-256(apple id), (phone), (email), (email2), zero suffix.
#
# It goes out through BlueZ's LEAdvertisingManager1 D-Bus API, so bluetoothd
# (which owns the controller on a normal desktop) enables it itself; raw HCI or
# btmgmt fight bluetoothd for the controller. Needs python3-dbus and PyGObject
# (Debian/Ubuntu: apt install python3-dbus python3-gi; Fedora: python3-dbus
# python3-gobject) and an adapter that is powered (`bluetoothctl power on`).
#
#   tools/airdrop_wake.py            # Everyone mode (empty hashes)
#   tools/airdrop_wake.py --appleid you@icloud.com   # contacts-only target
#
# Leave it running in its own terminal while opendrop_send.sh runs, then stop
# it with Ctrl-C (it unregisters the advert cleanly).
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import argparse
import hashlib
import signal
import sys

try:
    import dbus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib
except ImportError as e:
    sys.exit("error: missing %s; install python3-dbus and python3-gi "
             "(Fedora: python3-dbus python3-gobject)" % e.name)

APPLE_CID = 0x004C                       # Apple's Bluetooth company identifier
ADAPTER = "/org/bluez/hci0"
ADV_PATH = "/org/bluez/espdrop/airdrop_adv"


def hash2(identifier: str) -> bytes:
    """First 2 bytes of SHA-256(identifier), or 0x0000 for an empty string."""
    if not identifier:
        return b"\x00\x00"
    return hashlib.sha256(identifier.encode()).digest()[:2]


def build_payload(appleid, phone, email, email2) -> bytes:
    # Manufacturer-specific data AFTER the 2-byte company id (BlueZ prepends it).
    return (b"\x05\x12" + b"\x00" * 8 + b"\x01"
            + hash2(appleid) + hash2(phone) + hash2(email) + hash2(email2)
            + b"\x00")


class Advertisement(dbus.service.Object):
    """An org.bluez.LEAdvertisement1 broadcast advert carrying our payload."""

    def __init__(self, bus, path, payload, on_release):
        super().__init__(bus, path)
        self._payload = payload
        self._on_release = on_release

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != "org.bluez.LEAdvertisement1":
            raise dbus.exceptions.DBusException(
                "org.freedesktop.DBus.Error.InvalidArgs")
        return {
            "Type": dbus.String("broadcast"),
            "ManufacturerData": dbus.Dictionary(
                {dbus.UInt16(APPLE_CID): dbus.Array(self._payload, signature="y")},
                signature="qv"),
            # The legacy advert is 31 bytes and this payload already spends 24;
            # adding a TX-power field risks overflowing it.
            "IncludeTxPower": dbus.Boolean(False),
        }

    @dbus.service.method("org.bluez.LEAdvertisement1")
    def Release(self):
        # BlueZ calls this when it drops the advert (e.g. the controller went
        # away), the only notice we get; let the main loop re-register.
        self._on_release()


def main() -> int:
    ap = argparse.ArgumentParser(description="Wake an iPhone's AirDrop receiver over BLE")
    ap.add_argument("--appleid", default="", help="Apple ID to hash (contacts-only target)")
    ap.add_argument("--phone", default="", help="phone number as stored in the target's contacts")
    ap.add_argument("--email", default="", help="email address to hash")
    ap.add_argument("--email2", default="", help="second email address to hash")
    ap.add_argument("--adapter", default=ADAPTER, help="BlueZ adapter object path")
    args = ap.parse_args()

    payload = build_payload(args.appleid, args.phone, args.email, args.email2)
    everyone = not any((args.appleid, args.phone, args.email, args.email2))
    print("AirDrop BLE wake: manufacturer 0x%04X, data %s" % (APPLE_CID, payload.hex()))
    print("mode:", "Everyone (empty hashes)" if everyone
          else "contacts-only (hashed identifiers)")
    print("The target iPhone/iPad must be on AirDrop -> Everyone (or have a matching")
    print("contact) and unlocked. Ctrl-C to stop.")

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    try:
        # follow_name_owner_changes: survive a bluetoothd restart, which hands
        # org.bluez to a new owner that a bound proxy would not follow.
        manager = dbus.Interface(
            bus.get_object("org.bluez", args.adapter, follow_name_owner_changes=True),
            "org.bluez.LEAdvertisingManager1")
    except dbus.exceptions.DBusException as e:
        sys.exit("error: no BlueZ adapter at %s (%s); is bluetooth on?" % (args.adapter, e))

    loop = GLib.MainLoop()
    # RegisterAdvertisement is asynchronous; `pending` keeps the 3 s re-arm
    # timer from registering a second advert on top of an in-flight first.
    state = {"up": False, "pending": False}

    def ok():
        state["pending"] = False
        if not state["up"]:
            print("advert up", flush=True)
        state["up"] = True

    def ko(error):
        state["pending"] = False
        if state["up"]:
            print("advert lost (%s) - re-arming" % error, flush=True)
        state["up"] = False

    def released():
        if state["up"]:
            print("advert released by BlueZ - re-arming", flush=True)
        state["up"] = False

    # Keep a reference: if the object is collected its D-Bus path disappears and
    # BlueZ silently drops the advert.
    adv = Advertisement(bus, ADV_PATH, payload, released)

    def arm():
        if not state["up"] and not state["pending"]:
            state["pending"] = True
            try:
                manager.RegisterAdvertisement(
                    ADV_PATH, dbus.Dictionary({}, signature="sv"),
                    reply_handler=ok, error_handler=ko)
            except dbus.exceptions.DBusException as exc:
                state["pending"] = False
                print("register: %s" % exc, file=sys.stderr, flush=True)
        return True  # keep the timer

    arm()
    GLib.timeout_add_seconds(3, arm)

    def stop(*_):
        try:
            manager.UnregisterAdvertisement(ADV_PATH)
        except dbus.exceptions.DBusException:
            pass
        try:
            adv.remove_from_connection()
        except Exception:
            pass
        print("\nstopped.")
        loop.quit()
        return False

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, stop)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, stop)
    loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
