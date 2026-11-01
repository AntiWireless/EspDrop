AirDrop between iPhone, iPad, Mac and a Windows or Linux PC, with an
ESP32-C5 board on USB as the AirDrop radio. See the
[README](https://github.com/AntiWireless/EspDrop#readme) for what you need.

## Downloads

| file | for |
|---|---|
| `EspDrop-…-windows-x64.exe` | Windows 10 and 11, 64 bit |
| `EspDrop-…-linux-x86_64` | Linux on a 64-bit PC with glibc 2.35 or newer (Ubuntu 22.04+, Debian 12+, Fedora 36+, Linux Mint 21+, Arch, …) |
| `espdrop-esp32c5.bin` | the ESP32-C5 firmware on its own (the program has it inside) |
| `THIRD_PARTY-….txt` | what the programs contain, with their licenses |

## Install

Start the program (Linux: `chmod +x` first). It installs itself – Windows:
`%LOCALAPPDATA%\Programs\EspDrop`, Start menu, *Installed apps*; Linux:
`~/.local/bin/espdrop` and the applications menu – and removes the download
from the Downloads folder. Windows asks once because the program is not
signed: *More info* → *Run anyway*. Starting a newer version later updates it.

Plug in the ESP32-C5; *General → ESP firmware* installs the firmware over the
USB cable. On Linux the user needs the group `dialout` (Arch: `uucp`).

The app is in English and German (*General → Language*).

---

**Deutsch:** AirDrop zwischen iPhone, iPad, Mac und Windows- oder Linux-PC
über einen ESP32-C5 am USB-Anschluss. Programm herunterladen und starten: es
installiert sich selbst (Startmenü bzw. Anwendungsmenü) und entfernt den
Download. Den ESP anstecken, unter *Allgemein → ESP-Firmware* die Firmware
aufspielen. Die Sprache stellt man unter *Allgemein → Sprache* ein.
