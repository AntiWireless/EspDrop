# EspDrop - the version of the app and of the firmware it ships.
#
# The one place it is set: firmware/spikes/CMakeLists.txt reads it from here
# for the firmware's own version (esp_app_desc_t, the `version` command), so
# the app can tell whether the ESP runs the firmware it comes with.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

__version__ = "0.1.0"
