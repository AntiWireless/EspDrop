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
   `fcs_ok` steigt mit `action`, `fcs_bad` und `type_mismatch` bleiben 0.
   Falls nicht, stimmt auf dem C5 die Länge im Promiscuous-Puffer nicht – bitte melden.
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
8. **Optional: Bridge auf 5 GHz.** Bridge-Firmware für den C5 flashen, im
   `awdl_bridge.py`-Terminal `chan 44` (bzw. den Kanal aus Test 3) eingeben,
   dann wie in M2.4 pingen bzw. AirDrop testen. Der Knoten bleibt dabei auf
   einem Kanal und übernimmt die 5-GHz-Slots des Masters, ganz ohne Kanalwechsel.

## Danach

Ergebnisse (auch Fehlschläge) in `docs/UEBERBLICK.md` unter „Offene Fragen"
festhalten. Fällt Spike 3 Variante A positiv aus, geht es mit M1 weiter: den
OWL-Kern (`src/`) als Komponente portieren und auf einen echten Scheduler legen.
