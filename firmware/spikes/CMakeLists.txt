# EspDrop AWDL spikes (ESP32-C3, ESP32-C5). ESP-IDF >= 5.5 / 6.0.
cmake_minimum_required(VERSION 3.16)
# The firmware's version is the app's (host/espdrop/version.py): the app reads
# it back with the `version` command and knows whether the ESP runs the
# firmware it ships. Must be set before project.cmake (else git describe).
file(STRINGS "${CMAKE_CURRENT_LIST_DIR}/../../host/espdrop/version.py" _espdrop_version
     REGEX "^__version__ *= *\"[^\"]+\"")
string(REGEX REPLACE "^__version__ *= *\"([^\"]+)\".*" "\\1" PROJECT_VER "${_espdrop_version}")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(espdrop_spikes)
