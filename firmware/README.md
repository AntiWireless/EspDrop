# EspDrop firmware

ESP-IDF firmware for the ESP32 that acts as the AWDL radio for EspDrop.

## Layout

```
spikes/                    hardware spikes + AWDL node (ESP32-C3, ESP32-C5)
  components/awdl/         platform-independent AWDL frame build/parse
    include/               awdl_frame.h, awdl_wire.h
    awdl_frame.c
    test/host/             native unit test (no ESP-IDF): `make -C ... run`
  main/                    ESP-IDF glue: radio.c, console.c, app_main.c
  CMakeLists.txt, sdkconfig.defaults
```

The `components/awdl` code has **no ESP-IDF dependency** and is unit tested on
the host, so the protocol logic can be developed and checked without hardware.
The `main/` code is the only part that calls `esp_wifi`.

## Build

```sh
. $IDF_PATH/export.sh          # ESP-IDF >= 5.5, tested with 6.0.3
cd spikes
# one build dir per chip, target always given (do not use `idf.py set-target`)
idf.py -B build_c3 -DSDKCONFIG=build_c3/sdkconfig -DIDF_TARGET=esp32c3 -p /dev/ttyACM0 build flash monitor
idf.py -B build_c5 -DSDKCONFIG=build_c5/sdkconfig -DIDF_TARGET=esp32c5 -p /dev/ttyACM0 build flash monitor
# bridge firmware: its own build dir, e.g. for the C5
idf.py -B build_c5_bridge -DSDKCONFIG=build_c5_bridge/sdkconfig -DIDF_TARGET=esp32c5 \
  -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.bridge.defaults" -p /dev/ttyACM0 build flash
```

`sdkconfig.defaults.esp32c5` is picked up automatically for the C5.

The firmware's version is the host app's (`host/espdrop/version.py`, read by
`CMakeLists.txt` as `PROJECT_VER`); the bridge answers `version` with
`version 0.1.0 idf=v6.0.3 chip=esp32c5 rev=1.0 proto=1`, so the app knows
whether the ESP runs the firmware it ships. The app can also flash it (no IDF
needed, esptool over the same USB port): `espdrop flash` takes the newest
`build*` of the bridge here, or one image made with
`python tools/firmware_image.py spikes/build_c5_bridge` (bootloader,
partition table and app merged, written from 0).

The bridge firmware includes Bluetooth LE (NimBLE): the broadcaster role for
the AirDrop wake (`wake`), and the observer role so it can listen for AirDrop
adverts and let the Wi-Fi radio sleep until a device nearby wants to share
(`power listen`, docs/SPIKES.md). It uses the large single-app partition table.
ESP-IDF does not apply changed defaults to an existing `sdkconfig`, so after
pulling a change to `sdkconfig.bridge.defaults` (the observer role was one)
delete the build directory (e.g. `rm -rf build_c5_bridge`) before building
again.

## Host unit test

```sh
make -C spikes/components/awdl/test/host run
```

See [`../docs/SPIKES.md`](../docs/SPIKES.md) for the M0 test plan and
[`../docs/UEBERBLICK.md`](../docs/UEBERBLICK.md) for the overall design.

## Provenance / licence

The AWDL frame layouts, TLV numbers and timing come from the Open Wireless Link
project's reverse engineering and from OWL (https://github.com/seemoo-lab/owl).
This firmware is GPL-3.0-or-later, matching OWL. It is an interoperability
project and is not affiliated with or endorsed by Apple.
