# EspDrop - the entry of the compiled program (tools/build_app.py compiles
# this file). It sits next to the espdrop package on purpose: Python, and
# Nuitka when it compiles, look in the main script's folder first, and from
# tools/ `import espdrop` would find tools/espdrop.py instead of the package.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import sys

from espdrop.cli import main

sys.exit(main())
