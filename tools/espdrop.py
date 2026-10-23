#!/usr/bin/env python3
# EspDrop - start the M4 host app from a checkout, on Linux and Windows.
#
#   ~/opendrop-venv/bin/python tools/espdrop.py find
#   py tools\espdrop.py -p COM5 find          (Windows, venv with pyserial)
#
# The OpenDrop venv's python is needed for python-zeroconf (mDNS messages).
# Stop tools/awdl_bridge.py first: only one program can own the serial port.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "host"))
from espdrop.cli import main  # noqa: E402

sys.exit(main())
