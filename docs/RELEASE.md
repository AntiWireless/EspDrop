AirDrop zwischen iPhone, iPad, Mac und einem Windows- oder Linux-PC, mit einem
ESP32-C5 als AirDrop-Funkmodul am USB-Anschluss.

## Herunterladen

| Datei | Für |
|---|---|
| `EspDrop-…-windows-x64.exe` | Windows 10 und 11, 64 Bit |
| `EspDrop-…-linux-x86_64` | Linux, 64-Bit-PC (siehe unten) |
| `espdrop-esp32c5.bin` | die Firmware für den ESP32-C5 einzeln; sie steckt auch im Programm |
| `THIRD_PARTY-….txt` | was im Programm steckt, mit den Lizenzen |

Das Programm ist eine einzige Datei und braucht weder Python noch eine
Installation. Beim ersten Start entpackt es sich einmal (einige Sekunden),
danach startet es sofort.

## Windows

1. `EspDrop-…-windows-x64.exe` an einen festen Ort legen, z. B.
   `%LOCALAPPDATA%\Programs\EspDrop`, und doppelklicken. Autostart und das
   Rechtsklickmenü zeigen auf diesen Ort; nicht im Download-Ordner lassen.
2. Windows fragt einmal, weil das Programm nicht signiert ist: „Weitere
   Informationen“ → „Trotzdem ausführen“.

## Linux

Läuft auf 64-Bit-PCs (x86_64, kein ARM/Raspberry Pi) mit glibc 2.35 oder
neuer (`ldd --version` zeigt sie): Ubuntu 22.04 LTS und neuer samt Kubuntu,
Xubuntu, Linux Mint 21+, Pop!_OS 22.04+, Zorin OS 17+, elementary OS 7+;
Debian 12 und neuer; Fedora 36 und neuer; Arch, Manjaro, EndeavourOS, openSUSE
Tumbleweed. Nicht: Ubuntu 20.04, Linux Mint 20, Debian 11, RHEL/Alma/Rocky 9,
openSUSE Leap 15. X11 und Wayland gehen beide.

```sh
chmod +x EspDrop-*-linux-x86_64       # Downloads verlieren das Ausführrecht
./EspDrop-*-linux-x86_64
```

Für den ESP braucht der Benutzer die Gruppe `dialout` (Arch: `uucp`):
`sudo usermod -aG dialout $USER`, danach ab- und anmelden. Das Symbol im
Infobereich braucht einen Tray (KDE, Xfce, Cinnamon, Ubuntus GNOME; reines GNOME
nur mit der Erweiterung „AppIndicator“), sonst bleibt das Fenster offen.

## Der ESP32-C5

Den ESP32-C5 per USB anstecken und die App starten. Hat er noch nicht die
passende Firmware, bietet die App unter Allgemein → „ESP-Firmware“ das
Installieren an (über dasselbe USB-Kabel, ohne Tasten am ESP).

## Benutzen

- **Empfangen:** „Empfangen von: 10 Minuten“ oder „Für alle“; am iPhone teilen
  und den PC wählen. Dateien landen in Downloads.
- **Senden:** „Suchen“, Datei auf das Gerät ziehen, oder Rechtsklick auf eine
  Datei → „Mit EspDrop senden“. Am iPhone: AirDrop „Für alle“, entsperrt.

Quellcode und ausführliche Doku: dieses Repository (`host/README.md`,
`docs/SPIKES.md`). EspDrop steht unter der GPL-3.0.
