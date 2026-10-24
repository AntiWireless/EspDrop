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
| `espdrop/session.py` | one long-running link for a GUI: receiving always ready, sends alongside, events, reconnect |
| `espdrop/cli.py` | `find`, `send`, `receive`, `status`, `cmd`, `check` |
| `tests/` | simulated devices with iPad-shaped mDNS answers, end-to-end via OpenDrop |

`receive` asks before each transfer (`y`/`n` within 60 s, else declined;
`--yes` accepts all, `-d DIR` picks the folder). `find` shows each receiver's
name from its `/Discover` answer; `send` takes `-r NAME` (or id, or part of
the name) and lists the receivers to choose from when it finds several.

Only one program can own the serial port: stop `tools/awdl_bridge.py` first.

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
```

`opendrop_install.py` builds `%USERPROFILE%\opendrop-venv`: OpenDrop 0.13.0 from
PyPI, the 17 airdrop-mt7921 patches (downloaded at the pinned revision; every
patch and every resulting file is checked against pinned digests, so the code
is byte-identical to the Linux install), pyserial and cryptography (the TLS
certificate, which OpenDrop would otherwise make with openssl). It ends with
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
