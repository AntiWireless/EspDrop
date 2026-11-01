#!/usr/bin/env python3
# EspDrop - one image of the whole flash from an ESP-IDF build of the bridge,
# for the app to flash (release builds ship it as espdrop/firmware/
# espdrop-esp32c5.bin; `espdrop flash FILE` and the app's "Andere Datei …"
# take it too).
#
#   python tools/firmware_image.py firmware/spikes/build_c5_bridge
#   python tools/firmware_image.py BUILD -o host/espdrop/firmware/espdrop-esp32c5.bin
#
# The build is checked as the app checks it (bridge, not the test console;
# image headers for one chip; the app's version), merged with esptool's
# merge-bin (bootloader, partition table and app at their offsets, 0xFF in
# between, starting at 0), and the result is read back the same way.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "host"))

from espdrop import flash  # noqa: E402

CHIP_ARG = {5: "esp32c3", 23: "esp32c5"}


def make_image(build, output):
    """Merge the build into output; returns the image as flash.load reads it."""
    fw = flash.load(build)
    if len(fw.parts) < 2:
        raise flash.FirmwareError(f"{build} is already one image")
    from esptool.cmds import merge_bin
    data = merge_bin(fw.parts, CHIP_ARG[fw.chip_id], output=None)
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    tmp = output + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, output)
    merged = flash.load(output)
    if (merged.version, merged.chip_id) != (fw.version, fw.chip_id):
        raise flash.FirmwareError("the merged image does not read back as the build")
    for offset, part in fw.parts:              # every part where the ROM expects it
        if data[offset:offset + len(part)] != part:
            raise flash.FirmwareError(f"part at {offset:#x} differs in the merged image")
    return merged


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("build", help="ESP-IDF build folder of the bridge (flasher_args.json)")
    ap.add_argument("-o", "--output", default=flash.FIRMWARE_NAME,
                    help=f"image to write (default: {flash.FIRMWARE_NAME})")
    args = ap.parse_args(argv)
    try:
        fw = make_image(args.build, args.output)
    except (flash.FirmwareError, ImportError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"{args.output}: EspDrop {fw.version} for the {fw.chip}, built {fw.built}, "
          f"IDF {fw.idf}, {fw.size // 1024} KiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
