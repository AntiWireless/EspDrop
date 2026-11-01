# EspDrop tests - autostart and "send from the file manager" (integration.py):
# the XDG files for real in temporary folders, the Windows registry and the
# "Senden an" shortcut against a stand-in winreg and a recorded PowerShell call.
#
#   ~/opendrop-venv/bin/python -m unittest discover -s host/tests
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from espdrop import integration  # noqa: E402


class FakeWinreg:
    """The bits of winreg integration.py uses, on a dict: key -> {name: value}."""
    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1
    KEY_SET_VALUE = 2

    def __init__(self):
        self.keys = {}

    class _Handle:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def OpenKey(self, root, key, reserved=0, access=0):
        if key not in self.keys:
            raise FileNotFoundError(key)
        return self._Handle(key)

    def CreateKey(self, root, key):
        parts = key.split("\\")
        for i in range(1, len(parts) + 1):
            self.keys.setdefault("\\".join(parts[:i]), {})
        return self._Handle(key)

    def QueryValueEx(self, h, name):
        if name not in self.keys[h.path]:
            raise FileNotFoundError(name)
        return self.keys[h.path][name], self.REG_SZ

    def SetValueEx(self, h, name, reserved, kind, value):
        self.keys[h.path][name] = value

    def DeleteValue(self, h, name):
        if name not in self.keys[h.path]:
            raise FileNotFoundError(name)
        del self.keys[h.path][name]

    def _children(self, key):
        return [k.rsplit("\\", 1)[1] for k in self.keys
                if k.startswith(key + "\\") and "\\" not in k[len(key) + 1:]]

    def EnumKey(self, h, i):
        kids = self._children(h.path)
        if i >= len(kids):
            raise OSError("no more")
        return kids[i]

    def DeleteKey(self, root, key):
        if key not in self.keys:
            raise FileNotFoundError(key)
        if self._children(key):
            raise PermissionError("has subkeys")
        del self.keys[key]


class LinuxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = {"XDG_CONFIG_HOME": os.path.join(self.tmp.name, "config"),
               "XDG_DATA_HOME": os.path.join(self.tmp.name, "data")}
        self.patches = [mock.patch.dict(os.environ, env),
                        mock.patch.object(integration, "IS_WINDOWS", False)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)

    def test_autostart_starts_in_the_background(self):
        self.assertFalse(integration.autostart_enabled())
        integration.set_autostart(True)
        self.assertTrue(integration.autostart_enabled())
        path = os.path.join(self.tmp.name, "config", "autostart", "espdrop.desktop")
        entry = self.read(path)
        self.assertTrue(entry["Exec"].endswith(" gui --background"), entry["Exec"])
        self.assertIn(os.path.join("tools", "espdrop.py"), entry["Exec"])
        integration.set_autostart(False)
        self.assertFalse(integration.autostart_enabled())
        self.assertFalse(os.path.exists(path))

    def test_open_with_entry(self):
        integration.set_send_menu(True)
        self.assertTrue(integration.send_menu_enabled())
        entry = self.read(os.path.join(self.tmp.name, "data", "applications",
                                       "espdrop-send.desktop"))
        self.assertTrue(entry["Exec"].endswith(" gui --send %F"), entry["Exec"])
        self.assertIn("image/jpeg;", entry["MimeType"])
        integration.set_send_menu(False)
        self.assertFalse(integration.send_menu_enabled())

    def test_defaults_and_icon(self):
        # First start: both on; the entries carry our icon file.
        with mock.patch.object(integration, "ICON", "/x/espdrop.png"):
            self.assertEqual(integration.apply_defaults(), [])
            self.assertTrue(integration.autostart_enabled())
            self.assertTrue(integration.send_menu_enabled())
            entry = self.read(os.path.join(self.tmp.name, "data", "applications",
                                           "espdrop-send.desktop"))
            self.assertEqual(entry["Icon"], "/x/espdrop.png")
        self.assertEqual(integration.data_dir(), os.path.join(self.tmp.name, "data", "espdrop"))

    def test_launcher_in_the_application_menu(self):
        with mock.patch.object(integration, "ICON", "/x/espdrop.png"):
            integration.refresh()                  # every start, nothing switched on
        entry = self.read(os.path.join(self.tmp.name, "data", "applications",
                                       "espdrop.desktop"))
        self.assertTrue(entry["Exec"].endswith(" gui"), entry["Exec"])
        self.assertEqual((entry["Name"], entry["Icon"], entry["StartupWMClass"]),
                         ("EspDrop", "/x/espdrop.png", "EspDrop"))
        self.assertNotIn("NoDisplay", entry)
        self.assertFalse(integration.autostart_enabled())

    def test_compiled_program_starts_itself(self):
        with mock.patch("espdrop.frozen.program", lambda: "/opt/EspDrop/EspDrop"):
            self.assertEqual(integration.launch_command("--send"),
                             ["/opt/EspDrop/EspDrop", "gui", "--send"])
            integration.set_autostart(True)
        entry = self.read(os.path.join(self.tmp.name, "config", "autostart", "espdrop.desktop"))
        self.assertEqual(entry["Exec"], "/opt/EspDrop/EspDrop gui --background")

    def test_exec_quoting(self):
        line = integration._exec_line(["/opt/my venv/bin/python", "/x/50%.py", "gui"])
        self.assertEqual(line, '"/opt/my venv/bin/python" /x/50%%.py gui')


class FrozenTest(unittest.TestCase):
    def test_program_paths(self):
        from espdrop import frozen
        self.assertIsNone(frozen.program())               # from Python
        self.assertFalse(frozen.compiled())
        apps = os.path.join(os.sep, "home", "u", "Apps")

        class Compiled:                                   # Nuitka's __compiled__
            onefile = False
            containing_dir = os.path.join(os.sep, "opt", "EspDrop")
            original_argv0 = "./EspDrop"                  # as typed, even in a folder build
            process_exe = os.path.join(os.sep, "opt", "EspDrop", "EspDrop")
        with mock.patch.dict(frozen.__dict__, {"__compiled__": Compiled}), \
                mock.patch.object(sys, "executable", os.path.join(os.sep, "opt", "EspDrop",
                                                                  "python")):
            self.assertTrue(frozen.compiled())
            # The program file itself, not sys.executable (the build Python's name).
            self.assertEqual(frozen.program(), os.path.join(os.sep, "opt", "EspDrop", "EspDrop"))
            # onefile: the binary the user started, not its unpacked copy
            Compiled.onefile = True
            Compiled.containing_dir = apps
            Compiled.original_argv0 = "EspDrop-x86_64"
            Compiled.process_exe = os.path.join(os.sep, "tmp", "onefile_1", "EspDrop-x86_64")
            want = "EspDrop-x86_64.exe" if os.name == "nt" else "EspDrop-x86_64"
            self.assertEqual(frozen.program(), os.path.join(apps, want))
            self.assertEqual(frozen.program_dir(), apps)


class UnpackedCopiesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_in(self, here):
        from espdrop import frozen
        with mock.patch.object(frozen, "bundle_dir", lambda: here):
            frozen.clean_old_unpacked()

    def test_older_builds_go(self):
        app = os.path.join(self.tmp.name, "cache", "EspDrop", "app")
        for name in ("v0.1.0-20261031", "v0.1.0-20261101", "notes"):
            os.makedirs(os.path.join(app, name))
        self.run_in(os.path.join(app, "v0.1.0-20261101"))
        self.assertEqual(sorted(os.listdir(app)), ["notes", "v0.1.0-20261101"])

    def test_own_folder_under_another_spelling_stays(self):
        # Windows may give our path in its 8.3 short form; a symlink stands
        # in for that: another spelling of the very folder we run from.
        app = os.path.join(self.tmp.name, "cache", "EspDrop", "app")
        ours = os.path.join(app, "v0.1.0-20261101093700")
        os.makedirs(ours)
        short = os.path.join(app, "V01~1.0-2")
        os.symlink(ours, short)                     # what bundle_dir() might say
        self.run_in(short)
        self.assertTrue(os.path.isdir(ours))

    def test_anything_else_is_left_alone(self):
        # A folder build, or a cache path we did not choose: nothing is removed.
        other = os.path.join(self.tmp.name, "Programs", "EspDrop")
        os.makedirs(os.path.join(self.tmp.name, "Programs", "vOld"))
        os.makedirs(other)
        self.run_in(other)
        self.assertEqual(sorted(os.listdir(os.path.join(self.tmp.name, "Programs"))),
                         ["EspDrop", "vOld"])


class WindowsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = FakeWinreg()
        self.runs = []

        def run(argv, **kw):
            self.runs.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        self.patches = [mock.patch.object(integration, "IS_WINDOWS", True),
                        mock.patch.object(integration, "winreg", self.reg),
                        mock.patch.dict(os.environ, {"APPDATA": self.tmp.name}),
                        mock.patch.object(integration.subprocess, "run", run)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_autostart_run_key(self):
        self.assertFalse(integration.autostart_enabled())
        integration.set_autostart(True)
        value = self.reg.keys[integration.RUN_KEY]["EspDrop"]
        self.assertTrue(value.endswith("gui --background"), value)
        self.assertTrue(integration.autostart_enabled())
        integration.set_autostart(False)
        self.assertNotIn("EspDrop", self.reg.keys[integration.RUN_KEY])
        self.assertFalse(integration.autostart_enabled())

    def test_icon_and_refresh(self):
        ico = os.path.join(self.tmp.name, "EspDrop", "espdrop.ico")
        with mock.patch.object(integration, "ICON", ico):
            integration.set_send_menu(True)
            (argv, kw), = self.runs
            self.assertEqual(kw["env"]["ESPDROP_ICON"], ico)
            self.assertIn("IconLocation", argv[-1])
            self.assertEqual(self.reg.keys[integration.MENU_KEY]["Icon"], ico)
            integration.set_autostart(True)
            # Nothing changed (and the shortcut exists): refresh leaves it alone.
            os.makedirs(os.path.dirname(integration._sendto_path()), exist_ok=True)
            open(integration._sendto_path(), "wb").close()
            integration.refresh()
            self.assertEqual(len(self.runs), 1)
        # A new icon (or a moved checkout): refresh rewrites the entries.
        with mock.patch.object(integration, "ICON", ico + "2"):
            integration.refresh()
            self.assertEqual(len(self.runs), 2)
            self.assertEqual(self.reg.keys[integration.MENU_KEY]["Icon"], ico + "2")

    def test_context_menu_without_powershell(self):
        # PowerShell blocked (policy): the right-click entry is there anyway,
        # the error still reaches the caller.
        def fail(argv, **kw):
            raise subprocess.CalledProcessError(1, argv)
        with mock.patch.object(integration.subprocess, "run", fail):
            self.assertEqual(len(integration.apply_defaults()), 1)
        self.assertEqual(self.reg.keys[integration.MENU_KEY][""], "Mit EspDrop senden")
        self.assertTrue(integration.send_menu_enabled())
        self.assertTrue(integration.autostart_enabled())

    def test_send_to_and_context_menu(self):
        integration.set_send_menu(True)
        (argv, kw), = self.runs                         # one PowerShell call for the .lnk
        self.assertEqual(argv[0], "powershell")
        env = kw["env"]
        self.assertEqual(env["ESPDROP_LNK"], os.path.join(
            self.tmp.name, "Microsoft", "Windows", "SendTo", "EspDrop (AirDrop).lnk"))
        self.assertTrue(env["ESPDROP_ARGS"].endswith("gui --send"), env["ESPDROP_ARGS"])
        self.assertNotIn(env["ESPDROP_LNK"], argv[-1])  # values via the environment only
        menu = self.reg.keys[integration.MENU_KEY]
        self.assertEqual(menu[""], "Mit EspDrop senden")
        cmd = self.reg.keys[integration.MENU_KEY + r"\command"][""]
        self.assertTrue(cmd.endswith('gui --send "%1"'), cmd)
        self.assertTrue(integration.send_menu_enabled())
        integration.set_send_menu(False)
        self.assertNotIn(integration.MENU_KEY, self.reg.keys)
        self.assertNotIn(integration.MENU_KEY + r"\command", self.reg.keys)
        self.assertFalse(integration.send_menu_enabled())


if __name__ == "__main__":
    unittest.main()
