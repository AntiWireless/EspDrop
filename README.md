# EspDrop

**AirDrop for Windows and Linux**, with an ESP32-C5 board on USB as the
radio. Send files from your PC to an iPhone, iPad or Mac, and receive from
them, the same way Apple devices do it among themselves: no app on the iPhone,
no cloud, no cable to the phone.

A PC's own Wi-Fi card cannot speak AWDL, the Wi-Fi link AirDrop runs on, and
Bluetooth alone is not enough either. EspDrop moves that part onto a small,
cheap ESP32-C5 board: its firmware runs AWDL and the Bluetooth signal that
wakes Apple devices, plus a small IPv6 stack; the desktop app does AirDrop
itself on top (based on OpenDrop) and talks to the board over its USB port.

## What you need

- An **ESP32-C5** board with USB, e.g. the Waveshare ESP32-C5-Zero (the one
  EspDrop is tested with). The app puts the firmware on it, over the same USB
  cable, no buttons or tools needed.
- A PC with **Windows 10 or 11** (64 bit) or **Linux** on a 64-bit PC (glibc
  2.35 or newer: Ubuntu 22.04+, Debian 12+, Fedora 36+, Linux Mint 21+, Arch,
  …). No administrator rights, driver or firewall rule.
- On the iPhone/iPad: AirDrop set to **Everyone**, unlocked.

## Install

1. Download the program for your system from
   [Releases](https://github.com/AntiWireless/EspDrop/releases):
   `EspDrop-…-windows-x64.exe` or `EspDrop-…-linux-x86_64`.
2. Start it (Linux: `chmod +x` it first). It installs itself – Windows into
   `%LOCALAPPDATA%\Programs\EspDrop` with a Start menu entry and an entry under
   *Installed apps*, Linux into `~/.local/bin` with an entry in the
   applications menu – and removes the download from the Downloads folder.
   Windows asks once because the program is not signed: *More info* → *Run
   anyway*.
3. Plug in the ESP32-C5. Under *General → ESP firmware* the app offers to
   install its firmware; that takes about a minute.

Starting a newer download later updates the installed program. *Uninstall*
in the app's *…* menu (or under *Installed apps* on Windows) removes it
again; received files stay.

On Linux the user needs access to the board's serial port: `sudo usermod
-aG dialout $USER` (Arch: `uucp`), then log out and in again.

## Use

- **Receive:** set *Receiving from* to *10 minutes* or *Everyone*; on the
  iPhone, share something and pick your PC. Every transfer asks first; files
  land in Downloads (optionally also on the clipboard).
- **Send:** *Search*, then drop files on a device or click it – or right-click
  files: *Send with EspDrop* in Explorer, *Open with → AirDrop (EspDrop)* in a
  Linux file manager.
- The app starts with your login and waits quietly in the notification area;
  the ESP's radio sleeps while nothing needs it and wakes when an Apple device
  nearby wants to share.
- English or German (*General → Language*), light or dark.

Folders cannot be sent yet, only files; Apple's *Contacts Only* mode is not
supported (it needs an Apple ID).

## Build it yourself

| part | where | how |
|---|---|---|
| firmware (ESP-IDF 6) | [`firmware/`](firmware/README.md) | `idf.py` builds, `tools/firmware_image.py` makes the image |
| desktop app (Python, PySide6, patched OpenDrop) | [`host/`](host/README.md) | `tools/opendrop_install.py`, then `tools/espdrop.py gui` |
| the single-file programs | [`tools/build_app.py`](tools/build_app.py) | Nuitka (Python → C) |

GitHub Actions ([`.github/workflows/build.yml`](.github/workflows/build.yml))
builds the firmware, both programs and runs the tests on every push; a tag
`v*` drafts a release. The project's notes, experiments and test plans (in
German) are in [`docs/`](docs/).

## Credits and license

EspDrop builds on the reverse engineering of AWDL and AirDrop by the
[Open Wireless Link](https://owlink.org) project of TU Darmstadt's Secure
Mobile Networking Lab: [OWL](https://github.com/seemoo-lab/owl) (AWDL) and
[OpenDrop](https://github.com/seemoo-lab/opendrop) (AirDrop), and on the
patches in [jedbillyb/airdrop-mt7921](https://github.com/jedbillyb/airdrop-mt7921)
that make OpenDrop work with current iOS.

GPL-3.0-or-later, see [LICENSE](LICENSE). EspDrop is an interoperability
project and is not affiliated with or endorsed by Apple. AirDrop, iPhone,
iPad and Mac are trademarks of Apple Inc.
