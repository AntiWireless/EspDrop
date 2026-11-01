# EspDrop - running as the compiled program (Nuitka, tools/build_app.py) or
# from the checkout with Python.
#
# Nuitka marks every compiled module with a global `__compiled__` (it does not
# set sys.frozen). Its process_exe is the program file; in a onefile build
# that is the unpacked copy, and containing_dir with original_argv0 (argv[0]
# as typed) name the binary the user started. sys.executable is no help: in
# a standalone folder it is the folder plus the build Python's name.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import os
import sys


def compiled():
    """True in the compiled program (or another freezer's, sys.frozen)."""
    return "__compiled__" in globals() or bool(getattr(sys, "frozen", False))


def onefile():
    """True in the single-file build (it unpacks itself; tools/build_app.py)."""
    info = globals().get("__compiled__")
    return bool(info is not None and getattr(info, "onefile", False))


def program():
    """The program file to start again (autostart, the file manager): the
    onefile binary itself rather than its unpacked copy. None from Python."""
    if "__compiled__" in globals():
        info = globals()["__compiled__"]
        folder = getattr(info, "containing_dir", None)
        argv0 = getattr(info, "original_argv0", None)
        if getattr(info, "onefile", False) and folder and argv0:
            name = os.path.basename(argv0)
            if os.name == "nt" and not os.path.splitext(name)[1]:
                name += ".exe"                 # started as "EspDrop" from a console
            return os.path.join(folder, name)
        exe = getattr(info, "process_exe", None)
        if exe:
            return exe
        return os.path.abspath(sys.argv[0])
    if getattr(sys, "frozen", False):
        return sys.executable
    return None


def program_dir():
    """The compiled program's folder (bundled libraries sit there), else None."""
    p = program()
    return os.path.dirname(p) if p else None


def bundle_dir():
    """Where the compiled program's own files are (Qt, the bundled
    libraries): its folder, or for a onefile build the folder it unpacked
    itself into. None from Python."""
    if not compiled():
        return None
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


UNPACK_PARENT = ("EspDrop", "app")   # tools/build_app.py: {CACHE_DIR}/EspDrop/app/<build>


def clean_old_unpacked():
    """A onefile build unpacks once per build into the cache; remove what
    older builds left there (one that still runs keeps its files: deleting
    them fails, they go next time)."""
    import shutil
    here = bundle_dir()
    if not here:
        return
    parent = os.path.dirname(here)
    if (os.path.basename(parent), os.path.basename(os.path.dirname(parent))) != \
            tuple(reversed(UNPACK_PARENT)):
        return                                 # not our cache folder: leave it alone
    for name in os.listdir(parent):
        path = os.path.join(parent, name)
        if not name.startswith("v") or not os.path.isdir(path):
            continue
        try:
            ours = os.path.samefile(path, here)
        except OSError:
            continue                           # cannot tell: leave it
        # Compared as the same folder, not as text: Windows may hand us the
        # short 8.3 form of our own path (V01~1.0-2), which once deleted the
        # running program's files.
        if not ours:
            shutil.rmtree(path, ignore_errors=True)
