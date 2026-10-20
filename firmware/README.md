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
# bridge firmware: add -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.bridge.defaults"
```

`sdkconfig.defaults.esp32c5` is picked up automatically for the C5.

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
