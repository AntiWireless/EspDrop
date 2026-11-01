# EspDrop - the single-file program installs itself, like a real program.
#
# Started from anywhere else (the download), it copies itself to
#   Windows  %LOCALAPPDATA%\Programs\EspDrop\EspDrop.exe   (per user, no admin)
#   Linux    ~/.local/bin/espdrop
# and starts from there. On Windows it also gets a start menu entry and an
# entry in "Installed apps" whose "Uninstall" removes it again. A copy that is
# running already (an older version) is asked to quit first, so starting a new
# download is how one updates. The downloaded file is removed afterwards when
# it was in the Downloads folder.
#
# Only the single-file build does this (the folder build stays where it is),
# never a test run (ESPDROP_NO_DESKTOP) or a copy started with
# ESPDROP_PORTABLE=1.
#
# Uninstalling (the app's menu, "Installed apps", `EspDrop uninstall`) removes
# the program, its entries, its settings and its unpacked copies; received
# files stay.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import hashlib
import os
import shutil
import subprocess
import sys
import time

from . import frozen, integration
from .version import __version__

IS_WINDOWS = os.name == "nt"
APP = "EspDrop"


def install_path():
    """Where the program lives once installed."""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return os.path.join(base, "Programs", APP, APP + ".exe")
    return os.path.join(os.path.expanduser("~"), ".local", "bin", "espdrop")


def _same(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def installed():
    """This program is the installed one."""
    p = frozen.program()
    return bool(p) and _same(p, install_path())


def wanted():
    """Whether this start should install the program first."""
    if not frozen.onefile():
        return False
    if os.environ.get("ESPDROP_NO_DESKTOP") or os.environ.get("ESPDROP_PORTABLE"):
        return False
    return not installed()


def _digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.digest()


def install(src, wait=20.0):
    """Copy the program src to its place (replacing an older one, which may
    take a moment to let go of its file after quitting) and register it.
    Returns the installed path; raises OSError."""
    dst = install_path()
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if not (os.path.exists(dst) and _digest(dst) == _digest(src)):
        tmp = dst + ".new"
        shutil.copy2(src, tmp)
        end = time.monotonic() + wait
        while True:
            try:
                os.replace(tmp, dst)
                break
            except PermissionError:            # Windows: the old one still runs
                if time.monotonic() > end:
                    os.remove(tmp)
                    raise
                time.sleep(0.5)
    if not IS_WINDOWS:
        os.chmod(dst, 0o755)
    integration.set_program(dst, __version__)
    return dst


def in_downloads(path, downloads):
    """path lies directly in the Downloads folder."""
    return bool(downloads) and _same(os.path.dirname(os.path.abspath(path)), downloads)


def remove_download(path, downloads, tries=20, pause=0.5):
    """Remove the downloaded program once it has quit (Windows keeps the file
    locked until then). Only from the Downloads folder; True when gone."""
    if not in_downloads(path, downloads):
        return False
    for _attempt in range(tries):
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(pause)
    return False


def unpack_folder():
    """The folder the single file unpacks its builds into, or None."""
    here = frozen.bundle_dir()
    if not here:
        return None
    parent = os.path.dirname(here)
    if (os.path.basename(os.path.dirname(parent)), os.path.basename(parent)) == \
            frozen.UNPACK_PARENT:
        return parent
    return None


def uninstall(exe=None):
    """Remove the program, its desktop entries and its unpacked copies.
    The settings are the caller's (QSettings). Returns what failed."""
    errors = integration.remove_all()
    data = integration.data_dir()
    shutil.rmtree(data, ignore_errors=True)
    exe = exe or install_path()
    unpacked = unpack_folder()
    if IS_WINDOWS:
        # A running program cannot remove its own file: a detached cmd does it
        # once we have quit.
        steps = [f'del /f /q "{exe}"', f'rmdir "{os.path.dirname(exe)}"']
        if unpacked:
            steps.append(f'rmdir /s /q "{unpacked}"')
        subprocess.Popen(
            'cmd /c "ping -n 4 127.0.0.1 >nul & ' + " & ".join(steps) + '"',
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True)
    else:
        try:
            if os.path.exists(exe):
                os.remove(exe)                 # a running file can go on Linux
        except OSError as e:
            errors.append(str(e))
        if unpacked:
            shutil.rmtree(unpacked, ignore_errors=True)
    return errors


def start(path, args):
    """Start the installed program with these arguments, detached."""
    subprocess.Popen([path, *args], close_fds=True,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                     if IS_WINDOWS else 0,
                     start_new_session=not IS_WINDOWS)


def argv_without(argv, flag, with_value=True):
    """argv without `flag` (and its value)."""
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
        elif a == flag:
            skip = with_value
        else:
            out.append(a)
    return out


if __name__ == "__main__":                     # pragma: no cover - a manual check
    print(install_path(), wanted(), installed(), file=sys.stderr)
