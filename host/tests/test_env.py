# EspDrop tests - the Windows pieces of M4.5, run on Linux: TLS identity,
# archive writer, libarchive lookup, `espdrop check`, the installer's patcher.
#
#   ~/opendrop-venv/bin/python -m unittest discover -s host/tests
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import contextlib
import importlib.util
import io
import os
import ssl
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from espdrop import cli, env  # noqa: E402
import test_host  # noqa: E402  (module only: its TestCases are not collected twice)


def load_installer():
    path = os.path.join(HERE, "..", "..", "tools", "opendrop_install.py")
    spec = importlib.util.spec_from_file_location("opendrop_install", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class IdentityTest(unittest.TestCase):
    def setUp(self):
        try:
            import cryptography  # noqa: F401
        except ImportError:
            self.skipTest("python-cryptography not installed")

    def test_created_like_openssl_and_used_by_opendrop(self):
        from cryptography import x509
        env.load_opendrop()
        from opendrop.config import AirDropConfig
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(env.ensure_identity("Pr\u00fcf-PC", d), "created")
            self.assertEqual(env.ensure_identity("Pr\u00fcf-PC", d), "exists")
            keys, cert_file, key_file = env.identity_files(d)
            with open(cert_file, "rb") as f:
                cert = x509.load_pem_x509_certificate(f.read())
            self.assertEqual(cert.subject.rfc4514_string(), "CN=Pr\u00fcf-PC")
            self.assertEqual(cert.subject, cert.issuer)
            self.assertTrue(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
            self.assertEqual(cert.public_key().key_size, 2048)
            if os.name == "posix":
                self.assertEqual(os.stat(key_file).st_mode & 0o777, 0o600)
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert_file, key_file)
            # OpenDrop finds both files and does not run openssl.
            with mock.patch("subprocess.run", side_effect=AssertionError("openssl called")):
                config = AirDropConfig(computer_name="Pr\u00fcf-PC", airdrop_dir=d)
            config.get_ssl_context()


class ArchiveWriterTest(unittest.TestCase):
    """fix_archive_writer (used on Windows) gives members with '/' names and the
    file's bytes."""

    def test_names_and_content(self):
        env.load_opendrop()
        import libarchive
        from opendrop.util import AbsArchiveWrite

        class Writer(AbsArchiveWrite):
            pass
        env.fix_archive_writer(Writer)
        env.fix_archive_writer(Writer)                    # idempotent
        data = os.urandom(150000)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "Bild 1.jpg")
            with open(path, "wb") as f:
                f.write(data)
            out = io.BytesIO()
            with libarchive.custom_writer(out.write, "cpio", filter_name="gzip",
                                          archive_write_class=Writer) as archive:
                archive.add_abs_file(path, ".\\Bild 1.jpg")   # os.path.join(".", name) on Windows
        with libarchive.memory_reader(out.getvalue()) as archive:
            got = [(e.pathname, e.size, e.isfile, b"".join(e.get_blocks())) for e in archive]
        self.assertEqual(got, [("./Bild 1.jpg", len(data), True, data)])


class FixedWriterEndToEndTest(test_host.EndToEndTest):
    """espdrop send with the Windows archive writer, received by real OpenDrop."""

    def setUp(self):
        env.load_opendrop()
        from opendrop.util import AbsArchiveWrite
        self.cls, self.orig = AbsArchiveWrite, AbsArchiveWrite.__dict__["add_abs_file"]
        env.fix_archive_writer(AbsArchiveWrite)

    def tearDown(self):
        self.cls.add_abs_file = self.orig


class LibarchiveLookupTest(unittest.TestCase):
    def test_explicit_path_is_the_only_candidate(self):
        with mock.patch.dict(os.environ, {"LIBARCHIVE": "/opt/la/libarchive.so"}):
            self.assertEqual(env.libarchive_candidates(), ["/opt/la/libarchive.so"])

    def test_windows_falls_back_to_system_libarchive(self):
        with mock.patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}), \
                mock.patch.object(env.os, "name", "nt"):
            os.environ.pop("LIBARCHIVE", None)
            cands = env.libarchive_candidates()
        self.assertIn(None, cands)
        self.assertTrue(cands[-1].endswith("archiveint.dll"), cands)

    def test_probe(self):
        ok, detail = env.probe_libarchive(os.path.join(HERE, "no-such-libarchive.dll"))
        self.assertFalse(ok)
        self.assertIn("cannot load", detail)
        import ctypes.util
        found = ctypes.util.find_library("archive")
        if found:
            ok, detail = env.probe_libarchive(found)
            self.assertTrue(ok, detail)
            self.assertTrue(detail.startswith("libarchive 3."), detail)


class CheckTest(unittest.TestCase):
    def test_this_venv_is_ready(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["check"])
        self.assertEqual(rc, 0, out.getvalue())
        text = out.getvalue()
        for item in ("libarchive", "opendrop", "archive", "tls"):
            self.assertRegex(text, rf"ok   {item}", text)
        self.assertIn("ready.", text)


class PySerialPortTest(unittest.TestCase):
    def test_timeouts_only_set_when_they_change(self):
        try:
            import serial  # noqa: F401
        except ImportError:
            self.skipTest("pyserial not installed")
        from espdrop.transport import PySerialPort

        class FakeSerial:
            def __init__(self, name, timeout, write_timeout):
                self.sets, self.in_waiting = [], 0

            def __setattr__(self, k, v):
                if k in ("timeout", "write_timeout"):
                    self.sets.append((k, v))
                object.__setattr__(self, k, v)

            def read(self, n):
                return b""

            def write(self, data):
                return len(data)

        with mock.patch("serial.Serial", FakeSerial):
            port = PySerialPort("COM9")
        for _ in range(3):
            port.read(0.2)
            port.write(b"x")
        self.assertEqual(port.ser.sets, [])
        port.read(0.5)
        port.write(b"x", timeout=2.0)
        port.read(0.5)
        self.assertEqual(port.ser.sets, [("timeout", 0.5), ("write_timeout", 2.0)])


class PatchApplierTest(unittest.TestCase):
    SRC = "\n".join(f"line {i}" for i in range(1, 41)) + "\n"

    def setUp(self):
        self.inst = load_installer()

    def patch(self, body):
        return textwrap.dedent("""\
            commentary before the diff
            diff --git a/opendrop/x.py b/opendrop/x.py
            index 0000000..1111111 100644
            --- a/opendrop/x.py\t2026-01-01 00:00:00.000000000 +0000
            +++ b/opendrop/x.py\t2026-01-01 00:00:00.000000000 +0000
            """) + textwrap.dedent(body)

    def test_hunks_with_offset_and_dash_lines(self):
        # Two lines more at the top than the patch expects: both hunks apply at
        # offset +2. The second removes "-- old" and adds "++ new", which in the
        # diff read "--- old" / "+++ new" and must not be taken for a header.
        src = "extra a\nextra b\n" + self.SRC.replace("line 31", "-- old")
        p = self.patch("""\
            @@ -3,3 +3,4 @@
             line 3
            -line 4
            +line four
            +line 4b
             line 5
            @@ -30,3 +31,3 @@
             line 30
            --- old
            +++ new
             line 32
            """)
        (path, hunks), = self.inst.parse_patch(p)
        self.assertEqual(path, "opendrop/x.py")
        out = "\n".join(self.inst.apply_hunks(src.split("\n"), hunks))
        want = (src.replace("line 4\n", "line four\nline 4b\n")
                .replace("-- old", "++ new"))
        self.assertEqual(out, want)

    def test_mismatch_and_refusals(self):
        bad = self.patch("""\
            @@ -3,2 +3,2 @@
             line 3
            -line 99
            +line x
            """)
        (_path, hunks), = self.inst.parse_patch(bad)
        with self.assertRaises(self.inst.PatchError):
            self.inst.apply_hunks(self.SRC.split("\n"), hunks)
        with self.assertRaises(self.inst.PatchError):
            self.inst.parse_patch("--- /dev/null\n+++ b/opendrop/new.py\n@@ -0,0 +1 @@\n+x\n")
        with self.assertRaises(self.inst.PatchError):    # counts do not add up
            self.inst.parse_patch(self.patch("@@ -1,2 +1,2 @@\n line 1\n+only\n"))
        with tempfile.TemporaryDirectory() as site:
            outside = ("--- a/elsewhere/x.py\n+++ b/elsewhere/x.py\n"
                       "@@ -1 +1 @@\n-line 1\n+line one\n")
            with self.assertRaises(self.inst.PatchError):
                self.inst.apply_patch(site, outside.encode(), "outside")

    def test_apply_patch_writes_lf_files(self):
        with tempfile.TemporaryDirectory() as site:
            os.makedirs(os.path.join(site, "opendrop"))
            target = os.path.join(site, "opendrop", "x.py")
            with open(target, "wb") as f:
                f.write(self.SRC.encode())
            p = self.patch("@@ -1,2 +1,2 @@\n-line 1\n+line one\n line 2\n")
            self.inst.apply_patch(site, p.encode(), "t")
            with open(target, "rb") as f:
                self.assertEqual(f.read(), self.SRC.replace("line 1\n", "line one\n", 1).encode())


if __name__ == "__main__":
    unittest.main()
