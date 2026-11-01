# EspDrop tests - the single-file program installs itself (install.py): the
# download is copied to the program's place, registered, started from there,
# removed from Downloads; uninstalling takes it all away again.
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
sys.path.insert(0, HERE)

from espdrop import install, integration  # noqa: E402
from test_integration import FakeWinreg  # noqa: E402


class InstallCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.downloads = os.path.join(self.home, "Downloads")
        os.makedirs(self.downloads)
        self.download = os.path.join(self.downloads, "EspDrop-0.1.0-linux-x86_64")
        with open(self.download, "wb") as f:
            f.write(b"\x7fELF the program" * 100)

    def onefile(self, program):
        return [mock.patch("espdrop.frozen.onefile", lambda: True),
                mock.patch("espdrop.frozen.program", lambda: program)]

    def started(self, patches):
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class LinuxInstallTest(InstallCase):
    def setUp(self):
        super().setUp()
        self.started([mock.patch.object(install, "IS_WINDOWS", False),
                      mock.patch.object(integration, "IS_WINDOWS", False),
                      mock.patch.dict(os.environ, {
                          "HOME": self.home,
                          "XDG_CONFIG_HOME": os.path.join(self.home, ".config"),
                          "XDG_DATA_HOME": os.path.join(self.home, ".local", "share")}),
                      mock.patch("os.path.expanduser",
                                 lambda p: p.replace("~", self.home, 1))])

    def test_install_and_remove_the_download(self):
        dst = install.install_path()
        self.assertEqual(dst, os.path.join(self.home, ".local", "bin", "espdrop"))
        self.started(self.onefile(self.download))
        with mock.patch.dict(os.environ, {"ESPDROP_NO_DESKTOP": ""}):
            self.assertTrue(install.wanted())
            self.assertEqual(install.install(self.download), dst)
            self.assertTrue(os.access(dst, os.X_OK))
            with open(dst, "rb") as f, open(self.download, "rb") as g:
                self.assertEqual(f.read(), g.read())
            # the installed copy: no second install
            with mock.patch("espdrop.frozen.program", lambda: dst):
                self.assertTrue(install.installed())
                self.assertFalse(install.wanted())
        # the download goes - from Downloads only
        elsewhere = os.path.join(self.tmp.name, "stick", "EspDrop")
        os.makedirs(os.path.dirname(elsewhere))
        open(elsewhere, "wb").close()
        self.assertFalse(install.remove_download(elsewhere, self.downloads))
        self.assertTrue(os.path.exists(elsewhere))
        self.assertTrue(install.remove_download(self.download, self.downloads, pause=0))
        self.assertFalse(os.path.exists(self.download))

    def test_not_for_tests_portable_or_folder_builds(self):
        self.started(self.onefile(self.download))
        for env in ({"ESPDROP_NO_DESKTOP": "1"}, {"ESPDROP_PORTABLE": "1"}):
            with mock.patch.dict(os.environ, env):
                self.assertFalse(install.wanted())
        with mock.patch("espdrop.frozen.onefile", lambda: False):
            self.assertFalse(install.wanted())

    def test_update_replaces_and_uninstall_removes(self):
        dst = install.install(self.download)
        newer = os.path.join(self.downloads, "EspDrop-0.2.0-linux-x86_64")
        with open(newer, "wb") as f:
            f.write(b"\x7fELF newer")
        install.install(newer)
        with open(dst, "rb") as f:
            self.assertEqual(f.read(), b"\x7fELF newer")
        with mock.patch("espdrop.frozen.program", lambda: dst):
            integration.ICON = None
            integration.set_launcher()
            integration.set_autostart(True)
        launcher = os.path.join(self.home, ".local", "share", "applications", "espdrop.desktop")
        self.assertTrue(os.path.exists(launcher))
        self.assertEqual(install.uninstall(dst), [])
        self.assertFalse(os.path.exists(dst))
        self.assertFalse(os.path.exists(launcher))
        self.assertFalse(integration.autostart_enabled())

    def test_argv_without(self):
        self.assertEqual(install.argv_without(["gui", "--installed-from", "/x", "--background"],
                                              "--installed-from"), ["gui", "--background"])


class WindowsInstallTest(InstallCase):
    def setUp(self):
        super().setUp()
        self.reg = FakeWinreg()
        self.runs = []

        def run(argv, **kw):
            self.runs.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        self.started([mock.patch.object(install, "IS_WINDOWS", True),
                      mock.patch.object(integration, "IS_WINDOWS", True),
                      mock.patch.object(integration, "winreg", self.reg),
                      mock.patch.object(integration.subprocess, "run", run),
                      mock.patch.dict(os.environ, {
                          "LOCALAPPDATA": os.path.join(self.home, "AppData", "Local"),
                          "APPDATA": os.path.join(self.home, "AppData", "Roaming")})])

    def test_program_in_installed_apps_and_start_menu(self):
        dst = install.install(self.download)
        self.assertEqual(dst, os.path.join(self.home, "AppData", "Local", "Programs", "EspDrop",
                                           "EspDrop.exe"))
        entry = self.reg.keys[integration.UNINSTALL_KEY]
        self.assertEqual(entry["DisplayName"], "EspDrop")
        self.assertEqual(entry["UninstallString"], subprocess.list2cmdline([dst, "uninstall"]))
        self.assertEqual((entry["NoModify"], entry["NoRepair"]), (1, 1))
        (argv, kw), = self.runs                   # the start menu shortcut
        self.assertTrue(kw["env"]["ESPDROP_LNK"].endswith(
            os.path.join("Start Menu", "Programs", "EspDrop.lnk")))
        self.assertEqual(kw["env"]["ESPDROP_EXE"], dst)
        # Uninstall: the entries go now, the running file after we quit.
        spawned = []
        with mock.patch.object(install.subprocess, "Popen",
                               lambda cmd, **kw: spawned.append(cmd)):
            self.assertEqual(install.uninstall(dst), [])
        self.assertNotIn(integration.UNINSTALL_KEY, self.reg.keys)
        (cmd,), = [spawned]
        self.assertIn(f'del /f /q "{dst}"', cmd)
        self.assertIn("ping -n 4", cmd)


if __name__ == "__main__":
    unittest.main()
