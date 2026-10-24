# M0 – Hardware-Spikes auf dem ESP32-C3 (und ESP32-C5)

Ziel von M0: herausfinden, ob der ESP32-C3 das kann, was AWDL braucht, **bevor**
der ganze Stack portiert wird. Der C3 funkt nur auf 2,4 GHz, also läuft hier
alles auf **Kanal 6** (dem 2,4-GHz-Sozialkanal von AWDL). Der ESP32-C5 kann
zusätzlich 5 GHz (AWDL-Kanäle 44/149); dieselbe Firmware läuft auf beiden, die
C5-spezifischen Tests stehen unten im Abschnitt „ESP32-C5“.

Die Firmware dazu liegt in `firmware/spikes/`. Sie bringt WLAN hoch und bietet
eine Konsole über den eingebauten USB-Serial/JTAG-Port.

## Bauen und Flashen

Ein eigenes Build-Verzeichnis pro Chip, Zielchip immer mit angeben
(**nicht** `idf.py set-target` verwenden, das verwirft die Build-Einstellungen):

```sh
# ESP-IDF >= 5.5 (getestet mit 6.0.3), einmalig: . $IDF_PATH/export.sh
cd firmware/spikes

# ESP32-C3
idf.py -B build_c3 -DSDKCONFIG=build_c3/sdkconfig -DIDF_TARGET=esp32c3 \
  -p /dev/ttyACM0 build flash monitor

# ESP32-C5 (z. B. Waveshare ESP32-C5-Zero: USB-C = nativer USB-Serial/JTAG).
# Beim ersten Mal mit erase-flash, damit keine alten WLAN-Einstellungen der
# Werks-Firmware im NVS liegen bleiben.
idf.py -B build_c5 -DSDKCONFIG=build_c5/sdkconfig -DIDF_TARGET=esp32c5 \
  -p /dev/ttyACM0 build erase-flash flash monitor
```

Die Bridge-Firmware (M2) entsprechend mit
`-DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.bridge.defaults"` und einem
eigenen Verzeichnis, z. B. `build_c5_bridge`.

In `monitor` (oder jedem seriellen Terminal) `help` eingeben. Beenden mit `Ctrl-]`.
Beide Chips melden sich als `ttyACM*` mit derselben USB-ID (303a:1001); stecken
beide, die Namen unter `/dev/serial/by-id/` verwenden.

## Konsolenbefehle

| Befehl | Zweck |
|---|---|
| `info` | Chip und Revision, eigene MAC, Kanal, Land, Band-Modus (C5), Sendeleistung, Promiscuous-Status |
| `mac <aa:bb:cc:dd:ee:ff>` | Stations-MAC setzen (für den ACK-Test) |
| `chan <n>` | Kanal wechseln: 1..13, auf dem C5 zusätzlich 36..48 und 149..173 |
| `rx on` / `rx off` | Promiscuous-Empfang (nur AWDL-BSSID) an/aus |
| `tx psf [n]` / `tx mif [n]` | n Action-Frames senden |
| `txdata <hex>` | einen AWDL-Datenframe an die AWDL-Broadcast-MAC senden |
| `peers` | gesammelte Peer-Ereignisse ausgeben (Name, RSSI, Kanalsequenz, Zeitstempel) |
| `stats` | RX/TX-Zähler, inkl. FCS- und Frame-Typ-Prüfung |
| `reset` | Zähler nullen |
| `rxprobe` | Längenfelder und letzte Bytes des letzten AWDL-Action-Frames (wo steht die FCS?) |
| `chanlat [n] [c1,c2,...]` | Latenz von `esp_wifi_set_channel` über n Wechsel durch die Kanalliste messen (Standard C3: `1,6,11`, C5: `6,44,149`); getrennt nach Wechseln im selben Band und zwischen den Bändern |
| `txpwr [q]` | Obergrenze der Sendeleistung anzeigen/setzen (0,25-dBm-Schritte, 80 = 20 dBm) |
| `usbbench [kib]` | Datenstrom für die USB-Durchsatzmessung auf dem Host |

Zum Mitschneiden brauchst du **zusätzlich** deine Linux-WLAN-Karte im
Monitor-Mode auf Kanal 6:

```sh
sudo ip link set wlan0 down
sudo iw dev wlan0 set type monitor
sudo ip link set wlan0 up
sudo iw dev wlan0 set channel 6
sudo tcpdump -i wlan0 -w /tmp/awdl.pcap 'wlan addr3 00:25:00:ff:94:73'
```

Der BSSID-Filter `00:25:00:ff:94:73` ist die feste AWDL-BSSID; so siehst du nur
AWDL-Verkehr. **Mitschnitte enthalten MAC-Adressen und Gerätenamen von Leuten in
der Nähe – nicht committen** (die `.gitignore` blockt `*.pcap` und `*.log`).

## Die fünf Spikes

### Spike 1 – Sendet der C3 gültige AWDL-Frames?
1. `tx mif 5` auf dem C3.
2. Im `tcpdump`/Wireshark auf dem Laptop nachsehen: Erscheinen die Frames?
   Wireshark hat einen AWDL-Dissektor – prüfen, ob er Sync-Params, Kanalsequenz,
   ARPA-Name (`espdrop-c3`, auf dem C5 `espdrop-c5`) und Version sauber dekodiert.
3. Querprobe gegen den Host-Test: `firmware/spikes/components/awdl/test/host`
   gibt denselben MIF als Hexdump aus.

**Erfolg:** Wireshark erkennt die Frames als wohlgeformtes AWDL.

### Spike 2 – Empfängt und versteht der C3 AWDL von einem iPhone?
1. Auf dem iPhone AirDrop auf **„Jeder für 10 Minuten"**, Teilen-Dialog öffnen
   (das weckt AWDL).
2. `rx on`, dann mehrfach `peers`.
3. Erwartung: Das iPhone taucht als Peer auf, mit Name und einer Kanalsequenz,
   die auf 2,4 GHz einen Slot auf Kanal 6 enthält.

**Erfolg:** `peers` zeigt das iPhone mit korrekt dekodierter Kanalsequenz.
**Risiko:** Auf 2,4 GHz bietet das iPhone Kanal 6 evtl. nur in 1 von 16 Slots an
– Empfang kann selten sein. Mit `stats` beobachten, wie viele Action-Frames
ankommen.

### Spike 3 – ACKt der C3 Unicast an die eigene MAC, und sieht er die Frames? (wichtigstes Risiko)
Hintergrund: Ein Forenbericht sagt, der ESP liefere ge-ACKte Frames an die
eigene MAC nicht an den Sniffer. Dieser Test klärt das.

- **Variante A (AWDL-MAC = Interface-MAC):** Standard. Ein anderes Gerät (Mac/
  iPhone oder ein zweiter ESP) sendet Unicast an die C3-MAC.
  - `stats` → steigt `tx_done_ok`? Kommen die Frames im `rx_*`-Zähler an?
  - Auf dem Mitschnitt des Laptops prüfen, ob der C3 ACKs sendet.
- **Variante B (AWDL-MAC ≠ Interface-MAC):** mit `mac` eine andere MAC setzen,
  dann als AWDL-Adresse eine dritte verwenden. Erwartung: Frames kommen im
  Callback an, aber es gibt keine Hardware-ACKs.

**Erfolg:** Klarheit, ob Variante A ACKt *und* zustellt. Davon hängt ab, welche
Architektur der Daten-Pfad später bekommt.

### Spike 4 – Kanalwechsel-Latenz und Zeitbezug
1. `chanlat 200` → Mittel- und Maximalwert von `esp_wifi_set_channel`.
2. Bei einem empfangenen Peer `rx_ts` (Hardware-Empfangszeit) mit `esp_timer`
   vergleichen – brauchen wir für die AWDL-Synchronisation (M1).

**Erfolg:** Latenz klein gegen ein AW (16 TU ≈ 16 ms). MT7921 lag bei ~8,5 ms;
der C3 ist nicht offloaded, hoffentlich schneller.

### Spike 5 – USB-Durchsatz
1. Auf dem Host mitschreiben: `python3 tools/usb_throughput.py /dev/ttyACM0`
   (oder schlicht `time cat /dev/ttyACM0 >/dev/null` währenddessen).
2. Auf dem C3 `usbbench 512`.

**Erfolg:** Deutlich über dem AWDL-Realdurchsatz (50–250 kB/s), damit USB nicht
zum Flaschenhals wird.

## ESP32-C5: Tests für 5 GHz

Gleiche Firmware, gleicher Ablauf wie oben. Der C5 startet auf Kanal 6 und kann
mit `chan` auf 5 GHz wechseln. Die Firmware setzt Land `DE` (menuconfig →
EspDrop) und begrenzt die Sendeleistung ab Kanal 149 auf 11 dBm, weil
5725–5875 MHz in der EU nur 25 mW EIRP erlaubt. Kanal 44 ist nur für Innenräume
zugelassen. Die DFS-Kanäle 52–144 lehnt die Firmware ab (Senden bräuchte
Radarerkennung).

1. **Chip und USB.** `info` zeigt `chip: esp32c5 rev v1.0` (oder v1.2). ESP-IDF
   6.0.3 unterstützt v1.0 und v1.2; die Vorserien-Revision v0.1 startet nicht.
2. **Spikes 1–3 auf Kanal 6** wie beim C3. Zusätzlich in `stats`:
   `rx frame end: at sig_len-4` steigt mit `action`, alle anderen „frame end“-Zähler,
   `type_mismatch`, `ip6_short` und `ip6_long` bleiben 0. Dann stimmt die Frame-Länge.
   (`fcs_ok` steigt nur auf dem C3: der C5 liefert an der Stelle der FCS andere
   Bytes, dort zählt jeder Frame als `fcs_bad`. Gemessen, kein Fehler.)
3. **Welchen 5-GHz-Kanal nutzt dein iPhone?** Geht schon auf Kanal 6: `rx on`,
   am iPhone das Teilen-Menü öffnen, `peers`. In der Kanalsequenz steht 44 oder 149.
4. **Empfang auf 5 GHz.** `chan 44` (bzw. 149), `rx on`, Teilen-Menü offen,
   `peers` und `stats`: Das iPhone muss auftauchen, `action` steigen.
5. **Senden auf 5 GHz.** `chan 44`, `reset`, `tx mif 20`, `stats`:
   `tx ok` mindestens 20 (der Knoten sendet nebenbei eigene PSF/MIF, die
   zählen mit), `tx err` 0, `tx_done ok` steigt. Mitschnitt mit einer 5-GHz-fähigen Karte:
   `sudo iw reg set DE`, Monitor-Mode wie oben, `sudo iw dev wlan0 set channel 44`.
   Wireshark sollte saubere AWDL-Frames mit 6 Mbit/s zeigen.
6. **Kanalwechsel (entscheidend für 5 GHz).**
   `chanlat 200 6,6`, `chanlat 200 6,44`, `chanlat 200 6,149`,
   `chanlat 200 44,149`. Ein AWDL-Slot dauert 64 TU (≈ 65 ms); ein Wechsel um
   2 ms ist gut, unter 5 ms brauchbar (MT7921: 8,5 ms). Alle Zeilen der Ausgabe
   aufschreiben, auch den ersten Bandwechsel und eventuelle Fehler.
7. **USB-Durchsatz** wie Spike 5.
8. **Optional: Bridge auf 5 GHz.** Bridge-Firmware für den C5 flashen und wie in
   M2.4 pingen bzw. AirDrop testen. Der C5 wählt den 5-GHz-Kanal selbst: er folgt
   dem Master auf dessen bevorzugten Sozialkanal (44 oder 149). Mit `node` prüfen
   (`auto=on`, `chan=` der erwartete Kanal). `chan 44` pinnt einen festen Kanal
   (`auto` aus), `chan auto` schaltet die Automatik wieder ein. Der Knoten bleibt
   dabei auf einem Kanal und übernimmt dessen Slots beim Master, ohne Wechsel.

### Ergebnisse ESP32-C5 (Waveshare ESP32-C5-Zero, rev v1.0, 240 MHz, ESP-IDF 6.0.3)

- **Frame-Länge:** wie beim C3, Frame = `sig_len − 4` (TLV-Kette endet dort bei
  allen Frames). Die 4 Bytes sind auf dem C5 aber nicht die FCS, `dump_len` ist
  `sig_len + 4`; `fcs_bad` zählt daher jeden Frame – erwartet. IPv6-Längen stimmen.
- **iPhone in DE nutzt Kanal 44** (nicht 149). Sequenz im Leerlauf
  `6 0 44 0 0 0 0 0 6 0 44 0 …`, mit offenem Teilen-Menü
  `44 44 44 0 0 0 0 0 6 44 44 0 …`: 5 von 16 Slots auf 44, nur einer auf 6.
- **Kanalwechsel** (`esp_wifi_set_channel`, Mittel / p99): gleicher Kanal 0,70 /
  0,71 ms, innerhalb 5 GHz 0,92–1,03 / 1,01–1,15 ms, zwischen 2,4 und 5 GHz
  1,29–1,36 / 1,38–1,50 ms, erster Bandwechsel 1,7–2,4 ms. Ein Slot dauert ≈ 65 ms,
  ein Wechsel kostet also unter 3 % (MT7921: 8,5 ms). Gemessen ist die Dauer
  des API-Aufrufs, nicht die Zeit bis zum ersten Empfang.
- **Empfang und Senden auf Kanal 44** funktionieren: iPhone-Action- und
  Datenframes kommen an, eigene MIFs gehen ohne Fehler raus. Die Sendeleistung
  meldet der Treiber auf 44 mit 18 dBm.
- **Bridge auf Kanal 44** (automatisch gewählt): Ping zum iPhone 19/20, alle 20 Unicast-Frames
  vom iPhone per ACK bestätigt (C3 auf Kanal 6: ~66 %), RTT 4–440 ms (wir senden
  nur in den 44er-Slots des iPhones).
- **AirDrop-Empfang (M2.4) funktioniert:** Foto vom iPhone, 2,87 MB in 12,7 s
  (≈ 0,23 MB/s laut OpenDrop), korrekt entpackt.
- **Engpass ist USB:** Während der Übertragung war das iPhone in 15 von 16 Slots
  auf 44 und wir in allen dabei; verworfen wurde nur an der Warteschlange zum PC.
  `usbtest` (Bridge-Pfad über den Treiber) und `usbtest raw` (FIFO direkt per
  Polling) schaffen beide ~255 kB/s. Die Grenze liegt also am USB-Serial/JTAG-
  Port selbst (ein 64-Byte-Paket je Abholung durch den PC), nicht am Treiber;
  AirDrop nutzt davon schon ~90 %.
- **Nachmessung (2026-10-23, mit Bluetooth in der Firmware):** eine 38-MB-Datei
  in etwa 60 s, ~0,6 MB/s laut OpenDrop. Da jedes empfangene Byte über USB
  läuft, schafft der USB-Serial/JTAG-Weg mindestens das; `usbtest` (~255 kB/s)
  hat die Grenze unterschätzt, und die 2,87 MB oben waren zu klein (Aufbau und
  Annehmen zählen mit). Mit den Bluetooth-Defaults sank die Empfangs-
  Warteschlange auf 16 Plätze (`rxq=16`, ~700 `rx_drops`); mit verkleinerten
  NimBLE-Puffern und 24 KB Heap-Reserve für Wi-Fi waren es 425 Drops (~1,6 %
  der Frames) bei **gleicher** Geschwindigkeit. Die Drops begrenzen also nicht,
  sie sind TCPs Signal am Engpass; der liegt sehr wahrscheinlich am USB
  (Full-Speed, praktisch rund 1 MB/s). Mehr ginge nur über einen anderen Weg
  zum PC.

## Senden an ein iPhone (M3, PC -> iPhone)

Empfangen ist damit fertig. Für die Gegenrichtung muss das iPhone oder iPad als
Empfänger über AWDL antworten. Das tut es nur, wenn sein AWDL wach ist (offenes
Teilen-Menü) **und** ein Bluetooth-LE-Signal in der Nähe meldet, dass jemand
senden will. (Nachgewiesen im Referenzprojekt airdrop-mt7921, FINDINGS §27
und §37: gleiche Verbindung, gleiches offenes Menü, gefunden nur mit BLE.)

Das Wecksignal kommt vom Bluetooth des ESP selbst (Bridge-Firmware, Befehl
`wake`) oder vom Bluetooth des PCs (`tools/airdrop_wake.py`, braucht
`python3-dbus` und `python3-gi`, Fedora: `python3-dbus python3-gobject`).
`opendrop_send.sh` nimmt den ESP, wenn dessen Firmware Bluetooth hat, sonst den
PC; `--wake=esp`, `--wake=pc` oder `--wake=none` wählt fest. Der ESP sendet
genau dieselben Bytes wie das PC-Werkzeug, mit einer neuen zufälligen
BLE-Adresse je Wecken und immer mit Ablaufzeit (höchstens 600 s), damit ein
abgestürztes Skript keine Geräte in der Nähe dauerhaft weckt.

Bluetooth in der Bridge-Firmware: seit `sdkconfig.bridge.defaults` mit NimBLE
(nur Broadcaster) und der großen App-Partition (1,5 MB, das Image hat auf dem
C5 etwa 1,2 MB). ESP-IDF übernimmt neue Defaults nicht in ein schon
vorhandenes `sdkconfig`, also den Build-Ordner einmal löschen:
```sh
rm -rf build_c5_bridge
idf.py -B build_c5_bridge -DSDKCONFIG=build_c5_bridge/sdkconfig -DIDF_TARGET=esp32c5 \
  -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.bridge.defaults" -p /dev/ttyACM0 build flash
```
Im Bridge-Fenster zeigt `wake` dann `wake off` (ohne Bluetooth: `wake
unavailable`). Bluetooth teilt sich das Funkteil mit Wi-Fi und braucht Heap:
`stats` zeigt `rxq=` (Tiefe der Empfangs-Warteschlange, bisher 48) und `heap=`.

Die Bridge stellt für Werkzeuge den Steuer-Socket `/run/espdrop/awdl0.sock`
bereit (gehört dem Benutzer, der sie mit `sudo` gestartet hat).
`tools/espdrop_cmd.py wake 60` / `tools/espdrop_cmd.py wake off` /
`tools/espdrop_cmd.py node` schicken einen Befehl und zeigen die Antwort.

1. Bridge wie beim Empfang starten (`sudo tools/awdl_bridge.py /dev/ttyACM0`
   aus der eigenen Sitzung); der C5 wählt den 5-GHz-Kanal automatisch.
2. Senden (als normaler User), eine oder mehrere Dateien:
   ```sh
   tools/opendrop_send.sh foto.jpg
   ```
   Das Skript startet das BLE-Wecksignal, sucht den Empfänger auf `awdl0`,
   hält das Signal für Annehmen und Übertragung aufrecht und schaltet es am
   Ende ab.
3. Am iPhone/iPad: AirDrop auf „Für alle für 10 Minuten“ (fällt nach 10 Minuten
   auf „Nur Kontakte“ zurück, also kurz vor dem Senden neu wählen), entsperrt,
   **Teilen-Menü öffnen und offen lassen** (es hält AWDL wach; bei geschlossenem
   Menü meldet sich das Gerät nie als Empfänger). Bei der Abfrage „Annehmen“
   tippen.

Mehrere Apple-Geräte in Reichweite: mit `-r "Annas iPhone"` das Ziel am Namen
wählen. „Nur Kontakte“ statt „Jeder“: Identifikator über
`ESPDROP_WAKE_ARGS="--appleid du@icloud.com"` an das Wecksignal geben (gilt
für beide Quellen; `--phone`, `--email`, `--email2` ebenso).

Ergebnis (2026-10-23, C5-Bridge auf Kanal 44, BLE-Wecksignal vom PC): Senden an
ein iPad funktioniert, die Datei wird übertragen. Ping vom PC zum iPad bekommt
Antworten mit Teilen-Menü zu und offen, in beiden Sequenz-Modi (`seq mirror`
und `seq verbatim`); der Standard bleibt `mirror`. Am Vortag war dasselbe
iPad zwar per mDNS gefunden worden, hatte aber keinen einzigen Unicast an uns
geschickt (SYN-SENT, 100 % Ping-Verlust, obwohl `tx` jeden Frame als
quittiert zählte). Am nächsten Tag, nach neuem Flashen und Neustart von
Bridge und iPad-Sitzung, war das weg; die Ursache ist nicht geklärt.

Fehlersuche, wenn kein Empfänger gefunden wird (Gerät bereit, Wecksignal läuft):
- `tools/mdns_probe.py`: Kommen mDNS-Antworten des Geräts überhaupt bei einem
  Programm an? (Standardbibliothek, ohne venv.)
- `~/opendrop-venv/bin/python tools/zeroconf_probe.py`: dieselbe Suche wie
  `opendrop find` mit OpenDrops zeroconf. Zeigt pro Paket, was zeroconf auf
  welchem Socket bekam und ob es es lesen konnte, die Gruppenmitgliedschaft
  von `awdl0` und alle anderen Sockets auf UDP 5353, und endet mit einem
  Befund. Die Ausgabe enthält Gerätenamen und Adressen.
- Gefunden, aber `/Discover` hängt in SYN-SENT, und `ping -6` an das Gerät
  bleibt ohne Antwort, obwohl `tx` im Bridge-Fenster jeden Unicast als
  quittiert zählt (`uc_ack`): Das Gerät empfängt uns, sendet uns aber keinen
  Unicast. Zuerst alles neu starten: C5 abziehen und wieder anstecken, Bridge
  neu starten, am iPhone/iPad AirDrop einmal aus- und wieder einschalten (so
  verschwand es beim ersten Auftreten, siehe Ergebnis oben). Hält es an, die
  Sequenz prüfen: Im Referenzprojekt lag das an der angekündigten Kanal-Sequenz
  (airdrop-mt7921 FINDINGS §25/§26). Im Bridge-Fenster umschaltbar, ohne neu
  zu flashen: `seq mirror` (Standard: unser Kanal nur in den Slots des Masters
  auf diesem Kanal) oder `seq verbatim` (die Sequenz des Masters unverändert).
  Gesendet wird in beiden Fällen nur in den Slots auf unserem Kanal. `node`
  zeigt den Modus (`mode=`) und beide Sequenzen.

## M4.1: eigener IP-Stack auf dem ESP (IP-Betriebsart)

Entwurf: `docs/M4_ENTWURF.md`. Die Bridge-Firmware kennt zwei Betriebsarten:
`l2` (wie bisher: Frames an `awdl0` auf dem PC) und `ip` (der ESP beendet IPv6
selbst, lwIP auf der AWDL-Verbindung). Nach dem Start ist sie in `l2`;
`awdl_bridge.py` schaltet beim Start ausdrücklich auf `l2` (Meldung
`[hello] protocol 1, mode l2, caps 0x03`).

Test im Bridge-Fenster (`awdl0` bleibt dabei bestehen, bekommt in `ip` aber
nichts; Frames vom PC werden in `ip` verworfen, damit nicht zwei Stacks mit
derselben Adresse senden):
1. `mode ip`: Antwort `mode ip`.
2. `ip`: `up fe80::…` (unsere Link-Local, wie bisher auf `awdl0`), Zähler und
   die aus dem Verkehr gelernten Peers (`peers: fe80::…=mac(Alter)`).
   iPhone/iPad dafür wach halten (Teilen-Menü offen, gegebenenfalls `wake 60`).
3. `ping6 <adresse des iPads> 10`: zehn Echo-Requests vom ESP selbst; die
   Antworten erscheinen als `[device] ping6: reply …`, am Ende die Bilanz.
4. `ip` und `stats` noch einmal: Zähler, `heap=`.
5. `mode l2`: zurück; `awdl0` funktioniert wieder wie zuvor.

Ergebnis (2026-10-23, C5, iPad als Master auf Kanal 44): `ping6` vom ESP
10/10 beantwortet, 4–460 ms (Sägezahn: 1 s Ping-Abstand gegen 1,048 s
AWDL-Periode, jede Anfrage wartet auf den nächsten gemeinsamen Slot). Das iPad
wurde aus dem Verkehr gelernt (`learned=10`, `nd=0`). Speicher in `ip`:
`heap=30564`, `heap_min=24016`, weil die L2-Warteschlange (`rxq=40`, ~60 KB)
auch in `ip` belegt bleibt; für TCP (M4.3) muss sie in `ip` frei werden.

`ip`-Zähler: `rx` an lwIP übergeben, `not_ours` (an andere Stationen),
`nomem`/`full` (verworfen: kein Speicher / lwIP-Warteschlange voll), `tx`
gesendet, `fail` (AWDL-Sendewarteschlange voll), `learned` (Ziel aus der
gelernten Tabelle), `nd` (Ziel unbekannt, lwIPs Nachbarsuche), `mcast`.

## M4.2: Host-App, UDP über den ESP, `espdrop find`

Die Host-App liegt in `host/espdrop/` (Python, Linux und Windows). Sie übernimmt
die serielle Schnittstelle selbst; `tools/awdl_bridge.py` darf dabei **nicht**
laufen. Sie schaltet den ESP in die IP-Betriebsart (`HELLO`), öffnet über ihn
UDP-Port 5353 und sucht per mDNS nach AirDrop-Empfängern. Unter Linux braucht
sie kein root, nur Zugriff auf die Schnittstelle (Gruppe `dialout`, unter Arch
`uucp`), und kein Zusatzpaket; unter Windows `pip install pyserial`. Die
OpenDrop-venv wird für python-zeroconf (Lesen/Schreiben der mDNS-Nachrichten)
gebraucht.

In der IP-Betriebsart gibt die Firmware die L2-Warteschlange frei (`stats`:
`rxq=0`, `heap=` entsprechend höher); `awdl_bridge.py` legt sie beim Start
wieder an.

Test (Bridge beenden, Teilen-Menü am iPhone/iPad offen):
```sh
~/opendrop-venv/bin/python tools/espdrop.py status
~/opendrop-venv/bin/python tools/espdrop.py find -t 30
~/opendrop-venv/bin/python tools/espdrop.py find -t 30 -v   # jede AirDrop-mDNS-Nachricht
~/opendrop-venv/bin/python tools/espdrop.py cmd ip
```
`find` startet das BLE-Wecksignal des ESP für die Suchdauer, fragt alle 1,5 s
nach `_airdrop._tcp.local.` und meldet jeden Empfänger mit Kennung, Adresse,
Port und TXT-`flags`; am Ende schaltet es das Wecksignal ab. Der Port wird
automatisch gefunden (USB-Kennung von Espressif), sonst `-p /dev/ttyACM0`
bzw. `-p COM5`. Danach geht `awdl_bridge.py` wie gewohnt (es schaltet zurück
auf L2).

Ergebnis (2026-10-23, C5, iPad mit offenem Teilen-Menü, Wecksignal vom ESP):
`find` fand das iPad sofort (`found <id> [fe80::…]:8770 flags 111611`); in 30 s
21 Anfragen, 38 AirDrop-Antworten, keine unlesbar. Freier Heap in `ip`:
~89,5 KB (`rxq=0`), vorher ~30 KB mit belegter L2-Warteschlange.

Tests ohne Hardware (simulierter ESP mit iPad-Antworten):
```sh
~/opendrop-venv/bin/python -m unittest discover -s host/tests
```

## M4.3: senden über den ESP (`espdrop send`)

Senden läuft jetzt auch über den eigenen IP-Stack des ESP, ohne `awdl0`.
Die Host-App baut über den ESP eine TCP-Verbindung zum iPhone/iPad auf
(Socket-Proxy mit Kreditsteuerung) und fährt darauf das gepatchte OpenDrop
(TLS, `/Discover`, `/Ask`, `/Upload`) unverändert, nur mit ausgetauschter
Verbindungsschicht.

```sh
~/opendrop-venv/bin/python tools/espdrop.py send foto.jpg
~/opendrop-venv/bin/python tools/espdrop.py send -r "Annas iPhone" -n MeinPC a.pdf b.png
```
`send` startet das BLE-Wecksignal des ESP, sucht den Empfänger, baut die
Verbindung auf und überträgt; am iPad „Annehmen“ tippen. Bedingungen wie
immer: AirDrop „Für alle“, entsperrt, Teilen-Menü offen.

Ergebnis (2026-10-23, C5): Senden an ein iPad erfolgreich über `espdrop send`,
ohne `awdl0`/root/Firewall. `ip` danach: `tcp: opened=1 to_host=1137
from_host=146499 err=0` (eine Verbindung, ~146 KB Datei hin, ~1,1 KB HTTP
zurück), `learned=126`, `heap=74240` in der IP-Betriebsart.

Tests ohne Hardware: Zum simulierten ESP kommt ein TCP-überbrückender
(`TcpBridgeDevice`), der Verbindungen an einen echten, auf `127.0.0.1`
laufenden OpenDrop-Empfänger (`loopback_receiver.py`) weiterreicht. Der Test
`EndToEndTest.test_send_arrives` sendet so eine Datei komplett durch Proxy,
Kredite, TLS, `/Discover`, `/Ask` und `/Upload` und prüft, dass sie ankommt
(läuft im Container, wo IPv6 fehlt, weil der Proxy intern IPv4-Loopback nutzt).
```sh
~/opendrop-venv/bin/python -m unittest discover -s host/tests
```

## M4.4: empfangen über den ESP (`espdrop receive`)

Empfangen läuft jetzt ebenfalls über den eigenen IP-Stack des ESP. Die Firmware
nimmt eingehende AWDL-Verbindungen an (`TCP_LISTEN`/accept), die Host-App meldet
sich per mDNS als Empfänger an (Responder, alle 5 s, mit AAAA = Link-Local des
ESP aus der HELLO-MAC) und fährt den OpenDrop-Server auf `127.0.0.1`; jede
angenommene Verbindung wird dorthin durchgereicht.

```sh
~/opendrop-venv/bin/python tools/espdrop.py receive -n MeinPC
```
Am iPhone/iPad eine Datei teilen und „MeinPC“ wählen; die Datei landet im
aktuellen Verzeichnis. `awdl_bridge.py` darf dabei nicht laufen.

Tests ohne Hardware: Ein eingehend überbrückender Fake (`AcceptBridgeDevice`)
simuliert ein verbindendes Apple-Gerät; ein echter OpenDrop-Sender fährt
`/Discover`, `/Ask`, `/Upload` gegen den von `espdrop receive` gestarteten
Server. `ReceiveEndToEndTest` prüft, dass die Datei ankommt. Zusammen 20
Host-Tests.

Ergebnis (2026-10-23, C5, Linux): Senden und Empfangen funktionieren beide über
den eigenen IP-Stack des ESP.

## M4.5: Windows

Dieselbe Firmware, dieselbe Host-App. Unter Windows fehlt OpenDrop zweierlei,
was Linux mitbringt; beides erledigt jetzt die Host-App (`host/espdrop/env.py`):
- **TLS-Zertifikat:** OpenDrop erzeugt es beim ersten Start mit dem Programm
  `openssl`. Die Host-App legt es vorher mit python-cryptography an (gleiche
  Dateien in `~/.opendrop/keys`, RSA 2048, selbstsigniert, CN = Name). Ohne
  cryptography bleibt alles wie bisher (Linux).
- **libarchive:** Es gibt keine offizielle Windows-Version. Gesucht wird
  `LIBARCHIVE`, dann die beim Installieren angegebene DLL, dann `archive.dll`
  im `PATH`, dann Windows' eigene `System32\archiveint.dll`. Ob die genügt,
  ist offen; `espdrop check` probiert es aus. Sonst die DLL aus MSYS2
  (`host/README.md`).

Dazu eine Windows-Eigenheit beim Senden: OpenDrop baut den Namen der Datei im
Archiv und `FileBomPath` in `/Ask` mit `os.path.join(".", name)`, unter Windows
also `.\name`. Unter Windows schreibt die Host-App die Archiv-Einträge deshalb
selbst (Name mit `/`, Dateidaten aus Python, Kopf wie bei OpenDrop unter Linux:
normale Datei, Größe, Änderungszeit, ein Link, Inode 1) und setzt in `/Ask`
ebenfalls `./name`.

Installation ohne bash, git und openssl: `tools/opendrop_install.py` macht
dasselbe wie `opendrop_install.sh` (OpenDrop 0.13.0 + 17 Patches aus
airdrop-mt7921, Stand 27edf28) und legt pyserial und cryptography dazu. Die
Patches wendet es selbst an; jeder Patch und jede fertige Datei wird gegen
feste Prüfsummen der Linux-Installation geprüft, unter Windows läuft also
byte-genau derselbe OpenDrop-Code. Am Ende ruft es `espdrop check` auf.

Test unter Windows 10/11 (Python 3.9+ von python.org, 64 Bit; dieses Repo als
Checkout oder ZIP):
1. In cmd oder PowerShell im Repo-Ordner: `py tools\opendrop_install.py`.
   Es endet mit der Ausgabe von `espdrop check`, am besten mit `ready.`.
   Interessant ist die Zeile `libarchive` (welche DLL) und `archive`.
2. ESP mit der Bridge-Firmware anstecken (dieselbe wie unter Linux). Im
   Geräte-Manager erscheint ein COM-Port („USB Serial Device“, ohne eigenen
   Treiber). Dann:
   ```bat
   .\tools\espdrop.cmd check
   .\tools\espdrop.cmd status
   .\tools\espdrop.cmd find -t 30
   .\tools\espdrop.cmd send C:\Pfad\foto.jpg
   .\tools\espdrop.cmd receive -n MeinPC
   ```
   Bedingungen am iPad wie gewohnt (AirDrop „Für alle“, entsperrt,
   Teilen-Menü offen). Empfangene Dateien landen im aktuellen Ordner.
3. Zusätzlich einmal eine Datei mit Umlaut im Namen senden und empfangen
   (z. B. `Prüfung.txt`): ob libarchive unter Windows Nicht-ASCII-Namen richtig
   umsetzt, ist noch offen.

Erster Lauf unter Windows: Jeder Befehl endete mit `error: Write timeout`. Der
ESP nahm über USB nichts an, wahrscheinlich weil der Chip im Reset festgehalten
wurde. Der USB-Serial/JTAG hält ihn im Reset, solange RTS an und DTR aus ist,
und mit pyserials Vorgabe (beide an) kommt DTR unter Windows nicht zuverlässig
an. Die Host-App öffnet den Port unter Windows jetzt mit DTR und RTS aus (unter
Linux unverändert). Danach den ESP einmal ab- und wieder anstecken.

Zweiter Lauf: `find` fand das iPad, nach „Annehmen“ endete der Upload mit
`[WinError 10054]` (Verbindung vom iPad zurückgesetzt). Ursache sehr
wahrscheinlich: `/Ask` kündigte die Datei als `.\test.jpg` an, das Archiv
enthielt `./test.jpg`. Beide heißen unter Windows jetzt `./name`. `send` zeigt
jetzt die Zeiten der einzelnen Schritte (gefunden nach, verbunden in, angenommen
nach, Upload-Rate); `-v` gibt ein Protokoll mit Millisekunden aus.

Ergebnis (2026-10-23, Windows, C5, iPad): `find` und `send` funktionieren, die
Datei kam an. Die lange Wartezeit vor dem Senden lag an den Befehlen davor,
nicht an `send`. Noch offen: `receive` unter Windows, Dateinamen mit Umlaut.

Abgenommen, wenn `find`, `send` und `receive` unter Windows dasselbe tun wie
unter Linux. Hilfreich bei Fehlern: die vollständige Ausgabe von
`espdrop check` und des fehlgeschlagenen Befehls (ohne Gerätenamen und
Adressen, das Repo ist öffentlich).

Tests ohne Hardware (Linux, Container): `test_env.py` prüft Zertifikat
(OpenDrop nimmt es an, ruft kein openssl auf), den Windows-Archivschreiber
(Einträge `./name`, Inhalt; ein voller Sendevorgang damit bis in einen echten
OpenDrop-Empfänger), die libarchive-Suche, `espdrop check`, pyserial-Timeouts
den Port-Aufbau unter Windows (DTR/RTS aus), die Pfade in `/Ask`, die
Archiv-Köpfe und den Patch-Anwender des Installers. Der Installer selbst lief im Container
komplett durch (frische venv, 17 Patches, alle Prüfsummen gleich,
`check` ok); erneutes Ausführen erkennt die fertige Installation,
eine mit `opendrop_install.sh` gebaute auch. Zusammen 34 Host-Tests.

## Schlafmodus: WLAN aus, solange niemand AWDL braucht

AWDL hält das WLAN des ESP ständig im Empfang und sendet alle paar
Millisekunden; deshalb wurde der C5 auch unbenutzt warm. Die Firmware schaltet
das WLAN jetzt ab, solange nichts es braucht (`main/power.c`, Regeln in
`components/awdl/awdl_power.c` mit Host-Tests). Bluetooth bleibt davon
unberührt.

- **Nach dem Start** schläft das WLAN sofort, bis eine Host-App sich meldet.
- **L2-Betrieb** (`awdl_bridge.py`, `HELLO L2`): WLAN immer an, wie bisher.
- **IP-Betrieb** (`espdrop`, `HELLO IP`): WLAN nur an, solange die Host-App es
  hält: beim Suchen und Senden (`wake <s>` hält es mit), in einem
  Empfangsfenster (`power hold <s>`, die App erneuert das alle 30 s) und
  solange eine TCP-Verbindung offen ist (plus 20 s). Meldet sich die Host-App
  90 s nicht mehr (beendet, abgesteckt), verfallen ihre Anforderungen.
- **Befehl `power`:** zeigt `power auto|on awake|asleep (grund) hold=… wake=…
  tcp=… sleeps=… asleep=…%`; `power hold <1..3600>`, `power release`,
  `power on`, `power auto`. Für Diagnose im IP-Betrieb (z. B. `ping6`) vorher
  `power hold 120`.

Die Firmware lauscht dabei nicht auf Bluetooth-Signale anderer Geräte.
Empfangen ist deshalb nur möglich, solange die App ein Empfangsfenster offen
hat, ähnlich Apples „Für alle für 10 Minuten“. `espdrop receive` hält das
Fenster offen, solange es läuft.

Test (Firmware neu flashen, danach den ESP einmal ab- und anstecken):
```sh
~/opendrop-venv/bin/python tools/espdrop.py cmd power      # asleep (none), kurz nach dem Start
~/opendrop-venv/bin/python tools/espdrop.py find -t 20     # wacht auf, findet das iPad
~/opendrop-venv/bin/python tools/espdrop.py cmd power      # nach dem Ende wieder asleep
```
Der ESP sollte im Leerlauf spürbar kühler bleiben. `asleep=` zeigt, welchen
Anteil der Zeit seit dem Start das WLAN aus war.

## Danach

Ergebnisse (auch Fehlschläge) in `docs/UEBERBLICK.md` unter „Offene Fragen"
festhalten. Fällt Spike 3 Variante A positiv aus, geht es mit M1 weiter: den
OWL-Kern (`src/`) als Komponente portieren und auf einen echten Scheduler legen.
