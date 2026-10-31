# EspDrop - running as the compiled program (Nuitka, tools/build_app.py) or
# from the checkout with Python.
#
# Nuitka marks every compiled module with a global `__compiled__` (it does not
# set sys.frozen): containing_dir is the program's folder, original_argv0 the
# onefile binary's argv[0] (None in a standalone folder).
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
        folder = getattr(info, "containing_dir", None) or os.path.dirname(sys.executable)
        name = getattr(info, "original_argv0", None) or sys.executable
        return os.path.join(folder, os.path.basename(name))
    if getattr(sys, "frozen", False):
        return sys.executable
    return None


def program_dir():
    """The compiled program's folder (bundled libraries sit there), else None."""
    p = program()
    return os.path.dirname(p) if p else None
