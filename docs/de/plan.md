# Plan: Den Pi zum "Netzwerk-LaserCube" machen, damit die Original-Software ihn findet

Ziel dieses Schritts: Die **originale LaserCube-Software auf dem Mac** soll den
alten USB-Cube ansteuern, ohne dass er per USB am Mac hängt — sie soll ihn im
Netzwerk finden und benutzen, als wäre er ein neuer LAN/WiFi-Cube.

Bisheriger Stand (siehe die Projektdokumentation): Die Kette
Mac → UDP 45460 → Pi → USB → Cube läuft, aber mit **eigenem** Protokoll
(`LCB1`) und **eigenem** Client (`laser_net.py`). Die Original-Software kennt
dieses Protokoll nicht. Genau diese Lücke schließt der Plan.

---

## 1. Der entscheidende Befund

Die neuen LaserCubes (WiFi/LAN, ESP32) sprechen ein offenes, dokumentiertes
UDP-Protokoll. Der Hersteller hat die **Client**-Seite selbst als Quelltext
veröffentlicht:

- `Wickedlasers/libLaserdockCore` → `3rdparty/laserdocklib/src/LaserDockNetworkDevice.cpp`
  — die Klasse, mit der die Original-Software mit einem Netzwerk-Cube redet.
  Enthält alle Opcodes, das Byte-Layout des Info-Pakets und die Flusskontrolle.
- `ldCore/src/Hardware/ldNetworkHardwareManager.cpp` — die Suche im LAN
  (Broadcast) und der Anmelde-/Initialisierungsablauf.
- Als kompakte Zweitquelle: der Proof-of-Concept-Gist von Sidney San Martín,
  der einen echten Cube über Netzwerk fernsteuert.

**Daraus folgt die ganze Idee:** Wir kennen jede Nachricht, die die
Original-Software an einen Netzwerk-Cube schickt, und jede Antwort, die sie
erwartet. Der Pi muss also nicht dolmetschen — er muss sich schlicht **als
Netzwerk-Cube ausgeben**. Die Mac-Seite bleibt komplett unverändert, es wird
dort nichts installiert, gepatcht oder konfiguriert.

Wichtig zur Einordnung: Auf GitHub gibt es mehrere **Clients** (TouchDesigner-
Skripte, der Gist), die Netzwerk-Cubes ansteuern. Einen **Emulator**, der die
Geräteseite nachbildet, gibt es nach dieser Recherche nicht. Den bauen wir.

```
   Mac, ORIGINALE LaserCube-Software           Raspberry Pi                LaserCube V1
   ─────────────────────────────────           ────────────                ────────────
   sucht per Broadcast nach Cubes              lasercube_netdev.py         Galvos + Dioden
        │  UDP 45456 (alive)          ────►    gibt sich als Cube aus            ▲
        │  UDP 45457 (Befehle)        ◄───►    beantwortet alle Opcodes          │
        │  UDP 45458 (Samples)        ────►    puffert Samples                   │
                                                     │                           │
                                               lasercube.py (unveraendert)       │
                                                     │ ctypes                    │
                                               ldwrapper.so ── USB ──────────────┘
```

Die unteren beiden Schichten (`lasercube.py`, `ldwrapper.so`) bleiben **exakt
wie sie sind**. Neu ist nur die oberste Schicht: statt `laser_bridge.py` mit
`LCB1` läuft `lasercube_netdev.py` mit dem Hersteller-Protokoll.

---

## 2. Was der Pi genau nachbilden muss

Drei UDP-Ports, alle auf `0.0.0.0`:

| Port | Zweck |
|---|---|
| **45456** | „alive" — hierauf kommt der Broadcast, mit dem die Software das LAN absucht |
| **45457** | Befehle (Info, Ausgang an/aus, ILDA-Rate, Pufferstand) |
| **45458** | Sample-Daten — die eigentlichen Punkte |

Die Software bindet auf ihrer Seite dieselben Portnummern; Antworten gehen also
immer **von dem Port zurück, auf dem die Anfrage kam**. Ein normaler
`sendto()` auf dem gebundenen Socket erledigt das von selbst.

### 2.1 Gefunden werden (Port 45456)

Die Software sendet auf **jede** Broadcast-Adresse aller aktiven Interfaces ein
einziges Byte `0x27`. Antwort muss **genau 2 Byte** sein:

```
0x27 0x00
```

Alles andere wird verworfen (`DeviceAliveResponseValid()` prüft Länge == 2,
Byte 0 == 0x27, Byte 1 == 0). Danach legt die Software für die Absender-IP ein
Geräteobjekt an und redet ab da auf Port 45457 weiter.

### 2.2 Das Info-Paket (Opcode `0x77`) — der kritischste Teil

Die Software fragt alle 250 ms (untätig) bzw. 2,5 s (aktiv) `0x77` ab. Bleibt
die Antwort **4 Sekunden** aus, gilt das Gerät als getrennt. Die Antwort muss
**exakt 64 Byte** lang sein, sonst wird sie kommentarlos ignoriert:

| Offset | Größe | Inhalt |
|---|---|---|
| 0 | 1 | `0x77` (Opcode-Echo) |
| 1 | 1 | `0x00` = Ergebnis OK (alles ≠ 0 wird als „Befehl fehlgeschlagen" verworfen) |
| 2 | 1 | `0x00` = Protokollversion des Info-Pakets — muss 0 sein |
| 3 | 1 | Firmware major |
| 4 | 1 | Firmware minor |
| 5 | 1 | Flags: Bit0 Ausgang an, Bit1 Interlock, Bit2 Temp-Warnung, Bit3 Übertemperatur, Bits 4–7 Paketfehler |
| 6–9 | 4 | ungenutzt |
| 10–13 | 4 | DAC-Rate, uint32 little-endian |
| 14–17 | 4 | max. DAC-Rate, uint32 LE |
| 18 | 1 | ungenutzt |
| 19–20 | 2 | freie Samples im Empfangspuffer, uint16 LE |
| 21–22 | 2 | Gesamtgröße des Empfangspuffers, uint16 LE |
| 23 | 1 | Akkustand in Prozent |
| 24 | 1 | Temperatur in °C, int8 |
| 25 | 1 | Verbindungsart − 1 (also `2` für „Ethernet-Client") |
| 26–31 | 6 | Seriennummer, 6 Rohbytes |
| 32–35 | 4 | eigene IP-Adresse, 4 Bytes |
| 36 | 1 | ungenutzt |
| 37 | 1 | Modellnummer |
| 38–63 | 26 | Modellname, ASCII, null-terminiert |

Die Flag-Bits gelten so ab Firmware 0.13 — wir melden deshalb eine Version
≥ 0.13, dann muss die ältere Bit-Belegung gar nicht erst nachgebaut werden.

**Was hier hineingehört, holen wir uns vom echten Gerät:** Firmware-Version,
Seriennummer und DAC-Rate liest `lasercube.py` ohnehin schon über USB aus
(Opcodes `0x8B`/`0x8C`, `0x83`). Der Pi meldet also die *echten* Werte seines
Cubes weiter, keine erfundenen — nur Puffergröße und -füllstand sind virtuell
(Abschnitt 3).

### 2.3 Die Befehle auf Port 45457

Antwortformat ist immer `Opcode, 0x00[, Nutzdaten…]`.

| Opcode | Bedeutung | Was der Pi tun muss |
|---|---|---|
| `0x77` | Info abfragen | 64-Byte-Paket aus 2.2 |
| `0x78` | Pufferstand-Antworten auf Datenpakete ein/aus (Byte 1 = 0/1) | Merken, quittieren |
| `0x80` | Ausgang setzen (Byte 1 = 0/1) | **Das ist das Scharfschalten** — auf `armed` abbilden |
| `0x81` | Ausgang lesen | Zustand zurück |
| `0x82` | ILDA-Rate setzen (4 Byte uint32 LE) | An `lasercube.py` weitergeben, gedeckelt |
| `0x83` / `0x84` | Rate / max. Rate lesen | Aus dem Gerät |
| `0x8a` | freie Samples im Ringpuffer | uint16 LE, 4-Byte-Antwort `8a 00 lo hi` |
| `0x8d` | Ringpuffer leeren | Eigene Warteschlange leeren |
| `0xa0` | DAC-Puffer-Schwelle setzen | Quittieren, sonst ignorieren |
| `0xb0` / `0xb1` | Sicherheits-Challenge | siehe Risiko R1 |

Beim Verbindungsaufbau schickt die Software fest diese Reihenfolge:
Ausgang **aus** → Pufferantworten **ein** → ILDA-Rate setzen. Erst danach
kommen Samples.

### 2.4 Die Sample-Pakete auf Port 45458

```
0xa9 | 0x00 | msg_num u8 | frame_num u8 | Punkt | Punkt | ...
```

Je Punkt **10 Byte**, fünf uint16 little-endian in dieser Reihenfolge:
`x, y, r, g, b`. Höchstens 140 Punkte pro Paket (1404 Byte, passt in die MTU).

### 2.5 Umrechnung Netz-Sample → USB-Sample

Hier steckt eine Feinheit, die einem sonst einen halben Tag kostet. Die
Original-Software erzeugt intern **denselben komprimierten USB-Sample** wie für
den Kabelbetrieb und packt ihn für das Netz nur breiter aus:

```c
uint16_t x = s.x;                              // unveraendert uebernommen
uint16_t y = s.y;                              // unveraendert uebernommen
uint16_t r = ((s.rg      & 0xff) << 4);        // 8 Bit -> 12 Bit
uint16_t g = (((s.rg>>8) & 0xff) << 4);
uint16_t b = (s.b << 4);
```

Die Rückrichtung auf dem Pi ist also **trivial und verlustfrei**:

```python
usb_x = x & 0x0fff          # KEINE X-Spiegelung!
usb_y = y & 0x0fff
r8, g8, b8 = r >> 4, g >> 4, b >> 4
rg = r8 | (g8 << 8)
sample = struct.pack("<HHHH", rg, b8, usb_x, usb_y)
```

**Achtung, Abweichung zum bestehenden Code:** `pack_point()` in `lasercube.py`
spiegelt X (`4095 - x`), weil das die Software sonst nicht tut. Auf diesem Weg
hat die Original-Software die Spiegelung **schon selbst angewandt**, bevor sie
die Samples ins Netz gibt. Der Emulator braucht deshalb eine zweite,
spiegelfreie Pack-Funktion. Wird sie versehentlich mitgespiegelt, steht am Ende
alles verkehrt herum an der Wand — kosmetisch, aber verwirrend. In Stufe D am
einfachsten mit einem asymmetrischen Testbild (Buchstabe „F") zu prüfen.

---

## 3. Das eigentliche Entwurfsproblem: 6000 gegen 768

Der bisherige Ansatz überträgt **ganze Bilder**, weil der Ringpuffer des Cubes
nur 768 Samples (~17 ms) fasst und jeder Netzwerkhänger darüber ein
Pufferunterlauf wäre. Diese Freiheit haben wir jetzt nicht mehr: die
Original-Software schickt einen **rohen Sample-Strom**, und sie taktet ihn
danach, wieviel Platz das Gerät meldet.

Genau das ist aber auch die Lösung. Ein echter Netzwerk-Cube hat rund **6000
Samples** Puffer, und die Software geht damit korrekt um — sie füllt nur bis zu
einer Schwelle und wartet dann. Der Pi meldet deshalb einen **virtuellen
Puffer** und hinterlegt ihn mit eigenem RAM:

```
Netz ──► Warteschlange auf dem Pi (z.B. 6000 Samples ≈ 130 ms)
              │  gemeldet als rx_buffer_size / rx_buffer_free
              ▼
         bestehender LaserCubeStreamer ──► echter 768-Sample-Ring ──► Galvos
```

Damit kehrt sich die alte Sorge um: Der große Puffer sitzt jetzt auf dem Pi,
**hinter** der Netzwerkstrecke, und fängt den Jitter ab. Der USB-Streamer
bedient den 768er-Ring weiterhin aus lokalem RAM — er merkt vom Netzwerk gar
nichts.

Die Meldung `frei = 6000 − Warteschlangenlänge` muss **ehrlich** sein, sonst
regelt die Software falsch: meldet der Pi zuviel Platz, überläuft die
Warteschlange (Latenz und verworfene Punkte); meldet er zuwenig, drosselt die
Software und der Strom reißt ab. Der Wert geht an zwei Stellen hinaus — im
Info-Paket (Offset 19/20) und als Antwort `0x8a` nach jedem Datenpaket, sobald
`0x78` eingeschaltet ist.

Die Puffergröße ist ein **Stellparameter**, kein Naturgesetz: 6000 sind ~130 ms
Latenz bei 45.000 pps. Für Beat-genaue Effekte ist das viel. Ab Stufe C wird
das heruntergeregelt (Startwert für die Erprobung: 2000 ≈ 45 ms), solange die
Underrun-Zahl bei 0 bleibt.

**Läuft die Warteschlange trotzdem leer**, gilt unverändert die bestehende
Regel: der Streamer hört nie auf zu senden, sondern gibt einen **geblankten
Kreis** aus. Galvos in Bewegung, Diode aus — nie ein stehender Strahl.

---

## 4. Sicherheit

Die vorhandenen Mechanismen bleiben alle in Kraft, sie bekommen nur neue
Auslöser:

| Mechanismus | Auslöser auf diesem Weg |
|---|---|
| Startet unscharf | Der Emulator startet mit `output = false`. Licht erst nach `0x80 01` von der Software |
| Totmannschaltung | Kommen 0,4 s keine Sample-Pakete mehr, wird schwarz gezeichnet — unabhängig davon, was `0x80` zuletzt gesagt hat |
| Helligkeitsdeckel | `--max-rgb` wirkt auf jeden umgerechneten Punkt, bevor er auf USB geht |
| Punkt-Wächter | unverändert aus `LaserCubeStreamer`: liegen alle hellen Punkte enger als `min_span`, wird das Bild verworfen |
| Dunkel ist der Ausfallzustand | `disable_output()` an `atexit`, Signal-Handlern und `finally`; systemd mit `KillSignal=SIGTERM` |

Ein Punkt kommt neu hinzu und ist wichtig: **Der Emulator antwortet auf
Broadcasts.** Jede Software im LAN, die `0x27` schickt, findet ihn — und
`0x80 01` reicht dann zum Einschalten. Das ist bei einem echten Netzwerk-Cube
genauso, ändert aber nichts daran, dass das Gerät nicht in ein offenes Netz
gehört. Vorgesehen: ein `--allow <IP>`, das Befehle und Daten auf eine
Absenderadresse begrenzt, standardmäßig aus.

---

## 5. Risiken und offene Punkte

**R1 — Authentifizierung (das einzige echte K.-o.-Risiko).**
Nach dem ersten Info-Paket geht das Geräteobjekt in den Zustand
`AUTHENTICATING` und die Software schickt eine Challenge (`0xb0`) an den
ATSHA204-Chip des Cubes, die Antwort kommt mit `0xb1` zurück. Im
veröffentlichten Quelltext ist die Prüfung **abgeschaltet** — ohne hinterlegte
Callback-Funktion wird jedes Gerät als authentifiziert akzeptiert. Ob die
ausgelieferte Mac-App diese Callback-Funktion gesetzt hat, ist von außen nicht
zu sehen; das ist der geschlossene Teil.

Es gibt aber einen Ausweg, und er ist ziemlich elegant: **Der alte USB-Cube hat
denselben Sicherheitschip.** Der Kommentar im Herstellercode sagt sogar
ausdrücklich, die Beispiel-Challenge sei „per Wireshark vom USB-LaserCube
mitgeschnitten", und nennt die erwartete Antwort für den USB-Schlüssel. Der Pi
müsste die Challenge also nicht beantworten, sondern nur **an den echten Cube
durchreichen** und dessen Antwort zurückgeben. Das ist mehr Arbeit (die
USB-Opcodes dafür sind noch nicht vermessen), aber es ist ein gangbarer Weg mit
echtem, gerätespezifischem Schlüssel.

Deshalb steht Stufe A weiter unten ganz vorne: Die Frage „reicht ein
Info-Paket, oder verlangt die App eine gültige Challenge-Antwort?" ist in
**einer halben Stunde und ohne jedes Risiko** beantwortet — der Emulator kann
das komplett ohne angeschlossenen Laser.

**R2 — DAC-Rate.** Der Cube *meldet* 64.000 pps, lässt sich über USB aber nur
mit ~55.000 füttern und ist darüber dauerunterversorgt. Der Emulator darf
deshalb als `max_dac_rate` **nicht** den echten Wert weiterreichen, sondern
muss 45.000 (bewährt) bis maximal 50.000 melden. Dann bietet die Software im
Bedienfeld gar nichts Höheres an. Ein trotzdem eintreffendes `0x82` wird
zusätzlich hart gedeckelt.

**R3 — X-Spiegelung.** Siehe 2.5. Kein Risiko, nur eine Falle.

**R4 — Modellname und -nummer.** Unbekannt ist, ob die Software anhand von
Modellnummer/-name Funktionen freischaltet oder sperrt (etwa maximale
Punktrate). Falls es klemmt, ist das der erste Stellparameter zum Durchprobieren.

**R5 — Paketrate.** Die Software sendet bis zu 20 UDP-Pakete am Stück. Bei
45.000 pps und 140 Punkten pro Paket sind das ~320 Pakete/s bei ~450 KB/s.
Nach den gemessenen 205 MiB in 15 s über eth0 parallel zum Laserstrom ist das
für den Pi 3B+ unkritisch — aber diesmal muss der Pi jedes Paket auch
*auspacken*, nicht nur weiterreichen. Python-Seite: `iter_unpack` über den
ganzen Paketkörper statt einer Schleife pro Punkt, das ist der einzige Ort, an
dem CPU-Zeit wirklich anfällt.

---

## 6. Umsetzung in Stufen

Jede Stufe ist für sich prüfbar, und die riskante Frage steht vorne.

### Stufe A — Erkennung, ganz ohne Laser
`lasercube_netdev.py` implementiert nur die drei Sockets, die Alive-Antwort und
ein Info-Paket mit fest verdrahteten Plausibelwerten. Kein USB, kein Cube am
Pi. Sample-Pakete werden nur gezählt.

*Prüfkriterium:* Die originale LaserCube-Software auf dem Mac zeigt ein Gerät
an. Zusätzlich `tcpdump -i eth0 -X udp portrange 45456-45458` auf dem Pi
mitlaufen lassen — das zeigt schwarz auf weiß, ob nach dem Info-Paket eine
`0xb0`-Challenge kommt und damit, ob R1 zuschlägt.

*Wenn es hier klemmt,* ist der Mitschnitt bereits die Diagnose, und es geht mit
dem Durchreichen der Challenge an den USB-Cube weiter, bevor irgendetwas
anderes gebaut wird.

### Stufe B — Sample-Pfad, dunkel
Warteschlange, Pufferstandsmeldung und Umrechnung Netz → USB dazu, an den
echten `LaserCubeStreamer` angeschlossen — aber **`max_rgb = 0`**. Der Laser
scannt, es kommt garantiert kein Licht.

*Prüfkriterium:* gehaltene 45.000 pps, 0 Underruns, keine verworfenen Pakete,
Warteschlange pendelt um den Zielwert. Also genau die Zahlen, die schon für die
`LCB1`-Brücke vorliegen — direkt vergleichbar.

### Stufe C — Flusskontrolle einregeln
Virtuelle Puffergröße von 6000 herunter, solange die Underruns bei 0 bleiben.
Ergebnis ist ein Zahlenwert plus die gemessene Latenz.

### Stufe D — Licht
Mit `--max-rgb 40` und Sicherheitslinse. Testbild „F" zur Kontrolle der
Spiegelung (R3), danach Geometrie und Farbe.

### Stufe E — Betrieb
Dritte systemd-Unit `lasercube-netdev.service`, mit `Conflicts=` gegen
`lasercube-bridge` **und** `lasercube-web` — alle drei brauchen dasselbe
USB-Gerät exklusiv. Gleiche `KillSignal=SIGTERM`-Begründung wie bisher.

Damit hat der Pi drei Betriebsarten:

| Dienst | Gegenstelle | Port |
|---|---|---|
| `lasercube-netdev` | **Originale LaserCube-Software** (dieser Plan) | UDP 45456/45457/45458 |
| `lasercube-bridge` | eigene DMX-Control-App (`LCB1`) | UDP 45460 |
| `lasercube-web` | Web-Oberfläche auf dem Pi | HTTP 8770 |

Die eigene Brücke wird also **nicht ersetzt.** Beide Wege bleiben nebeneinander
bestehen — die Original-Software für Handbetrieb und Kalibrierung, die eigene
Brücke für die musiksynchrone Ansteuerung. Kein Portkonflikt, da 45460 frei
bleibt.

---

## 7. Was am bestehenden Code geändert werden muss

Erfreulich wenig:

- **`ldwrapper.c`** — nichts.
- **`lasercube.py`** — eine zusätzliche, spiegelfreie Pack-Funktion; der
  Streamer muss eine Sample-Warteschlange als Quelle akzeptieren können statt
  nur eine Bildquelle. Sicherheitslogik unverändert.
- **`laser_bridge.py`** — nichts, bleibt wie es ist.
- **Neu: `lasercube_netdev.py`** — der Emulator, geschätzt 400–500 Zeilen.
- **Neu: `lasercube-netdev.service`** und `Conflicts=`-Zeilen in den beiden
  bestehenden Units.
- **Am Mac: nichts.** Das ist der ganze Witz.

---

## Quellen

- [Wickedlasers/libLaserdockCore — LaserDockNetworkDevice.cpp](https://github.com/Wickedlasers/libLaserdockCore/blob/master/3rdparty/laserdocklib/src/LaserDockNetworkDevice.cpp) (LGPL-3.0) — Opcodes, Info-Paket-Layout, Sample-Format, Flusskontrolle
- [Wickedlasers/libLaserdockCore — ldNetworkHardwareManager.cpp](https://github.com/Wickedlasers/libLaserdockCore/blob/master/ldCore/src/Hardware/ldNetworkHardwareManager.cpp) — Broadcast-Suche, Authentifizierungsablauf
- [Proof of Concept: LaserCube über Netzwerk steuern (Sidney San Martín)](https://gist.github.com/s4y/0675595c2ff5734e927d68caf652e3af) — kompakte, lauffähige Client-Implementierung
- [aronbg/LaserCubeTD](https://github.com/aronbg/LaserCubeTD) — TouchDesigner-Client für Netzwerk-Cubes, Zweitquelle für das Sample-Format
- [LaserCube FAQ (laseros.com)](https://www.laseros.com/faq/) — Netzwerkbetrieb ab Software-Version 0.7.3
