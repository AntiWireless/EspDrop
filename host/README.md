# EspDrop host app (M4)

AirDrop on Windows and Linux through an ESP32-C5/C3 that runs its own IPv6
stack on the AWDL link. Design: [`docs/M4_ENTWURF.md`](../docs/M4_ENTWURF.md);
how to test each stage: [`docs/SPIKES.md`](../docs/SPIKES.md).

| module | role |
|---|---|
| `espdrop/link.py` | USB framing and message formats (shared with `tools/espdrop_link.py`) |
| `espdrop/transport.py` | serial port: pyserial, or a raw POSIX tty without it |
| `espdrop/device.py` | HELLO, commands, UDP sockets on the device |
| `espdrop/proxy.py` | TCP connections over the device, with credits, as local sockets |
| `espdrop/mdns.py` | AirDrop browser and responder over the device's UDP |
| `espdrop/airdrop.py` | `send`: the patched OpenDrop client over the proxy |
| `espdrop/receive.py` | `receive`: the patched OpenDrop server on loopback |
| `espdrop/env.py` | platform setup: libarchive, TLS certificate, Windows fixes, `check` |
| `espdrop/session.py` | one long-running link for a GUI: receive windows, sends alongside, events, reconnect |
| `espdrop/gui.py` | the desktop app (PySide6) on the session |
| `espdrop/flash.py` | the ESP's firmware from the app: images checked, then esptool |
| `espdrop/version.py` | the version of the app and of the firmware it ships |
| `espdrop/cli.py` | `find`, `send`, `receive`, `status`, `cmd`, `check`, `flash`, `gui` |
| `tests/` | simulated devices with iPad-shaped mDNS answers, end-to-end via OpenDrop |

`receive` asks before each transfer (`y`/`n` within 60 s, else declined;
`--yes` accepts all, `-d DIR` picks the folder). `find` shows each receiver's
name from its `/Discover` answer; `send` takes `-r NAME` (or id, or part of
the name) and lists the receivers to choose from when it finds several.

Only one program can own the serial port: stop `tools/awdl_bridge.py` first.

## Desktop app

`espdrop gui` (Windows: `.\tools\espdrop-gui.cmd`, no console window) opens
one window that owns the ESP for as long as it runs. It is drawn in the look of
Apple's own apps (`espdrop/ui.py`: rounded cards, switches, segmented
controls, round device avatars), with no image or font files: text uses the
system face (Segoe UI Variable on Windows 11, Inter or the desktop font on
Linux), the icon is drawn in code.

- **AirDrop:** "Empfangen von: Aus / 10 Minuten / Für alle", as on the iPhone;
  the name others see; where received files go (default: the Downloads folder
  itself). Every incoming transfer asks first, in a floating card (picture,
  sender, files; Ablehnen / Annehmen; no answer in 60 s = declined). Nothing
  gets overwritten: OpenDrop unpacks into a hidden folder
  (`Downloads/.espdrop-incoming`), and each file then moves next to the others
  under a free name (`IMG_1.JPG`, `IMG_1 (1).JPG`). "In die Zwischenablage
  kopieren" (off by default) also puts what arrived on the clipboard: files to
  paste in Explorer or the file manager, a single photo also as a picture, a
  link as text.
- **Geräte in der Nähe:** "Suchen" (60 s, with the ESP's Bluetooth wake) shows
  the devices as avatars with their names; drop files on one or click it. A
  ring around the avatar shows the wait for Accept and the progress.
- **Übertragungen:** progress, cancel, show in folder.
- The ESP's radio is only on while receiving, searching or sending (firmware
  with the sleep mode, docs/SPIKES.md); the line under the title says which.
- Closing the window keeps it in the tray; "Beenden" (the "…" button or the
  tray) ends it. Unplugging the ESP is fine: it reconnects when it comes back.
- **Allgemein → "Erscheinungsbild": Automatisch / Hell / Dunkel.** Automatisch
  follows the system; the choice also sets the title bar on Windows (Qt 6.8+).
- **Allgemein → "Beim Anmelden im Hintergrund starten"** (Windows:
  `HKCU\…\CurrentVersion\Run`; Linux: `~/.config/autostart`), on by default.
  It then starts as `espdrop gui --background`: tray only, visible to everyone
  until switched off, and only an incoming transfer brings up its card; a
  notification says when files arrived (click: open them).
- **Allgemein → "Im Rechtsklickmenü des Explorers anbieten"**, on by default,
  adds "Mit EspDrop senden" and "Senden an → EspDrop (AirDrop)" (on Windows 11
  under "Weitere Optionen anzeigen"); on Linux "Öffnen mit → AirDrop
  (EspDrop)". Either opens the AirDrop sheet: the devices a search finds, one
  click sends.
- **Defaults and the icon:** the very first start switches both entries on
  (once; switched off they stay off). Every start writes the icon to
  `%LOCALAPPDATA%\EspDrop\espdrop.ico` (Linux:
  `~/.local/share/espdrop/espdrop.png`) for the menu entries and points them
  at this copy of the app again, should it have moved. On Windows the app
  sets its own taskbar identity (AppUserModelID `EspDrop.EspDrop`), so the
  taskbar shows our icon instead of Python's.
- **One copy per user:** a second start (autostart, the file manager, the
  launcher) hands its files to the running app over a local socket and exits.
- **Allgemein → "ESP-Firmware"** shows what the ESP runs (the firmware's
  `version` command) and offers "Aktualisieren …" when the app ships a newer
  one; an ESP that does not answer (no or other firmware) gets "Firmware
  installieren …" under the title. The dialog writes the firmware over the
  same USB cable, no buttons on the ESP: the app lets go of the port, esptool
  resets the C5 into its ROM bootloader, writes, verifies (MD5) and restarts
  it, and the app connects again. "Andere Datei …" takes an image or an
  ESP-IDF build folder. The same from the command line: `espdrop flash
  [IMAGE]`.

It needs PySide6 in the venv: `tools/opendrop_install.py` installs it; for a
venv made by `opendrop_install.sh`, run `python3 tools/opendrop_install.py`
once (it only adds what is missing) or `~/opendrop-venv/bin/pip install PySide6`.

## Linux

The OpenDrop venv from `tools/opendrop_install.sh` (needs python3, git,
openssl, libarchive). pyserial is optional; without it the app uses the tty
directly. No root: only access to `/dev/ttyACM0` (group `dialout`, `uucp` on
Arch).

```sh
~/opendrop-venv/bin/python tools/espdrop.py check
~/opendrop-venv/bin/python tools/espdrop.py find
~/opendrop-venv/bin/python tools/espdrop.py flash      # the firmware onto the ESP
~/opendrop-venv/bin/python -m unittest discover -s host/tests
```

## The firmware the app ships

`espdrop flash` and the app take, in this order: `ESPDROP_FIRMWARE` (an
image or a build folder), `espdrop/firmware/espdrop-esp32c5.bin` (what
release builds ship), or the newest ESP-IDF build of the bridge for the C5 in
`firmware/spikes/build*` (a build of the test console is refused). Before
anything is written the image is checked: ESP image headers for one chip, the
bootloader where that chip's ROM looks (0x2000 on the C5), the partition table
at 0x8000, the app with its description (version, build date, IDF) at
0x10000; the ESP must turn out to be that chip. `tools/firmware_image.py
BUILD` makes the one-file image from a build (esptool's merge-bin, checked the
same way).

## The app as a program (Nuitka)

`tools/build_app.py` makes the app a native program that needs no Python:
Nuitka translates the Python code (ours, the patched OpenDrop and every
library they use) to C, compiles it, and puts it with the Python runtime, Qt
and the ESP firmware into one folder, `build/app/EspDrop/`, packed as
`build/EspDrop-<version>-windows-x64.zip` or `-linux-x86_64.tar.gz`. The
program `EspDrop(.exe)` is the whole app: without arguments the window, with
arguments the command line (`EspDrop find`, `EspDrop flash`, ...; on Windows
it uses the console it was started from and opens none of its own).

```sh
~/opendrop-venv/bin/pip install nuitka patchelf          # Linux
~/opendrop-venv/bin/python tools/build_app.py --firmware espdrop-esp32c5.bin --smoke
```
```bat
%USERPROFILE%\opendrop-venv\Scripts\pip install nuitka
%USERPROFILE%\opendrop-venv\Scripts\python tools\build_app.py --zig --smoke
```

It must run with the venv of `opendrop_install.py` (it checks the OpenDrop is
the patched one). Windows needs a C compiler: Visual Studio or its Build Tools
with C++, or `--zig` (Nuitka downloads Zig). `--firmware` takes an image
(`tools/firmware_image.py`), `--firmware-build` an ESP-IDF build folder;
without either it takes what `espdrop flash` would, else it builds without
firmware. `--smoke` starts the result: `--version`, `check` (OpenDrop, its
root certificate, libarchive, esptool, Qt) and the window offscreen.

The program finds its parts next to itself (the firmware as
`espdrop/firmware/espdrop-esp32c5.bin`). On Linux it uses the system's
libarchive and carries `libarchive.so.13` from the build machine as a
fallback. At its first start it puts its entries where the desktop looks
(autostart, the right-click menu, on Linux also the application menu), all
pointing at the program itself; a moved folder is followed at the next start.

**Builds on GitHub** (`.github/workflows/build.yml`), on every push that
touches the code: the firmware with ESP-IDF 6.0.3 (container
`espressif/idf:v6.0.3`) merged into one image; the app with Nuitka on Windows
and on Ubuntu 22.04 (its glibc is the oldest the Linux build needs), the
firmware inside, each started with `--smoke`; the host and firmware tests. The
files are on the run's page (Artifacts). A tag `v*` drafts a release with
them.

The Windows program is not signed: SmartScreen asks once ("Weitere
Informationen" → "Trotzdem ausführen").

## Windows 10/11

Needs Python 3.9+ from python.org (64 bit, with the `py` launcher) and this
checkout; no git, openssl, admin rights, driver or firewall rule. The ESP's
USB-Serial/JTAG shows up as a COM port with Windows' own driver.

```bat
py tools\opendrop_install.py
.\tools\espdrop.cmd check
.\tools\espdrop.cmd status
.\tools\espdrop.cmd find
.\tools\espdrop.cmd send C:\path\photo.jpg
.\tools\espdrop.cmd receive
.\tools\espdrop-gui.cmd
```

`opendrop_install.py` builds `%USERPROFILE%\opendrop-venv`: OpenDrop 0.13.0 from
PyPI, the 17 airdrop-mt7921 patches (downloaded at the pinned revision; every
patch and every resulting file is checked against pinned digests, so the code
is byte-identical to the Linux install), pyserial, cryptography (the TLS
certificate, which OpenDrop would otherwise make with openssl), PySide6
(the desktop app) and esptool (the ESP's firmware from the app). It ends with
`espdrop check`. `espdrop.cmd` runs the app with that venv (`OPENDROP_VENV`
picks another) and works in cmd and PowerShell. Received files land in the
current folder. The port is found by its Espressif USB id, else `-p COM5`.

**libarchive** (OpenDrop packs and unpacks every transfer with it) has no
official Windows build. The app tries, in order: `LIBARCHIVE` (path of a DLL),
the DLL recorded with `opendrop_install.py --libarchive FILE`, an `archive.dll`
on `PATH`, and Windows' own `C:\Windows\System32\archiveint.dll`. `check`
packs, reads and unpacks a test archive with it. If none works, take the one
from MSYS2:

```bat
winget install MSYS2.MSYS2
C:\msys64\usr\bin\bash -lc "pacman -S --noconfirm mingw-w64-ucrt-x86_64-libarchive"
py tools\opendrop_install.py --libarchive C:\msys64\ucrt64\bin\libarchive-13.dll
```

Known so far only from the code, to be confirmed on hardware: file names with
non-ASCII letters (`check` warns if libarchive mangles them).
