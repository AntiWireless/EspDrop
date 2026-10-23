# M4 – Entwurf: IP-Stack auf dem ESP, Socket-Proxy, eine Host-App für Windows und Linux

Status: **Entwurf zur Abstimmung, noch kein Code.** Die offenen Entscheidungen
stehen in Abschnitt 9.

## 1. Ziel und Grundidee

**Heute (Option A, nur Linux):** Der ESP ist Funkteil und Ethernet-Brücke. Er
reicht ganze Ethernet-Frames über USB an das TAP-Interface `awdl0` weiter.
IPv6, TCP, UDP und mDNS macht der Linux-Kernel, darüber läuft das gepatchte
OpenDrop. Das braucht root (TAP, `ip`), einen Linux-Kernel und Firewall-
Ausnahmen; unter Windows gibt es kein Gegenstück zu `awdl0`.

**M4 (Option C):** Der ESP bekommt einen eigenen IP-Stack (lwIP) auf der
AWDL-Verbindung. TCP-Verbindungen und UDP enden **auf dem ESP**; über USB gehen
nur noch die Nutzdaten der Verbindungen (Bytes) und Datagramme. TLS, HTTP und
die ganze AirDrop-Logik bleiben auf dem PC, in einer Host-App, die OpenDrop als
Bibliothek benutzt.

**Dieselbe Firmware und dieselbe Host-App laufen unter Windows und Linux.**
Getestet wird zuerst unter Linux. Unter Linux fallen dabei TAP, root-Rechte und
Firewall-Regeln weg: Die Host-App braucht nur Zugriff auf die serielle
Schnittstelle. Der bisherige Weg über `awdl0` bleibt als Diagnose-Modus in
derselben Firmware erhalten (Abschnitt 3.2).

```
 iPhone/iPad                ESP32-C5 / C3                      PC (Windows oder Linux)
 -----------      AWDL      --------------------------   USB   ------------------------------
   AirDrop  <~~~~~~~~~~~~>  Radio + AWDL (wie heute)     COBS   espdrop (Python)
                            lwIP: IPv6, TCP, UDP          <==>    Socket-Proxy  <-> lokale Sockets
                            Socket-Proxy                          mDNS (Browser + Responder)
                            BLE-Wecksignal (wie heute)            OpenDrop: TLS, HTTP, AirDrop
```

## 2. Was wo läuft

| Aufgabe | heute | M4 |
|---|---|---|
| AWDL (Sync, Election, Kanal, Slots, Daten-Frames) | ESP | ESP (unverändert) |
| BLE-Wecksignal (`wake`) | ESP | ESP (unverändert) |
| IPv6, Nachbarauflösung, TCP, UDP | Linux-Kernel | **ESP (lwIP)** |
| mDNS (`_airdrop._tcp`) | zeroconf auf dem PC | **Host-App**, über UDP-Proxy (Entscheidung 1) |
| TLS, HTTP, `/Discover`, `/Ask`, `/Upload`, Archive | OpenDrop | OpenDrop (unverändert, als Bibliothek) |
| Bedienung | `awdl_bridge.py` + `opendrop_*.sh` | `espdrop receive` / `espdrop send` |

TLS bleibt bewusst auf dem PC: Zertifikate, Schlüssel, die iOS-26-Anpassungen
und das Entpacken der Archive (dvzip/cpio) stecken in OpenDrop und den 17
Patches. Auf dem ESP wäre das schwer zu debuggen und würde RAM kosten, den
TCP dringender braucht.

## 3. Firmware

### 3.1 AWDL als lwIP-Netzwerkschnittstelle

- Ein eigenes `esp_netif` mit selbst geschriebenem Treiber („awdl“): Senden geht
  in den bestehenden Daten-Pfad (`awdl_app_tx_ethernet`, slot-gesteuert), der
  Empfang kommt aus dem bestehenden RX-Pfad (`deliver_msdu`), der heute an USB
  weiterreicht.
- MAC = die Wi-Fi-MAC des ESP; IPv6-Link-Local per EUI-64 daraus (wie heute
  `addr_gen_mode=0` auf `awdl0`, das brauchen Apple-Geräte). MTU 1460.
- Mitglied in `ff02::fb` (MLD) für mDNS.
- **Nachbarauflösung ohne NDP über die Luft:** Die Host-Bridge lernt heute die
  Zuordnung Link-Local ↔ MAC aus empfangenem Verkehr und trägt sie fest ein.
  Dasselbe macht die Firmware: Aus jedem empfangenen IPv6-Frame wird
  (Quell-IPv6, Quell-MAC) gelernt; ein eigener `output_ip6` der Netif schaut
  dort zuerst nach (Multicast → `33:33:…`), und nur unbekannte Ziele gehen an
  lwIPs normale ND.
- Neue Diagnosebefehle: `ip` (lwIP-Zähler, Nachbartabelle, offene
  Verbindungen) und `ping6 <adresse>` direkt vom ESP. Damit lässt sich der
  IP-Weg zum iPhone ohne Host-App prüfen.

### 3.2 Zwei Betriebsarten in einer Firmware

- **L2** (wie heute): Frames gehen an den Host, `awdl_bridge.py` und `awdl0`
  funktionieren unverändert. Bleibt für Diagnose mit tcpdump/Wireshark.
- **IP** (neu): Frames gehen in lwIP; über USB laufen Proxy-Nachrichten.
- Der Host wählt die Betriebsart mit der ersten Nachricht (`HELLO`, Abschnitt
  4). Ohne `HELLO` bleibt der ESP in L2, damit ältere Host-Werkzeuge
  weiterlaufen. Beide gleichzeitig geht nicht (zwei IP-Stacks mit derselben
  Adresse würden sich stören).
- Ein neues `HELLO` beendet alle Proxy-Verbindungen; so räumt ein Neustart der
  Host-App den ESP auf.

### 3.3 Socket-Proxy

- **TCP:** lauschen (Port 8771 für den Empfang), annehmen, verbinden (Port des
  Empfängers beim Senden), Daten, Halb-Schließen (FIN) und Abbruch (RST).
  Höchstens 4 gleichzeitige Verbindungen (iOS öffnet für `/Discover`, `/Ask`
  und `/Upload` teils eigene Verbindungen; OpenDrops Server ist deshalb
  mehrfädig).
- **UDP:** ein Socket auf Port 5353 mit Gruppe `ff02::fb`; empfangene
  Datagramme gehen mit Absender und „war Multicast“ an den Host, der Host
  schickt Datagramme an Multicast oder an eine Unicast-Adresse.
- **Flusskontrolle mit Krediten** je Verbindung und Richtung (Abschnitt 4.3).
  Der ESP gibt TCP-Fenster erst frei (`tcp_recved`), wenn die Daten auf USB
  sind. Wird USB eng, schließt sich also das TCP-Fenster, und das iPhone sendet
  langsamer, statt dass wie heute Frames an der Warteschlange verworfen werden
  (zuletzt ~1,6 %).

### 3.4 Speicherbudget

Gemessen auf dem C5 mit Bluetooth (2026-10-23): frei `heap` ≈ 67 KB bei einer
L2-Warteschlange von 16 Plätzen (≈ 24 KB), also ≈ 91 KB vor der Warteschlange.
In der IP-Betriebsart entfällt die L2-Warteschlange; der Platz geht an lwIP.

| Posten (Vorschlag) | Bedarf |
|---|---|
| TCP-Empfangsfenster 8 × MSS = 11,7 KB, 2 Verbindungen mit Daten | ≈ 24 KB |
| TCP-Sendepuffer 6 × MSS = 8,8 KB, 2 Verbindungen | ≈ 18 KB |
| pbufs für Frames unterwegs, Proxy-Puffer, UDP | ≈ 12 KB |
| Reserve für Wi-Fi (gemessen ~7 KB Spitze) | ≈ 24 KB |
| **Summe** | **≈ 78 KB** von ≈ 91 KB |

Das ist knapp, aber machbar. Falls nicht: Bluetooth erst bei `wake` starten und
danach wieder abbauen (Entscheidung 5). Die echten Zahlen kommen in M4.1 aus
`stats`/`ip`.

### 3.5 Diagnose ohne tcpdump

In der IP-Betriebsart gibt es kein `awdl0` mehr, an dem man mitschneiden kann.
Ersatz: Der Befehl `capture on|off` lässt den ESP jeden IP-Frame (in beide
Richtungen, mit Zeitstempel) zusätzlich als `CAPTURE`-Nachricht schicken; die
Host-App schreibt daraus eine `.pcapng`-Datei für Wireshark. Das kostet
USB-Bandbreite und ist nur zum Debuggen gedacht.

## 4. USB-Protokoll (Erweiterung des bestehenden COBS-Links)

Rahmen wie heute: COBS, Null-Byte als Trenner, erstes Byte = Nachrichtentyp.
Die Typen 1–8 (Echo, Log, Frame, Peer, Cmd) bleiben. Zahlen little-endian,
IPv6-Adressen als 16 Bytes in Netzwerk-Reihenfolge (immer AWDL-Link-Local,
die Zone ist implizit die AWDL-Netif).

### 4.1 Nachrichten

| Typ | Name | Richtung | Inhalt |
|---|---|---|---|
| 9 | `HELLO` | H→E | `u8 version, u8 mode (0 L2, 1 IP), u32 host_credit` |
| 10 | `HELLO_REPLY` | E→H | `u8 version, u8 mode, mac[6], u8 max_conns, u16 max_chunk, u8 caps (Bit 0 BLE)` |
| 16 | `TCP_LISTEN` | H→E | `u16 port` |
| 17 | `TCP_CONNECT` | H→E | `u16 conn, addr[16], u16 port` |
| 18 | `TCP_OPENED` | E→H | `u16 conn, addr[16] remote, u16 remote_port, u16 local_port, u8 accepted, u32 credit` |
| 19 | `TCP_DATA` | beide | `u16 conn, bytes` (höchstens `max_chunk`, Vorschlag 1400) |
| 20 | `TCP_CREDIT` | beide | `u16 conn, u32 bytes` (Empfänger gibt so viele Bytes zusätzlich frei) |
| 21 | `TCP_CLOSE` | beide | `u16 conn, u8 kind (0 FIN, 1 RST), i8 err` |
| 24 | `UDP_OPEN` | H→E | `u16 port, u8 flags (Bit 0 Gruppe ff02::fb)` |
| 25 | `UDP_RECV` | E→H | `u16 port, addr[16] src, u16 src_port, u8 flags (Bit 0 war Multicast), bytes` |
| 26 | `UDP_SEND` | H→E | `u16 port, addr[16] dst, u16 dst_port, bytes` |
| 28 | `RESULT` | E→H | `u8 request_type, u16 id, i8 err` (für `TCP_LISTEN`, `UDP_OPEN`, fehlgeschlagenes `TCP_CONNECT`) |
| 32 | `CAPTURE` | E→H | `u8 dir, u32 time_us, frame` (nur nach `capture on`) |

Verbindungsnummern: Der Host vergibt sie für ausgehende Verbindungen
(`0x0001–0x7fff`), der ESP für angenommene (`0x8000–0xffff`). So gibt es keine
Kollision ohne Rückfrage.

### 4.2 Abläufe

**Senden an ein iPhone:**
```
H→E  CMD "wake 120"
H→E  UDP_OPEN 5353 (Gruppe)             E→H  RESULT ok
H→E  UDP_SEND ff02::fb PTR? _airdrop    (alle 1,5 s, wie der OpenDrop-Patch)
E→H  UDP_RECV fe80::… PTR/SRV/TXT/AAAA  → Host kennt Adresse und Port
H→E  TCP_CONNECT 1, fe80::…, 8770       E→H  TCP_OPENED 1, credit 8760
H↔E  TCP_DATA / TCP_CREDIT …            (TLS, POST /Discover, /Ask, /Upload)
H→E  TCP_CLOSE 1 FIN                    E→H  TCP_CLOSE 1 FIN
H→E  CMD "wake off"
```

**Empfangen vom iPhone:**
```
H→E  TCP_LISTEN 8771                    E→H  RESULT ok
H→E  UDP_OPEN 5353 (Gruppe)             E→H  RESULT ok
H→E  UDP_SEND … Ankündigung _airdrop (alle 5 s) und Antworten auf Anfragen
E→H  TCP_OPENED 0x8001, accepted=1      → Host verbindet zum lokalen OpenDrop-Server
H↔E  TCP_DATA / TCP_CREDIT …
```

### 4.3 Flusskontrolle

- Jede Seite darf je Verbindung nur so viele Bytes schicken, wie die andere
  freigegeben hat. Startkredit ESP→Host aus `HELLO` (Vorschlag 64 KB), Host→ESP
  aus `TCP_OPENED` (= freier TCP-Sendepuffer auf dem ESP).
- Der ESP gibt Kredit an den Host zurück, sobald lwIP Daten bestätigt bekommen
  hat (`tcp_sent`); der Host gibt Kredit zurück, sobald die lokale Anwendung
  die Daten gelesen hat.
- Der ESP verteilt die USB-Bandbreite reihum auf die Verbindungen; `CMD`,
  `UDP_*` und Steuer-Nachrichten haben Vorrang vor `TCP_DATA`.

### 4.4 Version

`HELLO.version` = 1. Passt die Version nicht, antwortet der ESP mit seiner
eigenen und bleibt in L2; die Host-App meldet dann „Firmware zu alt/neu“.

## 5. Host-App `espdrop` (Python)

### 5.1 Aufbau

Ein Python-Paket unter `host/espdrop/`, lauffähig unter Windows und Linux:

| Modul | Aufgabe |
|---|---|
| `link` | COBS-Rahmen (aus `tools/espdrop_link.py`), serielle Schnittstelle über pyserial (`COM5`, `/dev/ttyACM0`) |
| `device` | Protokoll aus Abschnitt 4, `HELLO`, Befehle (`wake`, `node`, `stats` …) |
| `proxy` | Verbindungstabelle, Kredite; macht jede ESP-Verbindung zu einem normalen lokalen Socket |
| `mdns` | Browser und Responder für `_airdrop._tcp` über den UDP-Proxy |
| `airdrop` | Anbindung an das gepatchte OpenDrop (Server, Client, Konfiguration) |
| `cli` | `espdrop receive`, `espdrop send DATEI…`, `espdrop find`, `espdrop status`, `espdrop capture` |

Ein Prozess je Aufruf, wie heute `awdl_bridge.py` plus Skript zusammen. Ein
Dienst im Hintergrund oder ein Tray-Symbol kann später darauf aufsetzen.

### 5.2 OpenDrop als Bibliothek

OpenDrop 0.13.0 mit den 17 Patches aus airdrop-mt7921 bleibt die Grundlage
(gleiche Installation wie heute, `tools/opendrop_install.sh`, für Windows
angepasst). Ersetzt wird nur die Netzwerk-Anbindung, ohne die OpenDrop-Dateien
weiter zu patchen:

- **Empfangen:** Eine Unterklasse von `AirDropServer` lässt den HTTPS-Server
  auf `127.0.0.1` an einem freien Port lauschen, statt auf `awdl0`. Für jede
  vom ESP angenommene Verbindung öffnet `proxy` eine Verbindung dorthin. TLS,
  HTTP-Handler, Annehmen-Abfrage und Entpacken laufen unverändert. Die
  Dienst-Anmeldung geht an unser `mdns` statt an zeroconf.
- **Senden:** `AirDropClient` baut seine Verbindung über
  `HTTPSConnectionAWDL` auf. Die Host-App ersetzt diese Klasse durch eine
  Variante, deren `connect()` beim Proxy eine Verbindung zu `[fe80::…]:Port`
  bestellt und den lokalen Socket in TLS verpackt. Alle iOS-26-Anpassungen in
  `/Ask` und `/Upload` bleiben erhalten.
- **Suchen:** Unser `mdns`-Browser liefert dieselben Angaben wie zeroconfs
  `ServiceInfo` (Name, Adresse, Port, TXT-Flags), damit OpenDrops
  `/Discover`-Logik weiterläuft.

Lokale Sockets nur auf `127.0.0.1`: keine Firewall-Abfrage unter Windows,
keine Regel unter Linux.

### 5.3 mDNS auf dem Host

Was die letzten Wochen gezeigt haben, wird hier eingebaut statt nachgebessert:
- Browser: PTR-Anfrage für `_airdrop._tcp.local.` alle 1,5 s (wie
  `opendrop-mdns-repeat`), SRV/TXT/AAAA aus Antwort und Zusatz-Abschnitt,
  Goodbye (TTL 0) beachten, Antworten auf QU-Fragen auch per Unicast annehmen.
- Responder: unser Dienst mit TXT `flags`, SRV auf 8771, AAAA = Link-Local des
  ESP (aus `HELLO_REPLY`), Ankündigung alle 5 s (wie
  `opendrop-mdns-reannounce`), Antworten auf Anfragen, Goodbye beim Beenden.
- Kodierung/Dekodierung mit den Nachrichten-Klassen von python-zeroconf
  (`DNSIncoming`/`DNSOutgoing`, in OpenDrops Installation schon vorhanden); den
  Socket-Teil von zeroconf brauchen wir nicht.
- Jede gesehene Antwort lässt sich mit `-v` protokollieren, so wie
  `tools/zeroconf_probe.py` es jetzt tut.

### 5.4 Linux und Windows

- **Linux:** kein root, kein TAP, keine Firewall-Regel. Nur Zugriff auf
  `/dev/ttyACM0` (Gruppe `dialout` bzw. `uucp`).
- **Windows 10/11:** Der USB-Serial/JTAG-Port des ESP meldet sich als
  CDC-Gerät, Windows bringt den Treiber mit (COM-Port). Python 3.12+,
  pyserial und OpenDrops Abhängigkeiten; heikel ist `libarchive` (braucht eine
  DLL). Ein fertiges Paket (z. B. PyInstaller) kommt erst, wenn alles läuft.

## 6. Testen ohne Hardware

- **Simulierter ESP:** Ein Python-Modul spricht das Protokoll aus Abschnitt 4
  und beendet TCP/UDP selbst auf Loopback-Sockets. Zwei Host-Apps mit je einem
  simulierten ESP, die eine gemeinsame „Luft“ teilen, ergeben einen vollen
  AirDrop-Durchlauf OpenDrop → OpenDrop. So lassen sich Proxy, Kredite, mDNS
  und die OpenDrop-Anbindung hier im Container testen, bevor Sie flashen.
- **mDNS-Kodierung** gegen Pakete in der Form, die Ihr iPad tatsächlich
  schickt (14 Antworten mit NSEC/OPT, 209-Byte-Antworten, Goodbye), wie im
  Nachbau aus der Fehlersuche bei M3.
- **Firmware:** Die Proxy-Logik (Verbindungstabelle, Kredite, Nachrichten
  zerlegen) kommt in eine plattformunabhängige Komponente mit Host-Tests, wie
  `components/awdl`. Der lwIP-Teil wird auf der Hardware getestet.

## 7. Etappen

Alle Etappen werden zuerst unter Linux auf Ihrer Hardware abgenommen.

| Etappe | Inhalt | abgenommen, wenn |
|---|---|---|
| M4.1 | AWDL-Netif in lwIP, Betriebsart IP, `HELLO`, `ip`, `ping6` auf dem ESP | `ping6` vom ESP an das iPad bekommt Antworten; L2 mit `awdl_bridge.py` geht weiter; RAM-Zahlen bekannt |
| M4.2 | UDP-Proxy, Host-App-Grundgerüst, mDNS-Browser | `espdrop find` listet das iPad (Wecksignal vom ESP) |
| M4.3 | TCP verbinden, Kredite, OpenDrop-Client | `espdrop send datei` kommt am iPad an; Senderate gemessen |
| M4.4 | TCP lauschen, mDNS-Responder, OpenDrop-Server | iPad sieht „EspDrop“, Datei kommt an; Rate ≥ heute (~0,6 MB/s) |
| M4.5 | Windows | dieselben zwei Tests unter Windows |
| M4.6 | `capture` → pcapng, Puffer-Feinabstimmung, Anleitung | Mitschnitt in Wireshark lesbar; Doku fertig |

## 8. Risiken und offene Fragen

- **RAM** (Abschnitt 3.4): lwIP-Fenster gegen Bluetooth und Wi-Fi.
- **Durchsatz:** Ein kleines TCP-Fenster auf dem ESP kann bremsen, wenn AWDL
  in Schüben liefert (airdrop-mt7921 FINDINGS §18: ~68 ms Schub, ~450 ms Pause
  auf Kanal 149 mit dünner Sequenz). Auf dem C5 mit 15 von 16 Slots auf 44 ist
  das weniger ausgeprägt; gemessen wird in M4.3/M4.4.
- **Nachbarauflösung** mit Apple-Geräten in lwIP: Lern-Tabelle wie heute in
  der Bridge (3.1); ob iOS auf Neighbor Solicitations antwortet, zeigt M4.1.
- **CPU:** Der C5 hat einen Kern. AWDL-Zeitsteuerung, lwIP, USB und BLE teilen
  ihn; die AWDL-Task muss Vorrang behalten.
- **OpenDrop unter Windows:** `libarchive`-DLL, Pfade, Zertifikat-Erzeugung
  (OpenDrop ruft `openssl` auf).
- **Diagnose:** ohne `awdl0` kein tcpdump; dafür `capture` (3.5) und die
  L2-Betriebsart.

## 9. Entscheidungen, die ich von Ihnen brauche

| # | Frage | Vorschlag |
|---|---|---|
| 1 | mDNS auf dem Host (über UDP-Proxy) oder auf dem ESP? | **Host:** flexibler, auf dem PC zu debuggen, Firmware bleibt allgemein |
| 2 | Host-App in Python oder Rust? | **Python:** OpenDrop und die 17 Patches sind Python und bleiben unverändert nutzbar |
| 3 | L2-Betriebsart (`awdl0`) in der Firmware behalten? | **Ja**, als Diagnose-Modus; kostet nur die vorhandene Logik |
| 4 | OpenDrop als Bibliothek anbinden oder AirDrop selbst schreiben? | **Anbinden** (5.2); selbst schreiben erst, wenn OpenDrop im Weg steht |
| 5 | Bluetooth immer an oder nur für `wake` starten? | **Erst messen** (M4.1), dann entscheiden |
