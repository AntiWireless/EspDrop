# EspDrop – Überblick und Machbarkeitsanalyse

Stand: Oktober 2026. Ziel: AirDrop unter Linux und Windows. Ein ESP32 (C3, später
evtl. C5) dient dabei als billiger WLAN- und Bluetooth-Adapter mit den Fähigkeiten,
die normale PC-WLAN-Karten bzw. -Treiber nicht bieten.

---

## 1. Wie AirDrop technisch funktioniert

```
 ┌───────────────────────────────────────────────────────────────┐
 │ AirDrop-Anwendung: HTTPS (TLS) POST /Discover, /Ask, /Upload    │  OpenDrop (Python)
 │ Dienstsuche: mDNS/DNS-SD "_airdrop._tcp" über IPv6 link-local   │
 ├───────────────────────────────────────────────────────────────┤
 │ IPv6 (fe80::/64, Adresse aus MAC per EUI-64 abgeleitet)         │  Betriebssystem
 ├───────────────────────────────────────────────────────────────┤
 │ AWDL – Apple Wireless Direct Link                               │  OWL (C)
 │  • Action-Frames (PSF/MIF) mit TLVs: Sync, Election, Kanal-Seq. │
 │  • Datenframes: 802.11 + LLC/SNAP (OUI 00:17:f2, PID 0x0800)    │
 │  • feste BSSID 00:25:00:ff:94:73                                │
 │  • Zeitraster: AW = 16 TU, 4 AW = 1 Slot (64 TU ≈ 65,5 ms),    │
 │    16 Slots = 1 Kanalsequenz (≈ 1,05 s)                         │
 ├───────────────────────────────────────────────────────────────┤
 │ 802.11 PHY auf Kanal 6 (2,4 GHz) und 149 bzw. 44 (5 GHz)        │  WLAN-Hardware
 └───────────────────────────────────────────────────────────────┘
 Daneben: Bluetooth LE "Continuity"-Advertisement (Apple, Typ 0x05),
 weckt das AWDL-Interface von iPhones/Macs auf.
```

### Startpunkte

| Projekt | Inhalt | Zustand |
|---|---|---|
| [seemoo-lab/owl](https://github.com/seemoo-lab/owl) | AWDL in C, Userspace, Linux/macOS. libpcap + libev + libnl, erzeugt ein TAP-Interface `awdl0` | nicht mehr gepflegt |
| [seemoo-lab/opendrop](https://github.com/seemoo-lab/opendrop) | AirDrop-Protokoll in Python (zeroconf, HTTPS, libarchive) | nicht mehr gepflegt, mit iOS 26 ohne Patches defekt |
| [jedbillyb/airdrop-mt7921](https://github.com/jedbillyb/airdrop-mt7921) + [jedbillyb/owl](https://github.com/jedbillyb/owl) | OWL-Fork und OpenDrop-Patches, **mit iOS 26 in beide Richtungen nachgewiesen** (Okt. 2026 aktiv) | **wichtigste aktuelle Quelle** |

Alle drei stehen unter GPLv3, genau wie dieses Repo. Code kann also übernommen werden.

### Aufbau von OWL (≈ 6 200 Zeilen C)

- `src/` (≈ 2 500 Zeilen) ist **plattformunabhängig**: Frame-Bau (`tx.c`), TLV-Parsing (`rx.c`),
  Sync (`sync.c`), Election (`election.c`), Kanalsequenz (`channel.c`), Peer-Tabelle (`peers.c`),
  Sende-Fenster (`schedule.c`). Verwendet wird nur libc plus `ether_addr` und Endian-Makros.
  Ausnahme ist das Radiotap-Parsing im RX-Pfad.
  → **Lässt sich fast unverändert auf den ESP32 portieren.**
- `daemon/` ist Linux/macOS-spezifisch: libev-Eventloop (`core.c`), pcap-Injection und
  TUN/TAP (`io.c`), nl80211-Kanalwechsel und Neighbor-Table (`netutils.c`).
  → **Muss für den ESP32 neu geschrieben werden** (FreeRTOS + `esp_timer`, Promiscuous-RX,
  `esp_wifi_80211_tx`).

Bekannte Schwächen von Upstream-OWL, die im Fork von jedbillyb behoben sind:
- Die eigene Kanalsequenz ist statisch (16× derselbe Kanal). **iOS 26 sendet keinen Unicast
  an einen Peer mit konstanter Sequenz** (FINDINGS §25). Man muss die Sequenz des iPhones
  übernehmen (Strategie „verbatim“, ggf. „widen“).
- `peer->sync_offset` wurde nie gesetzt (§21).
- Die Peer-Timeouts stehen als „ms“ kommentiert, sind aber µs (effektiv 2 s Timeout).
- Die TX-Rate ist fest auf 12 Mbit/s OFDM (`tx.c`).

### Erkenntnisse zu iOS 26 (aus airdrop-mt7921/docs/FINDINGS.md)

- Das iPhone muss auf **„Jeder für 10 Minuten“** stehen. „Nur Kontakte“ braucht ein
  Apple-signiertes Validation Record, das man nur von einem echten Mac extrahieren kann.
- **Empfangen** (iPhone → PC): Das Share-Sheet muss offen sein, dann ist AWDL wach.
- **Senden** (PC → iPhone): Das iPhone annonciert `_airdrop._tcp` nur, solange ein
  BLE-Continuity-AirDrop-Advertisement läuft (§27, nachgewiesen). Bei „Jeder“ dürfen die
  Hash-Felder Nullen sein.
- Nötige OpenDrop-Änderungen: Chunked Requests ohne `Content-Length`, Container
  `application/x-dvzip` (Blöcke mit 4-Byte-BE-Header, Bit 31 = stored, sonst zlib,
  128-KiB-Blöcke → ODC-cpio), `TransferID` + `TransferType` schon im `/Ask`.
  Ohne Apple-ID werden Uploads akzeptiert (§37).
- Kanalsequenz eines iPhones: Im Leerlauf z. B. `132,0,149,0,0,0,0,0,6,0,149,0,…`
  (2 Slots auf 149, 1 Slot auf 6). Während eines Transfers wächst sie auf bis zu 11 Slots
  auf 149 und 5 auf 6. `0` heißt „nicht anwesend“. Slot 0 ist meist der Infrastruktur-Kanal.
- Gemessener Durchsatz MT7921: ~3 Mbit/s innerhalb eines Bursts, ~45–60 kB/s im Mittel.
  Begrenzt wird das durch den Duty-Cycle (wie viele Slots man mit dem Peer teilt), nicht
  durch die PHY-Rate.
- In der EU ist der 5-GHz-Sozialkanal teils **44** statt 149 (noch zu verifizieren).

Unabhängige Bestätigung, dass eine Nicht-Apple-Implementierung funktioniert: Seit Nov. 2025
spricht Google Quick Share auf dem Pixel 10 mit AirDrop, ebenfalls nur im Modus
„Jeder für 10 Minuten“.

---

## 2. Was der ESP32 können muss, und was er kann

### Kernanforderung von OWL: „aktiver Monitor-Modus“

1. Beliebige 802.11-Frames senden (Action- und Datenframes mit eigener BSSID).
2. Alle Frames mit der AWDL-BSSID empfangen, inklusive RSSI und möglichst genauem Zeitstempel.
3. **Unicast-Frames an die eigene AWDL-MAC in Hardware ACKen** (innerhalb SIFS = 10/16 µs).
   Das schafft nur die Hardware selbst. Über USB vom Host aus ist es unmöglich.
4. Schnelle Kanalwechsel im AWDL-Raster.

Für Windows gibt es praktisch keine Treiber mit Monitor-Mode und Injection. Deshalb muss
die **AWDL-Schicht für beide Betriebssysteme auf dem ESP32 laufen**, nicht erst für Windows.

### ESP-IDF-APIs (geprüft an esp-idf `master`, Okt. 2026)

| Anforderung | ESP-IDF | Bewertung |
|---|---|---|
| Senden | `esp_wifi_80211_tx()`: Beacon, Probe, **Action**, **Non-QoS-Data**; max. 1500 Byte; `en_sys_seq=false` → eigene Sequenznummern | AWDL-PSF/MIF sind Vendor-Action-Frames, AWDL-Daten sind Non-QoS-Data. Passt. MTU muss ≤ ~1460 sein |
| TX-Rate | `esp_wifi_config_80211_tx_rate()` bzw. `esp_wifi_config_80211_tx()` (vor `esp_wifi_start`) | Fest pro Interface. Mögliche Idee: STA-IF langsam für Action-Frames, AP-IF schnell für Daten (Nachteil: Das AP-IF sendet Beacons) |
| TX-Status | `esp_wifi_register_80211_tx_cb()` → `tx_status` SUCCESS/FAIL | Deutet auf ACK-Auswertung hin, noch zu verifizieren |
| Empfang | `esp_wifi_set_promiscuous()` + Filter (MGMT/DATA), `rx_ctrl`: RSSI, Kanal, `timestamp` (µs), `sig_len` inkl. FCS | Gut. Der Zeitstempel ist genauer als bei OWL (dort `clock_gettime` bei pcap-Auslieferung) |
| Kanalwechsel | `esp_wifi_set_channel()` | Latenz unbekannt, messen (MT7921: 8,5 ms) |
| BLE-Advertisement | NimBLE/Bluedroid, Manufacturer Data 0x004C | Unproblematisch. Wi-Fi/BLE-Koexistenz teilt sich das Radio, also BLE nur bei Bedarf aktivieren |

### Größtes technisches Risiko: ACK und Empfang an die eigene MAC

Ein Forenbericht (IDF 5.5.2) sagt: Datenframes an die MAC des aktiven Interfaces werden
**ge-ACKt, aber nicht an den Promiscuous-Callback geliefert**, weil der WLAN-Stack sie
verschluckt. Falls das zutrifft:

- **Variante A** – AWDL-MAC = Interface-MAC: Es wird ge-ACKt, aber die Frames müssen auf
  anderem Weg abgegriffen werden. Zu prüfen sind interne RX-Hooks und das AP-Interface.
- **Variante B** – AWDL-MAC ≠ Interface-MAC: Alles kommt im Callback an, aber es gibt keine
  ACKs. Das iPhone wiederholt dann jeden Frame bis zu ~7×. Das funktioniert, ist aber langsam
  (siehe OWL-README). Außerdem erkennt der ESP die ACKs des iPhones auf *unsere* Frames
  womöglich nicht und wiederholt selbst.

→ **Das muss als Allererstes auf echter Hardware getestet werden.** Davon hängen Durchsatz
und Zuverlässigkeit ab, nicht aber die grundsätzliche Machbarkeit.

### ESP32-C3 vs. ESP32-C5

| | ESP32-C3 | ESP32-C5 |
|---|---|---|
| WLAN | 802.11 b/g/n, **nur 2,4 GHz** | 802.11ax, **2,4 + 5 GHz** (5180–5885 MHz → Kanäle 36/44/149) |
| BLE | 5.0 | 5.0 |
| CPU / RAM | RISC-V 160 MHz, 400 KB SRAM | RISC-V 240 MHz, 384 KB SRAM (PSRAM optional) |
| USB | USB-Serial/JTAG (CDC-ACM, Full-Speed), **kein OTG** | ebenfalls USB-Serial/JTAG, kein OTG |
| AWDL-Kanäle | nur 6 | 6, 44, 149 |

Folge: Mit dem **C3** sieht man ein iPhone nur in dessen Kanal-6-Slots, im Leerlauf
1 von 16, im Transfer-Peak bis zu 5 von 16. Discovery und kleine Dateien sind
wahrscheinlich machbar, der Durchsatz wäre aber deutlich niedriger als auf 149.
Unklar ist auch, ob iOS eine Sequenz akzeptiert, die nur Kanal 6 enthält.
**Der C5 ist für alltagstaugliche Nutzung sehr wahrscheinlich nötig.** Die API ist
identisch, der Code bleibt also derselbe.

USB ist kein Flaschenhals: Full-Speed-CDC liefert grob mehrere 100 kB/s bis ~1 MB/s
(messen), AWDL realistisch 50–250 kB/s.

---

## 3. Architektur: Was läuft wo?

### Immer auf dem ESP32 (beide Betriebssysteme)

- AWDL komplett: portierter OWL-Kern (`src/`) mit den Fixes von jedbillyb, dazu neuer
  Radio-Treiber (Promiscuous-RX, Raw-TX, Kanal-Scheduler über `esp_timer`), PSF/MIF-Timer,
  Peer-Verwaltung.
- BLE-Continuity-Advertisement (AirDrop, Typ 0x05).
- Host-Link über USB-CDC mit Paket-Framing (SLIP/COBS): Datenframes + Steuerkanal + Log.

### Wie die IP-Schicht zum Host kommt – drei Optionen

| | A: „AWDL-Netzwerkkarte“ (L2) | C: „Socket-Proxy“ (IP-Stack auf ESP) | B: „AirDrop-Appliance“ |
|---|---|---|---|
| ESP zusätzlich | – | lwIP (IPv6 link-local) + mDNS, TCP-Sockets werden zum Host getunnelt | zusätzlich TLS, HTTP, plist, dvzip/cpio |
| Linux-Host | Bridge USB ↔ TAP `awdl0`, gepatchtes OpenDrop **unverändert** | Host-App mit Socket-Shim | einfache Host-App |
| Windows-Host | Bridge USB ↔ Wintun (L3, IPv6 → MAC per EUI-64 trivial) oder TAP-Windows6. **Treiber + Admin nötig**, Konflikte mit Windows-eigenem mDNS/NDP/LLMNR-Traffic | **Keine Treiber, kein Admin**, reine Userspace-App | keine Treiber. Sogar Browser-UI per WebSerial denkbar |
| App-Protokoll | Host (leicht zu aktualisieren) | Host (leicht zu aktualisieren) | Firmware (iOS-Änderungen = Firmware-Update) |
| Aufwand Firmware | gering | mittel | hoch (RAM-knapp, v. a. TLS + Puffer) |

**Empfehlung:**
1. **Zuerst A**: kleinste Firmware, und unter Linux lässt sich mit Wireshark auf `awdl0`
   und dem bereits gepatchten OpenDrop am besten debuggen.
2. **Für Windows dann C**: Der ESP bekommt lwIP + mDNS (hat er ohnehin), der Host spricht
   TLS/HTTP/AirDrop in Userspace. Kein Treiber, und dieselbe Host-App läuft auch unter Linux.
   Die Vorstellung „ein Großteil des Codes auf dem ESP32“ trifft damit für die
   zeitkritischen Teile zu. Die Anwendungslogik, die sich mit jeder iOS-Version ändert
   (siehe die 17 OpenDrop-Patches für iOS 26), bleibt besser auf dem Host.
3. B nur, falls ein komplett treiber- und installationsfreier Betrieb gewünscht ist.

---

## 4. Vorgeschlagener Fahrplan

**M0 – Hardware-Spikes (entscheiden über alles Weitere)**
1. Raw-TX: einen AWDL-PSF/MIF mit `esp_wifi_80211_tx` senden. Taucht er in einer
   Wireshark-Aufzeichnung auf, und reagiert ein Mac/iPhone darauf?
2. RX: AWDL-Action-Frames im Promiscuous-Mode empfangen (Filter auf BSSID
   00:25:00:ff:94:73) und Kanalsequenz-TLV eines iPhones dekodieren.
3. ACK-Test: Unicast-Datenframes an die AWDL-MAC. Werden sie ge-ACKt, kommen sie im
   Callback an? Varianten A/B aus Abschnitt 2 durchspielen.
4. Latenz von `esp_wifi_set_channel` und den Zeitbezug `rx_ctrl.timestamp` ↔
   `esp_timer_get_time()` messen.
5. USB-CDC-Durchsatz messen.

**M1 – AWDL auf dem ESP:** OWL-`src/` als ESP-IDF-Komponente (Basis: jedbillyb/owl),
neuer Scheduler. Ziel: iPhone als Peer sehen, Sync + Election, Log über USB.

**M2 – Linux-Pfad (Option A):** Host-Bridge USB ↔ TAP `awdl0`. Dann `ping6` zum iPhone,
danach mit dem gepatchten OpenDrop eine Datei **empfangen**.

**M3 – BLE-Wake auf dem ESP:** Datei an das iPhone **senden**.

**M4 – Windows (Option C):** lwIP + mDNS auf dem ESP, Socket-Proxy-Protokoll, Host-App
(z. B. Python oder Rust) mit OpenDrop-Logik inkl. iOS-26-Patches.

**M5 – C5 / 5 GHz und Durchsatz:** Kanäle 44/149, Strategie für die Kanalsequenz
(verbatim/widen), höhere TX-Rate (HT-MCS statt 12 Mbit/s OFDM).

### Vorgeschlagene Repo-Struktur

```
firmware/                 ESP-IDF-Projekt (C)
  components/awdl/        portierter OWL-Kern (GPLv3)
  main/                   Radio-I/O, Scheduler, BLE, Host-Link
host/
  common/                 Host-Link-Protokoll
  linux/                  espdrop-bridge (USB ↔ TAP awdl0)
  windows/                später
docs/
```

---

## 5. Offene Fragen und Risiken

| Risiko | Auswirkung | Gegenmaßnahme |
|---|---|---|
| ESP liefert ge-ACKte Frames an eigene MAC nicht an den Sniffer | Durchsatz und Stabilität | M0-Test, Variante A/B, interne Hooks |
| iOS akzeptiert keine reine Kanal-6-Sequenz (C3) | C3 nur eingeschränkt nutzbar | früh testen, C5 einplanen |
| Kanalwechsel-Latenz / Timing-Jitter auf FreeRTOS | verpasste Availability Windows | `esp_timer`, Tick 1000 Hz, Wechsel vorziehen |
| Wi-Fi/BLE-Koexistenz auf einem Radio | verpasste AWs während BLE aktiv ist | BLE nur zur Discovery |
| Apple ändert AirDrop/AWDL (iOS 27 …) | Protokollbruch | App-Logik auf dem Host halten (leicht aktualisierbar) |
| Regulatorik 5 GHz: 149 in DE nur als SRD mit 25 mW EIRP, 44 nur in Innenräumen | Kanalwahl, Reichweite | Firmware begrenzt 149+ auf 11 dBm; welchen Kanal deutsche iPhones nutzen, misst der C5-Test (SPIKES.md) |

Langfristig ließe sich auch Wi-Fi Aware (NAN) nutzen: ESP-IDF unterstützt NAN, und Apple
öffnet es für Apps. Das wäre aber kein AirDrop, sondern bräuchte eine eigene App auf dem
iPhone, und gehört daher nicht zum Projektumfang.
