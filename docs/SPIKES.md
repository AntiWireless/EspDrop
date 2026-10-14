# M0 – Hardware-Spikes auf dem ESP32-C3

Ziel von M0: herausfinden, ob der ESP32-C3 das kann, was AWDL braucht, **bevor**
der ganze Stack portiert wird. Der C3 funkt nur auf 2,4 GHz, also läuft hier
alles auf **Kanal 6** (dem 2,4-GHz-Sozialkanal von AWDL). Für den Alltag wird
später vermutlich ein C5 (5 GHz, Kanäle 44/149) nötig – siehe `UEBERBLICK.md`.

Die Firmware dazu liegt in `firmware/spikes/`. Sie bringt WLAN hoch und bietet
eine Konsole über den eingebauten USB-Serial/JTAG-Port.

## Bauen und Flashen

```sh
# ESP-IDF >= 5.5 (getestet mit 6.0.3), einmalig: . $IDF_PATH/export.sh
cd firmware/spikes
idf.py set-target esp32c3
idf.py build
idf.py -p /dev/ttyACM0 flash monitor      # C3: USB-Serial/JTAG meldet sich als ttyACM*
```

In `monitor` (oder jedem seriellen Terminal) `help` eingeben. Beenden mit `Ctrl-]`.

## Konsolenbefehle

| Befehl | Zweck |
|---|---|
| `info` | eigene MAC, Kanal, Promiscuous-Status |
| `mac <aa:bb:cc:dd:ee:ff>` | Stations-MAC setzen (für den ACK-Test) |
| `chan <1..13>` | Kanal wechseln |
| `rx on` / `rx off` | Promiscuous-Empfang (nur AWDL-BSSID) an/aus |
| `tx psf [n]` / `tx mif [n]` | n Action-Frames senden |
| `txdata <hex>` | einen AWDL-Datenframe an die AWDL-Broadcast-MAC senden |
| `peers` | gesammelte Peer-Ereignisse ausgeben (Name, RSSI, Kanalsequenz, Zeitstempel) |
| `stats` | RX/TX-Zähler |
| `reset` | Zähler nullen |
| `chanlat [n]` | Latenz von `esp_wifi_set_channel` über n Wechsel messen |
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
   ARPA-Name (`espdrop-c3`) und Version sauber dekodiert.
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

## Danach

Ergebnisse (auch Fehlschläge) in `docs/UEBERBLICK.md` unter „Offene Fragen"
festhalten. Fällt Spike 3 Variante A positiv aus, geht es mit M1 weiter: den
OWL-Kern (`src/`) als Komponente portieren und auf einen echten Scheduler legen.
