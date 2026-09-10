# Umsetzung: LaserOS steuert den USB-Cube über das Netz

Gebaut, installiert und am echten Gerät geprüft am 10.09.2026.
Der Plan dazu: [plan.md](plan.md).

**Ergebnis: LaserOS 0.18.1 auf dem Mac erkennt den Pi als Netzwerk-LaserCube,
authentifiziert ihn und meldet ihn als betriebsbereit.**

```
09:56:03.453   new network lasercube found with ip: "192.168.0.50"
09:56:03.461   device @ "192.168.0.50" needs authenticating...
09:56:03.621   device @ "192.168.0.50" Ready.
09:56:06.027   network h/w device initialised OK.
```

Auf dem Mac wurde dafür nichts installiert und nichts konfiguriert — außer
einem Schalter in den Systemeinstellungen (siehe Hürde 1).

---

## Der geprüfte Aufbau

| | |
|---|---|
| Pi | Raspberry Pi 3B+, Raspberry Pi OS Lite (Debian 13), aarch64 |
| Verzeichnis | `/home/pi/lasercube` |
| Dienst | `lasercube-netdev.service`, aktiv und beim Booten automatisch gestartet |
| Cube | LaserCube V1 `1fc9:04d8`, Firmware 3.7, Ringbuffer 768 Samples |
| Mac | LaserOS 0.18.1 |

Alle IP-Adressen in diesem Dokument sind Beispielwerte.

---

## Die zwei echten Hürden

Das Protokoll selbst war nach dem Plan in einem Zug richtig — beide Hürden
lagen woanders, und beide sahen zunächst aus wie ein Protokollfehler.

### Hürde 1: macOS blockierte LaserOS still

**Symptom:** LaserOS fand den Pi (`new network lasercube found`) und meldete
exakt 4 Sekunden später `*** COMMS LOST ***`. Dazwischen kam am Pi **kein
einziges Paket** an — nachgewiesen sowohl im Emulator-Log als auch per
`tcpdump` über alle Ports.

Das ergab zunächst keinen Sinn: Die Suche funktionierte ja, also lief
Netzwerkverkehr in beide Richtungen.

**Ursache:** macOS 26 „Lokales Netzwerk". LaserOS durfte **broadcasten** (so
fand es den Pi) und Antworten **empfangen**, aber jeder **Unicast ins LAN**
wurde vom System kommentarlos verworfen. Genau der erste `0x77`-Aufruf an
Port 45457 ist so ein Unicast.

Erschwerend: Die App bringt keine `NSLocalNetworkUsageDescription` mit, fragt
also nie sichtbar nach der Berechtigung — sie scheitert nur leise.

**Behebung:** Systemeinstellungen → Datenschutz & Sicherheit → **Lokales
Netzwerk** → LaserOS einschalten, danach LaserOS neu starten.

> Das ist der erste Punkt, den man prüfen sollte, wenn der Cube plötzlich nicht
> mehr auftaucht. Der Schalter fällt bei macOS-Updates gerne wieder um.

### Hürde 2: die Authentifizierung ist echt

Im offengelegten Quelltext von `libLaserdockCore` ist die Prüfung
abgeschaltet — ohne hinterlegte Callback-Funktion gilt jedes Gerät als
authentifiziert. Das war die Hoffnung in Risiko R1 des Plans.

**Die ausgelieferte LaserOS hat diese Prüfung.** Sie schickte eine
**zufällige** Challenge, nicht die im Quelltext fest verdrahtete:

```
01 e02e0000409c0000232708000000   fester ATSHA204-Kopf (Opcode 0x08 = MAC)
<32 Byte Zufall>                   32 Byte Zufall, bei jedem Start neu
aafd                              CRC16
```

Antwort: `Device Authenication Failed.`

**Behebung — der im Plan skizzierte Ausweg trägt:** Der alte USB-Cube hat
denselben Sicherheitschip. Der Pi rechnet nichts vor, sondern **reicht die
Challenge über USB an die echte Hardware durch** und gibt deren Antwort
zurück. Der Schlüssel bleibt, wo er hingehört — im Chip.

Dafür nötig:

1. **`ldwrapper.c`: `ld_cmd_raw()`** — ein generischer Befehls-Roundtrip, der
   die volle 64-Byte-Antwort liefert und das Statusbyte *nicht* prüft (bei
   diesen Befehlen gehört es zur Antwort). Gleiche Regeln wie überall in der
   Datei: nur synchrone Bulk-Transfers, endliche Timeouts, kein
   `set_configuration`, kein Reset.
2. **`lasercube.py`: `command_raw()`** — plus eine **Sperre um alle
   Befehle**. Sie laufen alle über denselben Bulk-Endpoint auf Interface 0;
   ohne Sperre nehmen sich der Streamer-Thread (Füllstand alle 16 Pakete) und
   der Netzwerk-Thread gegenseitig die Antworten weg. `send_samples()` läuft
   über Interface 1 und braucht sie nicht.
3. **`lasercube_netdev.py`: `_ask_chip()`** — das eigentliche Durchreichen.

Zwei Details waren dabei am Gerät auszumessen und stehen in keiner Quelle:

**Der Chip braucht Rechenzeit.** Gemessen:

| Wartezeit nach `0xB0` | was zurückkommt |
|---|---|
| 0 ms | die Antwort der **vorigen** Runde |
| 20 ms | ein kurzes Statuspaket (Länge 4) |
| **50 ms** | **die richtige MAC (Länge 0x23 = 35)** |
| ab 100 ms | Nullen — die Antwort ist schon abgeholt |

Der Cube führt also eine Warteschlange. `_ask_chip()` räumt sie deshalb erst
leer, schickt dann die Challenge und pollt, bis das Längenbyte `0x23`
dasteht.

**Die Nutzdaten beginnen bei Offset 3,** nicht 2:

```
[0] 0xB1 Opcode-Echo   [1] Status   [2] 0x00   [3] 0x23 Länge   [4..] MAC + CRC
```

Erwartet wird `resp[3:38]` — 35 Byte **inklusive** des Längenbytes. Genau so
steht es auch im Quellcode-Kommentar von libLaserdockCore.

**Gegenprobe, dass das wirklich der Chip ist:** Auf die im Quelltext
dokumentierte Standard-Challenge antwortete unser Cube byte-genau mit der dort
als erwartet notierten Antwort (`23 34 8e 0c f3 01 6e 65 …`). Ein unbekannter
Opcode (`0xBF`) liefert dagegen Status `0xFF`. Der Weg ist echt, nichts ist
nachgebaut.

---

## Ein Fehler, der teuer gewesen wäre

Der Emulator schaltete anfangs bei `0x80 00` („unscharf") den **physischen
Ausgang** des Cubes ab. Folge: Das Gerät holt dann keine Samples mehr aus
seinem 768er-Ringpuffer, der läuft in Sekundenbruchteilen voll, der nächste
Bulk-Transfer blockiert bis zum Timeout (`libusb -7`) und der Streamer-Thread
beendet sich. Der Laser war danach tot, bis der Dienst neu startete — sichtbar
nur daran, dass `usb_pakete` nicht mehr stieg.

**Richtig ist die bewährte Betriebsart:** Der Ausgang bleibt eingeschaltet,
solange der Streamer läuft. „Unscharf" heißt **geblankter Kreis** — Galvos in
Bewegung, Diode aus. Das ist ohnehin der sicherere Zustand.

Dazu kam ein Beobachtungsproblem: Der Streamer schreibt Fehler in eine Queue,
statt sie zu werfen. Wird sie nicht ausgelesen, stirbt sein Thread lautlos.
Der Emulator liest sie jetzt ins Log und hat zusätzlich einen **Wachhund**,
der einen gestorbenen Streamer meldet und neu startet.

---

## Messwerte

Geprüft mit `test_netdev.py`, das die Original-Software nachspielt — inklusive
ihrer Flusskontrolle (nie mehr senden, als das Gerät als frei gemeldet hat).
30 Sekunden am echten Cube, 45.000 pps:

| | |
|---|---|
| gesendet | 8.921 Pakete, 1.216.500 Punkte |
| Pufferantworten zurück | 8.910 von 8.921 |
| verworfene Pakete | **0** |
| Pufferüberläufe | **0** |
| Underruns | **0** |
| Warteschlange | pendelt um 700–900 Samples ≈ **18 ms Latenz** |
| Puffer frei, Minimum | 1.152 von 6.000 |

### Die virtuelle Puffergröße

Mit 3.000 Samples lief der Puffer regelmäßig über (1,2 % Verlust). Mit
**6.000** — der Größe eines echten Netzwerk-Cubes — ist er sauber. Das kostet
**keine** Latenz: Die Software füllt ihn ohnehin nur ein paar tausend Samples
tief, die gemeldete Größe gibt nur Luft nach oben. Gemessene Latenz bleibt bei
~18 ms.

---

## Bedienung

```bash
# Status und Live-Log
ssh pi 'systemctl status lasercube-netdev'
ssh pi 'journalctl -u lasercube-netdev -f'

# Neu starten
ssh pi 'sudo systemctl restart lasercube-netdev'

# Helligkeitsdeckel ändern (0 = garantiert kein Licht)
ssh pi 'sudo sed -i "s/--max-rgb [0-9]*/--max-rgb 40/" \
         /etc/systemd/system/lasercube-netdev.service \
         && sudo systemctl daemon-reload && sudo systemctl restart lasercube-netdev'

# Jeden Befehl protokollieren (zur Fehlersuche, nicht im Dauerbetrieb)
ssh pi 'cd ~/lasercube && sudo systemctl stop lasercube-netdev \
         && python3 -u lasercube_netdev.py --verbose'

# Protokoll vom Mac aus durchmessen (ohne Licht)
cd ~/lasercube && python3 test_netdev.py --seconds 20

# NOT-AUS
ssh pi 'sudo systemctl stop lasercube-netdev'
```

Der Dienst startet **immer unscharf**. Licht gibt es erst, wenn LaserOS
ausdrücklich `0x80 01` schickt — und auch dann nur, solange Sample-Pakete
ankommen: Bleiben sie länger als 0,4 s aus, wird schwarz gezeichnet.

---

## Was noch offen ist

**Es ist bis hierher nie Licht gekommen.** Alle Messungen liefen mit
`--max-rgb 0` oder unscharf. Der erste Betrieb mit Licht — Geometrie, Farben,
Spiegelung der X-Achse — steht noch aus und gehört an den Anfang der ersten
Sitzung mit Sicherheitslinse.

Zur X-Spiegelung: Der Emulator wendet sie bewusst **nicht** an, weil LaserOS
sie schon selbst in die Samples hineinrechnet. Ist das Bild seitenverkehrt,
ist das die Stelle — nachzuprüfen mit einem asymmetrischen Testbild.

Die anderen beiden Betriebsarten (`lasercube-bridge` auf UDP 45460 für die
eigene DMX-App, `lasercube-web` auf HTTP 8770) sind auf diesem Pi noch nicht
als Dienste installiert. Der Code liegt bereit; `Conflicts=` ist in der
netdev-Unit schon eingetragen, sodass sie sich später gegenseitig
ausschließen.
