# python -m espdrop ...  (with host/ on the path; tools/espdrop.py does that)
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors
import sys

from .cli import main

sys.exit(main())
