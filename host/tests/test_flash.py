# EspDrop tests - the firmware from the app (flash.py): images are checked
# before anything is written, the version on the ESP, and the esptool calls
# (against stand-ins, and once through the installed esptool's own logger).
#
#   ~/opendrop-venv/bin/python -m unittest discover -s host/tests
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import json
import os
import struct
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from espdrop import flash  # noqa: E402
from espdrop.flash import APP_OFFSET, FirmwareError  # noqa: E402

C5, C3 = 23, 5


def image_header(chip, segments=1):
    h = bytearray(24)
    h[0], h[1] = 0xE9, segments
    struct.pack_into("<H", h, 12, chip)
    return bytes(h)


def app_image(chip=C5, version="0.1.0", size=0x3000):
    """An app image as ESP-IDF lays it out: header, first segment, and the
    app description at 0x20 (made-up build date)."""
    desc = (struct.pack("<IIII", flash.APP_DESC_MAGIC, 0, 0, 0)
            + version.encode().ljust(32, b"\0") + b"espdrop_spikes".ljust(32, b"\0")
            + b"12:34:56".ljust(16, b"\0") + b"Oct 31 2026".ljust(16, b"\0")
            + b"v6.0.3".ljust(32, b"\0") + bytes(32))
    data = image_header(chip) + struct.pack("<II", 0x42000020, len(desc)) + desc
    return data + b"\x5a" * (size - len(data))


def merged_image(chip=C5, version="0.1.0", boot=True, table=True):
    data = bytearray(b"\xff" * APP_OFFSET) + app_image(chip, version)
    if boot:
        at = flash.BOOT_OFFSET.get(chip, 0)
        data[at:at + 0x1000] = image_header(chip) + b"\x11" * (0x1000 - 24)
    if table:
        data[0x8000:0x8020] = b"\xaa\x50" + b"\x01" * 30
    return bytes(data)


class ImageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, data):
        path = os.path.join(self.tmp.name, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def build(self, folder="build_c5_bridge", bridge=True, chip=C5):
        """An ESP-IDF build folder: flasher_args.json, the three files, the config."""
        base = os.path.join(self.tmp.name, folder)
        self.write(os.path.join(base, "bootloader", "bootloader.bin"),
                   image_header(chip) + bytes(8))
        self.write(os.path.join(base, "partition_table", "partition-table.bin"),
                   b"\xaa\x50" + bytes(30))
        self.write(os.path.join(base, "espdrop_spikes.bin"), app_image(chip))
        args = {"flash_files": {"0x2000": "bootloader/bootloader.bin",
                                "0x8000": "partition_table/partition-table.bin",
                                "0x10000": "espdrop_spikes.bin"},
                "app": {"offset": "0x10000", "file": "espdrop_spikes.bin"},
                "extra_esptool_args": {"chip": "esp32c5" if chip == C5 else "esp32c3"}}
        self.write(os.path.join(base, "flasher_args.json"), json.dumps(args).encode())
        self.write(os.path.join(base, "config", "sdkconfig.json"),
                   json.dumps({"ESPDROP_BRIDGE_MODE": bridge, "IDF_TARGET": "esp32c5"}).encode())
        return base

    def test_merged_image(self):
        fw = flash.load(self.write("espdrop-esp32c5.bin", merged_image()))
        self.assertEqual((fw.version, fw.chip, fw.idf), ("0.1.0", "ESP32-C5", "v6.0.3"))
        self.assertEqual(fw.built, "Oct 31 2026 12:34:56")
        self.assertEqual([offset for offset, _ in fw.parts], [0])
        self.assertEqual(fw.size, APP_OFFSET + 0x3000)

    def test_what_is_refused(self):
        cases = [
            (app_image(), "App-Teil"),                                  # the app alone
            (os.urandom(0x20000), "Keine ESP-Firmware"),
            (merged_image(boot=False), "Bootloader"),
            (merged_image(table=False), "Partitionstabelle"),
            (merged_image(chip=99), "anderen Chip"),
        ]
        for i, (data, words) in enumerate(cases):
            with self.subTest(words), self.assertRaises(FirmwareError) as cm:
                flash.load(self.write(f"x{i}.bin", data))
            self.assertIn(words, str(cm.exception))
        with self.assertRaises(FirmwareError):
            flash.load(os.path.join(self.tmp.name, "missing.bin"))

    def test_c3_image_has_its_bootloader_at_0(self):
        self.assertEqual(flash.load(self.write("c3.bin", merged_image(chip=C3))).chip, "ESP32-C3")

    def test_build_folder(self):
        base = self.build()
        for path in (base, os.path.join(base, "flasher_args.json")):
            fw = flash.load(path)
            self.assertEqual([o for o, _ in fw.parts], [0x2000, 0x8000, 0x10000])
            self.assertEqual((fw.version, fw.chip), ("0.1.0", "ESP32-C5"))
        with self.assertRaises(FirmwareError) as cm:
            flash.load(self.build("build_c5", bridge=False))
        self.assertIn("Test-Konsole", str(cm.exception))

    def test_release_image_from_a_build(self):
        # tools/firmware_image.py: the build merged into one image (esptool's
        # merge-bin), which the app then takes as it is.
        try:
            import esptool.cmds  # noqa: F401
        except ImportError:
            self.skipTest("esptool not installed")
        sys.path.insert(0, os.path.join(HERE, "..", "..", "tools"))
        import firmware_image
        out = os.path.join(self.tmp.name, "out", flash.FIRMWARE_NAME)
        fw = firmware_image.make_image(self.build(), out)
        self.assertEqual((fw.version, fw.chip, [o for o, _ in fw.parts]),
                         ("0.1.0", "ESP32-C5", [0]))
        data = fw.parts[0][1]
        self.assertEqual(data[0x2000], 0xE9)
        self.assertEqual(data[0x8000:0x8002], b"\xaa\x50")
        self.assertEqual(data[:0x2000], b"\xff" * 0x2000)
        with self.assertRaises(FirmwareError):                # the console build: no
            firmware_image.make_image(self.build("build_c5", bridge=False), out + "2")

    def test_bundled(self):
        here = os.path.join(self.tmp.name, "pkg")
        os.makedirs(here)
        with mock.patch.dict(os.environ, {"ESPDROP_FIRMWARE": ""}):
            self.assertIsNone(flash.bundled(here=here, root=self.tmp.name))
            # A local build of the bridge in the checkout (not the console one).
            self.build(os.path.join("firmware", "spikes", "build_c5"), bridge=False)
            self.assertIsNone(flash.bundled(here=here, root=self.tmp.name))
            b = self.build(os.path.join("firmware", "spikes", "build_c5_bridge"))
            self.assertEqual(flash.bundled(here=here, root=self.tmp.name), b)
            # What a release ships next to the package wins over a build.
            shipped = self.write(os.path.join("pkg", "firmware", flash.FIRMWARE_NAME), b"x")
            self.assertEqual(flash.bundled(here=here, root=self.tmp.name), shipped)
        with mock.patch.dict(os.environ, {"ESPDROP_FIRMWARE": "/x/fw.bin"}):
            self.assertEqual(flash.bundled(here=here, root=self.tmp.name), "/x/fw.bin")


class VersionTest(unittest.TestCase):
    def test_parse(self):
        v = flash.parse_version("version 0.1.0 idf=v6.0.3 chip=esp32c5 rev=1.0 proto=1")
        self.assertEqual(v, {"version": "0.1.0", "idf": "v6.0.3", "chip": "esp32c5",
                             "rev": "1.0", "proto": "1"})
        self.assertIsNone(flash.parse_version("unknown cmd 'version'"))
        self.assertIsNone(flash.parse_version(None))

    def test_newer(self):
        self.assertTrue(flash.is_newer("0.2.0", "0.1.9"))
        self.assertFalse(flash.is_newer("0.1.0", "0.1.0"))
        self.assertFalse(flash.is_newer("0.1.0", "0.2.0"))     # never offer a downgrade
        self.assertTrue(flash.is_newer("0.1.0", None))         # too old to say
        self.assertTrue(flash.is_newer("0.1.0-3-gabc", "0.1.0"))
        self.assertFalse(flash.is_newer("v0.1.0", "0.1.0"))

    def test_written(self):
        parts = [(0x2000, b"b" * 0x1000), (0x8000, b"p" * 0xC00), (0x10000, b"a" * 0x5000)]
        self.assertEqual(flash.written(parts, 0x2000), 0)
        self.assertEqual(flash.written(parts, 0x2800), 0x800)
        self.assertEqual(flash.written(parts, 0x8C00), 0x1C00)
        self.assertEqual(flash.written(parts, 0x12000), 0x1C00 + 0x2000)
        self.assertEqual(flash.written(parts, 0x15000), 0x6C00)


class FakeEsptool:
    """esptool.cmds and esptool.logger as far as flash() uses them."""

    def __init__(self, chip_id=C5, chip_name="ESP32-C5", fail=None):
        self.calls, self.chip_id, self.chip_name, self.fail = [], chip_id, chip_name, fail
        self.logger = None
        fake = self

        class TemplateLogger:
            pass

        class Log:
            def set_logger(self, logger):
                fake.logger = logger

        class Esp:
            IMAGE_CHIP_ID = chip_id
            CHIP_NAME = chip_name

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                fake.calls.append("closed")
                return False

        def detect_chip(port):
            fake.calls.append(("detect", port))
            if fake.fail:
                raise fake.fail
            return Esp()

        def write_flash(esp, parts, compress=False):
            fake.calls.append(("write", [(o, len(d)) for o, d in parts], compress))
            fake.logger.progress_bar(1, 1, prefix="Uploading stub ")   # not a write
            for offset, data in parts:
                for at in range(0, len(data) + 1, max(len(data) // 2, 1)):
                    fake.logger.progress_bar(at // 3, len(data) // 3,
                                             prefix=f"Writing at {offset + at:#010x} ")
            fake.logger.print("Hash of data verified.")

        self.cmds = types.SimpleNamespace(
            detect_chip=detect_chip, write_flash=write_flash,
            run_stub=lambda esp: (fake.calls.append("stub"), esp)[1],
            attach_flash=lambda esp: fake.calls.append("attach"),
            reset_chip=lambda esp, mode: fake.calls.append(("reset", mode)))
        self.log_module = types.SimpleNamespace(TemplateLogger=TemplateLogger, log=Log())

    def modules(self):
        pkg = types.ModuleType("esptool")
        pkg.cmds, pkg.logger = self.cmds, self.log_module
        return {"esptool": pkg, "esptool.cmds": self.cmds, "esptool.logger": self.log_module}


class FlashTest(unittest.TestCase):
    def fw(self, parts=None):
        parts = parts or [(0, merged_image())]
        return flash.Firmware(parts, C5, flash.app_desc(app_image()), "test")

    def run_flash(self, fake, fw=None):
        stages, logs = [], []
        with mock.patch.dict(sys.modules, fake.modules()):
            flash.flash(fw or self.fw(), "/dev/ttyACM0",
                        on_progress=lambda stage, fraction=None: stages.append((stage, fraction)),
                        on_log=logs.append)
        return stages, logs

    def test_writes_verifies_restarts(self):
        fake = FakeEsptool()
        stages, logs = self.run_flash(fake)
        self.assertEqual(fake.calls, [("detect", "/dev/ttyACM0"), "stub", "attach",
                                      ("write", [(0, APP_OFFSET + 0x3000)], True),
                                      ("reset", "hard-reset"), "closed"])
        names = [s for s, _ in stages]
        self.assertEqual(names[0], "connect")
        self.assertEqual(names[-3:], ["verify", "restart", "done"])
        writes = [f for s, f in stages if s == "write"]
        self.assertEqual(writes[0], 0.0)
        self.assertEqual(writes, sorted(writes))
        self.assertEqual(writes[-1], 1.0)
        self.assertIn("Hash of data verified.", logs)

    def test_progress_over_several_parts(self):
        fw = self.fw([(0x2000, b"b" * 0x5000), (0x8000, b"p" * 0xC00), (0x10000, b"a" * 0x9000)])
        stages, _ = self.run_flash(FakeEsptool(), fw)
        writes = [f for s, f in stages if s == "write"]
        self.assertEqual(writes, sorted(writes))           # never jumps back per part
        self.assertAlmostEqual(writes[-1], 1.0)
        self.assertEqual([s for s, _ in stages].count("verify"), 1)

    def test_other_chip_is_left_alone(self):
        fake = FakeEsptool(chip_id=C3, chip_name="ESP32-C3")
        with self.assertRaises(FirmwareError) as cm:
            self.run_flash(fake)
        self.assertIn("ESP32-C3", str(cm.exception))
        self.assertNotIn("write", [c[0] for c in fake.calls if isinstance(c, tuple)])

    def test_errors_in_words(self):
        for error, words in (
                (Exception("Failed to connect to ESP32-C5: No serial data received."), "BOOT"),
                (OSError("could not open port '/dev/ttyACM0': [Errno 16] Device or resource busy"),
                 "belegt"),
                (RuntimeError("something else"), "something else")):
            with self.subTest(words), self.assertRaises(FirmwareError) as cm:
                self.run_flash(FakeEsptool(fail=error))
            self.assertIn(words, str(cm.exception))

    def test_without_esptool(self):
        with mock.patch.dict(sys.modules, {"esptool": None, "esptool.cmds": None}):
            with self.assertRaises(FirmwareError) as cm:
                flash.flash(self.fw(), "COM5")
        self.assertIn("esptool", str(cm.exception))

    def test_real_esptool_logger(self):
        # The installed esptool hands its progress to our logger as expected
        # ("Writing at 0x..." in the prefix), through its own adapter.
        try:
            import esptool.cmds as cmds
            from esptool.logger import log
        except ImportError:
            self.skipTest("esptool not installed")
        size = APP_OFFSET + 0x3000

        class Esp:
            IMAGE_CHIP_ID, CHIP_NAME = C5, "ESP32-C5"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def write_flash(esp, parts, compress=False):
            # What esptool's write_flash does for the progress (cmds.py).
            # The description is address + bytes written (uncompressed), so
            # the last one is the image's end; the bar counts compressed bytes.
            with log.progress(total=size // 2, unit="B") as progress:
                for at in list(range(0x4000, size, 0x4000)) + [size]:
                    progress.update(advance=0x2000, description=f"Writing at {at:#010x}")
            log.print("Hash of data verified.")

        stages, logs = [], []
        with mock.patch.multiple(cmds, detect_chip=lambda port: Esp(), run_stub=lambda e: e,
                                 attach_flash=lambda e: None, write_flash=write_flash,
                                 reset_chip=lambda e, m: None):
            flash.flash(self.fw(), "COM5", lambda s, f=None: stages.append((s, f)), logs.append)
        writes = [f for s, f in stages if s == "write"]
        self.assertGreater(len(writes), 3)
        self.assertAlmostEqual(writes[1], 0x4000 / size)
        self.assertIn("verify", [s for s, _ in stages])
        self.assertIn("Hash of data verified.", logs)


if __name__ == "__main__":
    unittest.main()
