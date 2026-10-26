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
| `espdrop/cli.py` | `find`, `send`, `receive`, `status`, `cmd`, `check`, `gui` |
| `tests/` | simulated devices with iPad-shaped mDNS answers, end-to-end via OpenDrop |

`receive` asks before each transfer (`y`/`n` within 60 s, else declined;
`--yes` accepts all, `-d DIR` picks the folder). `find` shows each receiver's
name from its `/Discover` answer; `send` takes `-r NAME` (or id, or part of
the name) and lists the receivers to choose from when it finds several.

Only one program can own the serial port: stop `tools/awdl_bridge.py` first.

## Desktop app

`espdrop gui` (Windows: `.\tools\espdrop-gui.cmd`, no console window) opens
one window that owns the ESP for as long as it runs:

- **Empfangen:** "Empfangsbereit machen" for 10 minutes, an hour or until
  switched off; others see the name set at the top. Every incoming transfer
  asks first (sender, files, thumbnail; no answer in 60 s = declined). Files go
  to the folder shown (default `Downloads/EspDrop`).
- **Senden:** "Geräte suchen" (60 s, with the ESP's Bluetooth wake) lists the
  receivers by name; drop files on one, or pick one and "Dateien senden …".
- **Übertragungen:** progress, cancel and open via the context menu.
- The ESP's radio is only on while receiving, searching or sending (firmware
  with the sleep mode, docs/SPIKES.md); the label top right says which.
- Closing the window keeps it in the tray; "Beenden" ends it. Unplugging the
  ESP is fine: it reconnects when it comes back.
- **Autostart:** menu EspDrop → "Beim Anmelden im Hintergrund starten"
  (Windows: `HKCU\…\CurrentVersion\Run`; Linux: `~/.config/autostart`). It
  then starts as `espdrop gui --background`: tray only, receiving until
  switched off, and only an incoming transfer brings up its dialog; a
  notification says when files arrived (click: open them).
- **Sending from the file manager:** menu EspDrop → "Im Explorer-Menü
  anbieten" adds "Senden an → EspDrop (AirDrop)" and "Mit EspDrop senden" to
  the right-click menu (on Windows 11 under "Weitere Optionen anzeigen"); on
  Linux "Öffnen mit → AirDrop (EspDrop)". Either opens "An wen senden?": the
  devices a search finds, one click sends. Windows' own share sheet ("Teilen")
  only lists packaged apps (MSIX); that comes with a packaged build.
- **One copy per user:** a second start (autostart, the file manager, the
  launcher) hands its files to the running app over a local socket and exits.

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
~/opendrop-venv/bin/python -m unittest discover -s host/tests
```

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
certificate, which OpenDrop would otherwise make with openssl) and PySide6
(the desktop app). It ends with
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
