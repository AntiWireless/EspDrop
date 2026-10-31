# EspDrop - put the firmware on the ESP from the app (esptool, in this process).
#
# Two kinds of firmware are accepted, and checked before anything is written:
#   - an image of the whole flash, as release builds ship it (`esptool
#     merge-bin`: bootloader, partition table and the app at their offsets),
#     written from 0
#   - an ESP-IDF build folder: the files its flasher_args.json lists, each at
#     its offset (only a build of the bridge firmware, not the test console)
# Both must carry ESP image headers for one chip, and the app its description
# (esp_app_desc_t at offset 0x20: version, build date, IDF); the chip the ESP
# turns out to be must be that one before esptool erases anything.
#
# The ESP32-C5's USB-Serial/JTAG needs no buttons: esptool resets it into the
# ROM bootloader over the same USB port the app uses, writes, verifies the
# result (MD5) and resets it into the new firmware. The ROM bootloader cannot
# be overwritten, so a failed or interrupted write is fixed by flashing again.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import glob
import json
import os
import re
import struct

IMAGE_MAGIC = 0xE9
APP_DESC_MAGIC = 0xABCD5432
APP_OFFSET = 0x10000
PART_TABLE_OFFSET = 0x8000
CHIPS = {5: "ESP32-C3", 23: "ESP32-C5"}       # image chip id -> name (as esptool says it)
BOOT_OFFSET = {5: 0x0, 23: 0x2000}            # where each chip's ROM looks for the bootloader
FIRMWARE_NAME = "espdrop-esp32c5.bin"         # the image release builds ship


class FirmwareError(Exception):
    """Not a usable firmware, or flashing failed; the message is for the user."""


class Firmware:
    """What to write: parts [(offset, bytes)] for one chip, and its version."""

    def __init__(self, parts, chip_id, desc, source):
        self.parts = parts
        self.chip_id = chip_id
        self.version = desc["version"]
        self.project = desc["project"]
        self.built = desc["built"]
        self.idf = desc["idf"]
        self.source = source

    @property
    def chip(self):
        return CHIPS.get(self.chip_id, f"Chip {self.chip_id}")

    @property
    def size(self):
        return sum(len(data) for _, data in self.parts)

    def __repr__(self):
        return f"<Firmware {self.version} {self.chip} {self.source}>"


# ---- reading images ------------------------------------------------------------

def _cstr(raw):
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


def image_chip(data, offset=0):
    """The chip id in the ESP image header at offset, or None if none is there."""
    if len(data) < offset + 24 or data[offset] != IMAGE_MAGIC:
        return None
    return struct.unpack_from("<H", data, offset + 12)[0]


def app_desc(data, offset=0):
    """The app description (esp_app_desc_t) of the app image at offset, or None."""
    at = offset + 0x20
    if len(data) < at + 144 or struct.unpack_from("<I", data, at)[0] != APP_DESC_MAGIC:
        return None
    return {"version": _cstr(data[at + 16:at + 48]),
            "project": _cstr(data[at + 48:at + 80]),
            "built": f"{_cstr(data[at + 96:at + 112])} {_cstr(data[at + 80:at + 96])}".strip(),
            "idf": _cstr(data[at + 112:at + 144])}


def load(path):
    """The firmware in a file (an image of the whole flash) or an ESP-IDF build
    folder (or its flasher_args.json). Raises FirmwareError with the reason."""
    if os.path.isdir(path) or os.path.basename(path) == "flasher_args.json":
        return _load_build(path if os.path.isdir(path) else os.path.dirname(path))
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise FirmwareError(f"{path} lässt sich nicht lesen: {e.strerror or e}") from e
    chip, desc = image_chip(data, APP_OFFSET), app_desc(data, APP_OFFSET)
    if chip is None or desc is None:
        if image_chip(data) is not None and app_desc(data) is not None:
            raise FirmwareError("Das ist nur der App-Teil einer Firmware. Gebraucht wird das "
                                f"vollständige Image ({FIRMWARE_NAME}) oder der Build-Ordner.")
        raise FirmwareError("Keine ESP-Firmware: es fehlt die App an 0x10000.")
    boot = BOOT_OFFSET.get(chip)
    if boot is None:
        raise FirmwareError(f"Firmware für einen anderen Chip (Kennung {chip}).")
    if image_chip(data, boot) != chip:
        raise FirmwareError(f"Kein vollständiges Image: der Bootloader für den {CHIPS[chip]} "
                            f"fehlt an {boot:#x}.")
    if data[PART_TABLE_OFFSET:PART_TABLE_OFFSET + 2] != b"\xaa\x50":
        raise FirmwareError("Kein vollständiges Image: die Partitionstabelle fehlt an 0x8000.")
    return Firmware([(0, data)], chip, desc, path)


def _load_build(folder):
    try:
        with open(os.path.join(folder, "flasher_args.json"), encoding="utf-8") as f:
            args = json.load(f)
        files = {int(off, 16): name for off, name in args["flash_files"].items()}
        app_offset = int(args["app"]["offset"], 16)
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise FirmwareError(f"{folder} ist kein ESP-IDF-Build-Ordner ({e}).") from e
    if _bridge_mode(folder) is False:
        raise FirmwareError("Dieser Build ist die Test-Konsole, nicht die Firmware für die App: "
                            "mit sdkconfig.bridge.defaults bauen (firmware/README.md).")
    parts = []
    for offset in sorted(files):
        try:
            with open(os.path.join(folder, files[offset]), "rb") as f:
                parts.append((offset, f.read()))
        except OSError as e:
            raise FirmwareError(f"{files[offset]} fehlt im Build ({e.strerror or e}).") from e
    app = dict(parts).get(app_offset, b"")
    chip, desc = image_chip(app), app_desc(app)
    if chip is None or desc is None:
        raise FirmwareError(f"Die App im Build ({files.get(app_offset)}) ist kein ESP-Image.")
    if chip not in CHIPS:
        raise FirmwareError(f"Firmware für einen anderen Chip (Kennung {chip}).")
    for offset, data in parts:
        if data[:1] == bytes([IMAGE_MAGIC]) and image_chip(data) != chip:
            raise FirmwareError(f"{files[offset]} ist für einen anderen Chip als die App.")
    return Firmware(parts, chip, desc, folder)


def _bridge_mode(folder):
    """True/False from the build's configuration, None if it does not say."""
    try:
        with open(os.path.join(folder, "config", "sdkconfig.json"), encoding="utf-8") as f:
            return bool(json.load(f).get("ESPDROP_BRIDGE_MODE", False))
    except (OSError, ValueError):
        return None


def bundled(here=None, root=None):
    """Where the firmware this app comes with is: ESPDROP_FIRMWARE, the image
    shipped next to this module (release builds: here/firmware), or a local
    ESP-IDF build of the bridge for the ESP32-C5 in this checkout (root;
    newest first). None if there is none."""
    env = os.environ.get("ESPDROP_FIRMWARE")
    if env:
        return env
    here = here or os.path.dirname(os.path.abspath(__file__))
    shipped = os.path.join(here, "firmware", FIRMWARE_NAME)
    if os.path.isfile(shipped):
        return shipped
    root = root or os.path.dirname(os.path.dirname(here))
    builds = []
    for args in glob.glob(os.path.join(root, "firmware", "*", "build*", "flasher_args.json")):
        folder = os.path.dirname(args)
        try:
            with open(args, encoding="utf-8") as f:
                info = json.load(f)
            if info["extra_esptool_args"]["chip"] != "esp32c5" or not _bridge_mode(folder):
                continue
            app = os.path.join(folder, info["app"]["file"])
            builds.append((os.path.getmtime(app), folder))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return max(builds)[1] if builds else None


# ---- the version on the ESP ------------------------------------------------------

def parse_version(reply):
    """The firmware's `version` reply ('version 0.1.0 idf=v6.0.3 chip=esp32c5
    rev=1.0 proto=1') as a dict; None for firmware without the command."""
    words = (reply or "").split()
    if len(words) < 2 or words[0] != "version":
        return None
    info = {"version": words[1]}
    for word in words[2:]:
        key, sep, value = word.partition("=")
        if sep:
            info[key] = value
    return info


def _version_key(v):
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else None


def is_newer(candidate, installed):
    """True if candidate is a newer X.Y.Z than installed (or installed is
    unknown: firmware too old to say); for versions that are not X.Y.Z, any
    difference counts."""
    if not installed:
        return True
    a, b = _version_key(candidate), _version_key(installed)
    if a is None or b is None:
        return candidate != installed
    return a > b


# ---- writing -------------------------------------------------------------------

def ports():
    """Serial ports that look like an ESP, best first."""
    from .transport import candidates
    return candidates()


def flash(fw, port, on_progress=None, on_log=None):
    """Write fw to the ESP on port; the app must have let go of the port.

    on_progress(stage, fraction): "connect", "write" (0..1 over all parts),
    "verify", "restart", "done". on_log(text): esptool's messages. Raises
    FirmwareError with a message for the user."""
    try:
        from esptool.cmds import attach_flash, detect_chip, reset_chip, run_stub, write_flash
        from esptool.logger import TemplateLogger, log
    except ImportError as e:
        raise FirmwareError("esptool fehlt: tools/opendrop_install.py installiert es "
                            "(oder pip install esptool).") from e
    report = on_progress or (lambda stage, fraction=None: None)
    parts = sorted(fw.parts)                   # written in this order

    class Progress(TemplateLogger):
        """esptool's output: the write progress, the rest as log lines."""
        writing = False
        verifying = False

        def print(self, *args, **kwargs):
            text = " ".join(str(a) for a in args).strip()
            if text and on_log:
                on_log(text)

        def note(self, message):
            self.print(f"Note: {message}")

        def warning(self, message):
            self.print(f"Warning: {message}")

        def error(self, message):
            self.print(f"Error: {message}")

        def stage(self, finish=False):
            pass

        def set_verbosity(self, verbosity):
            pass

        def progress_bar(self, cur_iter, total_iters, prefix="", suffix="", bar_length=30):
            if not self.writing or self.verifying:
                return                         # the stub upload, reading: not ours
            # "Writing at 0x00012000": the flash address, counted in bytes of
            # the image (the bar itself counts compressed bytes, per part).
            m = re.search(r"Writing at (0x[0-9a-fA-F]+)", prefix or "")
            if m:
                done = written(parts, int(m.group(1), 16))
            elif len(parts) == 1 and total_iters:
                done = fw.size * min(cur_iter / total_iters, 1.0)
            else:
                return
            report("write", min(done / fw.size, 1.0))
            if done >= fw.size:
                self.verifying = True
                report("verify", None)         # esptool checks the MD5 next

    progress = Progress()
    log.set_logger(progress)
    report("connect", None)
    try:
        with detect_chip(port) as esp:
            if esp.IMAGE_CHIP_ID != fw.chip_id:
                raise FirmwareError(f"Am Anschluss {port} steckt ein {esp.CHIP_NAME}; "
                                    f"diese Firmware ist für den {fw.chip}.")
            esp = run_stub(esp)
            attach_flash(esp)
            report("write", 0.0)
            progress.writing = True
            write_flash(esp, parts, compress=True)
            progress.writing = False
            report("restart", None)
            reset_chip(esp, "hard-reset")
    except FirmwareError:
        raise
    except Exception as e:                     # noqa: BLE001 - esptool, pyserial: explain
        raise FirmwareError(explain(e, port)) from e
    report("done", 1.0)


def written(parts, address):
    """Bytes of parts (sorted, written in order) done once esptool writes at
    this flash address."""
    done = 0
    for offset, data in parts:
        if address >= offset + len(data):
            done += len(data)
        elif address > offset:
            done += address - offset
    return done


def explain(error, port):
    """esptool's and pyserial's errors in words for the user."""
    text = str(error) or type(error).__name__
    low = text.lower()
    if any(k in low for k in ("could not open port", "permission denied", "access is denied",
                              "zugriff verweigert", "resource busy", "errno 16")):
        hint = (" Unter Linux muss der Benutzer in der Gruppe 'dialout' sein."
                if os.name == "posix" else "")
        return (f"Der Anschluss {port} ist belegt oder gesperrt: hat ihn ein anderes Programm "
                f"offen (awdl_bridge.py, ein serieller Monitor)?{hint}")
    if any(k in low for k in ("failed to connect", "no serial data", "wrong boot mode")):
        return ("Der ESP ließ sich nicht in den Bootloader schalten. Abstecken, die BOOT-Taste "
                "gedrückt halten, wieder anstecken, loslassen und erneut versuchen.")
    return f"Flashen fehlgeschlagen: {text}"
