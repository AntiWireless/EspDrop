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
