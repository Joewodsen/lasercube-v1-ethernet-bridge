#!/usr/bin/env python3
"""
Netzwerk-LaserCube-Emulator: der Pi gibt sich als LAN/WiFi-LaserCube aus.

Laeuft auf dem Raspberry Pi, der per USB am alten LaserCube V1 haengt. Er
beantwortet das Hersteller-Protokoll der neuen Netzwerk-Cubes, sodass die
ORIGINALE LaserCube-Software auf dem Mac ihn im LAN findet und benutzt, als
waere er ein Netzwerkgeraet. Auf dem Mac muss dafuer nichts installiert oder
konfiguriert werden.

    python3 lasercube_netdev.py                  # Vollbetrieb
    python3 lasercube_netdev.py --no-usb         # Stufe A: nur Protokoll, kein Laser
    python3 lasercube_netdev.py --max-rgb 40     # Helligkeitsdeckel

PROTOKOLL (abgeleitet aus libLaserdockCore/LaserDockNetworkDevice.cpp, LGPL-3)
-----------------------------------------------------------------------------
Drei UDP-Ports:

  45456  "alive"    Broadcast-Suche. Rein: 0x27. Raus: genau 2 Byte 27 00.
  45457  Befehle    Antwort immer  opcode, 0x00 [, Nutzdaten].
  45458  Samples    0xa9 | 0x00 | msg u8 | frame u8 | Punkte a 10 Byte
                    (x, y, r, g, b - je uint16 little-endian, 12 Bit genutzt)

Das 64-Byte-Info-Paket auf 0x77 ist der kritische Teil; Laenge und Offsets
muessen exakt stimmen, sonst verwirft die Software es kommentarlos.

FLUSSREGELUNG
-------------
Die Software schickt einen ROHEN Sample-Strom und taktet ihn danach, wieviel
Platz das Geraet meldet. Der echte Ringpuffer des V1 fasst aber nur 768
Samples (~17 ms) - viel zu wenig fuer Netzwerk-Jitter. Der Pi meldet deshalb
einen VIRTUELLEN Puffer und hinterlegt ihn mit eigenem RAM:

    Netz --> Warteschlange auf dem Pi  --> LaserCubeStreamer --> 768er-Ring
             (gemeldet als rx_buffer)      (unveraendert)

Der grosse Puffer sitzt damit HINTER der Netzwerkstrecke und faengt den
Jitter ab. Die Meldung "frei" muss ehrlich sein: zu viel -> Warteschlange
laeuft ueber, zu wenig -> die Software drosselt und der Strom reisst ab.

SICHERHEIT
----------
  - Startet unscharf. Licht erst nach 0x80 01 von der Software.
  - Totmannschaltung: 0,4 s keine Sample-Pakete -> schwarz.
  - Helligkeitsdeckel --max-rgb auf jedem Punkt.
  - Stehstrahl-Waechter ueber ein Zeitfenster (siehe StandingBeamGuard).
  - Der Streamer hoert nie auf zu senden: ohne Daten geht ein GEBLANKTER
    Kreis raus - Galvos in Bewegung, Diode aus. Nie ein stehender Strahl.
"""

import argparse
import binascii
import collections
import json
import select
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, ".")

# ---- Protokollkonstanten --------------------------------------------------

ALIVE_PORT = 45456
CMD_PORT = 45457
DATA_PORT = 45458

CMD_GET_ALIVE = 0x27
CMD_GET_FULL_INFO = 0x77
CMD_ENABLE_BUFFER_SIZE_RESPONSE = 0x78
CMD_SET_OUTPUT = 0x80
CMD_GET_OUTPUT = 0x81
CMD_SET_ILDA_RATE = 0x82
CMD_GET_ILDA_RATE = 0x83
CMD_GET_MAX_ILDA_RATE = 0x84
CMD_GET_RINGBUFFER_EMPTY = 0x8A
CMD_CLEAR_RINGBUFFER = 0x8D
CMD_SET_NV_MODEL_INFO = 0x97
CMD_SET_DAC_BUF_THOLD = 0xA0
CMD_SECURITY_REQUEST = 0xB0
CMD_SECURITY_RESPONSE = 0xB1
CMD_SAMPLE_DATA = 0xA9

INFO_PACKET_SIZE = 64           # die Software prueft auf GENAU 64
NET_POINT = struct.Struct("<HHHHH")     # x, y, r, g, b  - je 12 Bit in 16
USB_POINT = struct.Struct("<HHHH")      # rg, b, x, y    - so will es der V1

# Verbindungsart: die Software rechnet +1 dazu. 2 -> CON_ETHERNET_CLIENT.
CONNECTION_ETHERNET_CLIENT = 2

# Die Sicherheits-Challenge, die libLaserdockCore fest verdrahtet mitschickt,
# wenn die Anwendung keine eigene Callback-Funktion gesetzt hat. Sie stammt
# laut Quellcode-Kommentar aus einem Wireshark-Mitschnitt eines USB-Cubes.
KNOWN_SECURITY_REQUEST = binascii.unhexlify(
    "01e02e0000409c00002327080000 00a12100 00ea350000754f000090 1f000040"
    "39 00009c6d0000f22d0000a26f000073c4".replace(" ", ""))

# Die zugehoerige, echte Antwort eines USB-LaserCube (Schluessel im sha204
# programmiert) - ebenfalls aus dem Quellcode-Kommentar.
# Die Antwort des Sicherheitschips im 64-Byte-USB-Paket:
#
#   [0] Opcode-Echo 0xB1
#   [1] Statusbyte des Cubes (0x00 = Befehl verstanden)
#   [2] immer 0x00
#   [3] Laengenbyte der ATSHA204-Antwort - bei einer MAC-Antwort 0x23 = 35
#   [4..] die eigentlichen 32 Byte MAC + 2 Byte CRC
#
# Am Geraet gemessen: die Nutzdaten, die die Software erwartet, sind
# resp[3:38] - also INKLUSIVE des Laengenbytes 0x23. Genau so stehen sie
# auch im Quellcode-Kommentar von libLaserdockCore ("23 34 8e 0c ...").
SECURITY_REPLY_OFFSET = 3
SECURITY_REPLY_LEN = 35
SECURITY_MAC_LEN_BYTE = 0x23

# Der Chip rechnet ~35 ms an einer MAC. Vorher gelesen, kommt entweder eine
# alte Antwort aus der Warteschlange oder ein kurzes Statuspaket zurueck.
SECURITY_POLL_DELAYS = (0.05, 0.03, 0.05, 0.1, 0.2)

KNOWN_SECURITY_RESPONSE = binascii.unhexlify(
    "23348e0cf301 6e652233fb3c565f3207fd19f41d09486f254c20b24f39bdcb20b909ce"
    .replace(" ", ""))


def own_ip(fallback="0.0.0.0"):
    """Eigene LAN-Adresse ermitteln, ohne einen Namen aufzuloesen."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]
    except OSError:
        return fallback
    finally:
        s.close()


class StandingBeamGuard:
    """Schuetzt vor einem stehenden, hellen Strahl.

    Anders als bei ganzen Bildern gibt es hier keine Bildgrenze, an der man
    pruefen koennte - es kommt ein durchlaufender Sample-Strom. Ein einzelner
    Block von ein paar hundert Samples DARF klein sein, das ist bei einem
    detailreichen Bild voellig normal. Gefaehrlich ist erst, wenn ueber ein
    ganzes ZEITFENSTER hinweg alle leuchtenden Samples auf einem Fleck
    liegen. Genau darauf prueft diese Klasse.
    """

    def __init__(self, min_span_u12: float, window: float = 0.2):
        self.min_span = min_span_u12
        self.window = window
        self.blocks = collections.deque()    # (t, minx, maxx, miny, maxy)
        self.tripped = False

    def check(self, minx, maxx, miny, maxy, lit_count) -> bool:
        """True = sicher. Nur leuchtende Samples duerfen hier eingehen."""
        now = time.time()
        if lit_count:
            self.blocks.append((now, minx, maxx, miny, maxy))
        while self.blocks and now - self.blocks[0][0] > self.window:
            self.blocks.popleft()
        if not self.blocks:
            self.tripped = False
            return True            # nichts leuchtet - immer sicher
        # Das Fenster muss auch wirklich voll sein, sonst schlaegt der
        # Waechter direkt nach dem Einschalten an.
        if now - self.blocks[0][0] < self.window * 0.75:
            return not self.tripped
        span_x = max(b[2] for b in self.blocks) - min(b[1] for b in self.blocks)
        span_y = max(b[4] for b in self.blocks) - min(b[3] for b in self.blocks)
        safe = span_x >= self.min_span or span_y >= self.min_span
        self.tripped = not safe
        return safe


class NetworkCube:
    """Der Emulator selbst."""

    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()

        # -- Zustand, den die Software sieht --
        self.armed = False                  # 0x80 - startet AUS
        self.buffer_replies = False         # 0x78
        self.virtual_buffer = args.buffer
        self.dac_rate = args.dac_rate
        self.max_dac_rate = args.max_dac_rate
        self.fw_major, self.fw_minor = 3, 7
        self.serial6 = b"\x00" * 6
        self.ip = own_ip()

        # -- Sample-Warteschlange: Netz rein, USB raus --
        self.queue = collections.deque()
        self.last_data_at = 0.0

        self.guard = StandingBeamGuard(args.min_span * 2047.5)
        self.stats = {"pakete": 0, "samples": 0, "verworfen": 0,
                      "ueberlauf": 0, "leer": 0, "absender": None,
                      "geblankt": 0}

        self.laser = None
        self.streamer = None
        self.running = True
        self.security_reply = None
        self.seen_cmd_peers = set()
        self.seen_alive_peers = set()

    # -- Bruecke zum USB-Treiber -------------------------------------------

    def frame_source(self):
        """Wird vom Streamer-Thread immer wieder aufgerufen.

        Gibt fertig gepackte USB-Samples zurueck - oder None, wenn nicht
        scharf, die Verbindung steht oder die Warteschlange leer ist. Der
        Streamer sendet dann von sich aus einen geblankten Kreis.
        """
        with self.lock:
            if not self.armed:
                return None
            if time.time() - self.last_data_at > self.args.dark_after:
                return None
            want = self.args.block
            have = len(self.queue)
            if have == 0:
                self.stats["leer"] += 1
                return None
            take = min(want, have)
            out = bytearray()
            for _ in range(take):
                out += self.queue.popleft()
            return bytes(out)

    # -- Netz -> USB, der heisse Pfad ---------------------------------------

    def _ingest(self, body: bytes):
        """Sample-Nutzlast umrechnen und in die Warteschlange legen.

        Die Original-Software packt denselben komprimierten USB-Sample nur
        breiter aus: x und y werden unveraendert uebernommen, r/g/b sind von
        8 auf 12 Bit hochgeschoben. Die Rueckrichtung ist damit verlustfrei -
        und die X-Spiegelung darf hier NICHT noch einmal angewandt werden,
        die Software hat sie schon drin.
        """
        cap = self.args.max_rgb
        minx = miny = 0xFFFF
        maxx = maxy = -1
        lit = 0
        packed = []
        for x, y, r, g, b in NET_POINT.iter_unpack(body):
            r >>= 4
            g >>= 4
            b >>= 4
            if r > cap:
                r = cap
            if g > cap:
                g = cap
            if b > cap:
                b = cap
            x &= 0x0FFF
            y &= 0x0FFF
            if r or g or b:
                lit += 1
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
                if y < miny:
                    miny = y
                if y > maxy:
                    maxy = y
            packed.append(USB_POINT.pack(r | (g << 8), b, x, y))

        if not self.guard.check(minx, maxx, miny, maxy, lit):
            # Stehender heller Strahl: Positionen behalten, Licht rausnehmen.
            self.stats["geblankt"] += 1
            packed = [USB_POINT.pack(0, 0, x, y)
                      for x, y, _r, _g, _b in NET_POINT.iter_unpack(body)]

        with self.lock:
            free = self.virtual_buffer - len(self.queue)
            if len(packed) > free:
                # Die Software haelt sich normalerweise an unsere Meldung.
                # Kommt es trotzdem dazu, ist Wegwerfen richtig: aufstauen
                # wuerde die Latenz dauerhaft hochziehen.
                self.stats["ueberlauf"] += len(packed) - max(0, free)
                packed = packed[:max(0, free)]
            self.queue.extend(packed)
            self.last_data_at = time.time()
            self.stats["samples"] += len(packed)

    def buffer_free(self) -> int:
        with self.lock:
            return max(0, self.virtual_buffer - len(self.queue))

    # -- Das 64-Byte-Info-Paket --------------------------------------------

    def info_packet(self) -> bytes:
        buf = bytearray(INFO_PACKET_SIZE)
        buf[0] = CMD_GET_FULL_INFO
        buf[1] = 0x00                     # Ergebnis OK
        buf[2] = 0x00                     # Protokollversion des Info-Pakets
        buf[3] = self.fw_major
        buf[4] = self.fw_minor
        # Flags ab FW 0.13: Bit0 Ausgang an, Bit1 Interlock, Bit2 Temp-Warnung,
        # Bit3 Uebertemperatur, Bit4-7 Paketfehler. Wir melden nur den Ausgang.
        buf[5] = 0x01 if self.armed else 0x00
        struct.pack_into("<I", buf, 10, self.dac_rate)
        struct.pack_into("<I", buf, 14, self.max_dac_rate)
        struct.pack_into("<H", buf, 19, min(0xFFFF, self.buffer_free()))
        struct.pack_into("<H", buf, 21, min(0xFFFF, self.virtual_buffer))
        buf[23] = 100                     # Akku in Prozent
        buf[24] = 30 & 0xFF               # Temperatur in Grad C
        buf[25] = CONNECTION_ETHERNET_CLIENT
        buf[26:32] = self.serial6
        try:
            buf[32:36] = socket.inet_aton(self.ip)
        except OSError:
            buf[32:36] = b"\x00\x00\x00\x00"
        buf[37] = self.args.model_number
        name = self.args.model_name.encode("ascii", "replace")[:25]
        buf[38:38 + len(name)] = name     # Rest ist schon 0 -> null-terminiert
        return bytes(buf)

    # -- Befehle auf Port 45457 --------------------------------------------

    def handle_cmd(self, data: bytes, addr, sock):
        if not data:
            return
        op = data[0]
        if self.args.verbose:
            log("<- %s:%d  Befehl 0x%02x  %s"
                % (addr[0], addr[1], op, data[:16].hex()))
        # Erstkontakt immer melden, auch ohne --verbose: das ist der Moment,
        # an dem sich entscheidet, ob die Software das Geraet annimmt.
        if addr[0] not in self.seen_cmd_peers:
            self.seen_cmd_peers.add(addr[0])
            log("Erstkontakt auf dem Befehlsport von %s (Befehl 0x%02x)"
                % (addr[0], op))

        if op == CMD_GET_FULL_INFO:
            sock.sendto(self.info_packet(), addr)

        elif op == CMD_ENABLE_BUFFER_SIZE_RESPONSE:
            self.buffer_replies = bool(len(data) > 1 and data[1])
            sock.sendto(bytes([op, 0x00]), addr)

        elif op == CMD_SET_OUTPUT:
            want = bool(len(data) > 1 and data[1])
            self.set_armed(want)
            sock.sendto(bytes([op, 0x00]), addr)

        elif op == CMD_GET_OUTPUT:
            sock.sendto(bytes([op, 0x00, 1 if self.armed else 0]), addr)

        elif op == CMD_SET_ILDA_RATE:
            if len(data) >= 5:
                rate = struct.unpack_from("<I", data, 1)[0]
                self.set_dac_rate(rate)
            sock.sendto(bytes([op, 0x00]), addr)

        elif op == CMD_GET_ILDA_RATE:
            sock.sendto(bytes([op, 0x00]) + struct.pack("<I", self.dac_rate), addr)

        elif op == CMD_GET_MAX_ILDA_RATE:
            sock.sendto(bytes([op, 0x00]) + struct.pack("<I", self.max_dac_rate), addr)

        elif op == CMD_GET_RINGBUFFER_EMPTY:
            sock.sendto(bytes([op, 0x00]) + struct.pack("<H", min(0xFFFF, self.buffer_free())),
                        addr)

        elif op == CMD_CLEAR_RINGBUFFER:
            with self.lock:
                self.queue.clear()
            sock.sendto(bytes([op, 0x00]), addr)

        elif op == CMD_SET_DAC_BUF_THOLD:
            sock.sendto(bytes([op, 0x00]), addr)

        elif op == CMD_SECURITY_REQUEST:
            self._handle_security_request(data, addr, sock)

        elif op == CMD_SECURITY_RESPONSE:
            self._handle_security_response(addr, sock)

        elif op == CMD_SET_NV_MODEL_INFO:
            sock.sendto(bytes([op, 0x00]), addr)

        else:
            log("unbekannter Befehl 0x%02x von %s: %s" % (op, addr[0], data.hex()))

    def _handle_security_request(self, data: bytes, addr, sock):
        """Die Challenge an den Sicherheitschip - wir reichen sie ueber USB
        an den echten Cube durch.

        LaserOS schickt hier eine ZUFAELLIGE Challenge an den ATSHA204 des
        Geraets und prueft die Antwort kryptografisch. Selbst ausrechnen
        koennen wir sie nicht: der Schluessel steckt im Chip und laesst sich
        nicht auslesen - genau dafuer ist er da.

        Beantworten kann sie aber der Cube, der bei uns per USB haengt: es
        ist derselbe Chip. Der Pi gibt hier also gar nichts vor, sondern
        leitet die Anfrage 1:1 an die echte Hardware weiter. Aufbau der
        Challenge (ATSHA204-MAC-Befehl):

            01                    Aufwecken des Chips
            e0 2e 00 00 40 9c 00 00 23 27 08 00 00 00
                                  fester Befehlskopf (Opcode 0x08 = MAC)
            <32 Byte>             die eigentliche Zufalls-Challenge
            <2 Byte>              CRC16

        Ohne angeschlossenen Cube (--no-usb) gibt es einen dokumentierten
        Ersatzwert, der aber nur zur fest verdrahteten Standard-Challenge
        aus libLaserdockCore passt.
        """
        challenge = data[1:]
        if self.laser is not None:
            self.security_reply = self._ask_chip(challenge)
        elif challenge == KNOWN_SECURITY_REQUEST:
            log("kein USB: bekannte Standard-Challenge -> dokumentierte "
                "Ersatzantwort")
            self.security_reply = None
        else:
            log("kein USB und unbekannte Challenge (%d Byte) - die "
                "Authentifizierung wird fehlschlagen" % len(challenge))
            self.security_reply = None
        # Quittung; danach holt die Software mit 0xb1 die Antwort ab.
        sock.sendto(bytes([CMD_SECURITY_REQUEST, 0x00]), addr)

    def _ask_chip(self, challenge: bytes):
        """Challenge an den Chip geben und die fertige MAC abholen.

        Der Cube fuehrt eine Warteschlange: liest man zu frueh, bekommt man
        die Antwort der VORIGEN Runde oder ein kurzes Statuspaket. Deshalb
        wird erst geleert, dann gefragt, dann gepollt, bis das Laengenbyte
        einer MAC-Antwort (0x23 = 35 Byte) dasteht. Am Geraet gemessen ist
        sie nach etwa 50 ms fertig.
        """
        try:
            # Reste aus einer frueheren Runde wegraeumen.
            for _ in range(4):
                old = self.laser.command_raw(bytes([CMD_SECURITY_RESPONSE]))
                if not any(old[SECURITY_REPLY_OFFSET:
                               SECURITY_REPLY_OFFSET + SECURITY_REPLY_LEN]):
                    break

            resp = self.laser.command_raw(bytes([CMD_SECURITY_REQUEST]) + challenge)
            if resp[1] != 0:
                log("Cube lehnt die Challenge ab, Status 0x%02x" % resp[1])
                return None

            for delay in SECURITY_POLL_DELAYS:
                time.sleep(delay)
                resp = self.laser.command_raw(bytes([CMD_SECURITY_RESPONSE]))
                body = resp[SECURITY_REPLY_OFFSET:
                            SECURITY_REPLY_OFFSET + SECURITY_REPLY_LEN]
                if body and body[0] == SECURITY_MAC_LEN_BYTE:
                    log("Challenge vom Cube beantwortet: %s" % body.hex())
                    return body
            log("Cube hat keine MAC-Antwort geliefert (Chip zu langsam?)")
            return None
        except Exception as exc:
            log("Challenge konnte nicht an den Cube gereicht werden: %s" % exc)
            return None

    def _handle_security_response(self, addr, sock):
        """Die Antwort des Chips abholen und weiterreichen.

        Format zur Software hin:  b1 | 0x00 ok | 0x00 ok | 35 Byte Antwort.
        Sie prueft beide Nullbytes und nimmt den Rest als Nutzdaten.
        """
        body = self.security_reply
        if body is None:
            body = KNOWN_SECURITY_RESPONSE
            log("Sicherheitsantwort: Ersatzwert (der Cube hat nichts geliefert)")
        sock.sendto(bytes([CMD_SECURITY_RESPONSE, 0x00, 0x00]) + body, addr)

    # -- Zustandswechsel ---------------------------------------------------

    def set_armed(self, want: bool):
        """0x80 der Software auf unseren Scharf-Zustand abbilden.

        WICHTIG: Der physische Ausgang des Cubes wird hier NICHT geschaltet.
        Er bleibt eingeschaltet, solange der Streamer laeuft - unscharf
        bedeutet ein GEBLANKTER Kreis, nicht Stillstand.

        Das ist nicht nur die sicherere Betriebsart (Galvos in Bewegung statt
        stehender Strahl), es ist auch technisch zwingend: bei
        abgeschaltetem Ausgang holt das Geraet keine Samples mehr aus seinem
        768er-Ringpuffer. Der laeuft dann in Sekundenbruchteilen voll, der
        naechste Bulk-Transfer blockiert bis zum Timeout (libusb -7) und der
        Streamer-Thread beendet sich - der Laser ist danach tot, bis der
        Dienst neu startet. Genau das ist beim Aufbau passiert.
        """
        if want == self.armed:
            return
        self.armed = want
        log("scharf: %s" % ("JA - die Software hat den Ausgang eingeschaltet"
                            if want else "nein, es wird geblankt gezeichnet"))

    def set_dac_rate(self, rate: int):
        # Das Geraet MELDET 64000, laesst sich ueber USB aber nur mit ~55000
        # beliefern und ist darueber dauerunterversorgt. Hart deckeln.
        rate = max(1000, min(int(rate), self.max_dac_rate))
        if rate == self.dac_rate:
            return
        self.dac_rate = rate
        log("DAC-Rate auf %d gesetzt" % rate)
        if self.laser is not None:
            try:
                self.laser.set_dac_rate(rate)
            except Exception as exc:
                log("DAC-Rate setzen fehlgeschlagen: %s" % exc)

    # -- Schleifen ---------------------------------------------------------

    def data_loop(self, sock):
        """Eigener Thread: der heisse Pfad, ~320 Pakete/s bei 45000 pps."""
        while self.running:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if self.args.allow and addr[0] != self.args.allow:
                continue
            if len(data) < 4 or data[0] != CMD_SAMPLE_DATA:
                continue
            body = data[4:]
            if len(body) % NET_POINT.size:
                self.stats["verworfen"] += 1
                continue
            self.stats["pakete"] += 1
            self.stats["absender"] = addr[0]
            self._ingest(body)
            if self.buffer_replies:
                sock.sendto(bytes([CMD_GET_RINGBUFFER_EMPTY, 0x00])
                            + struct.pack("<H", min(0xFFFF, self.buffer_free())),
                            addr)

    def control_loop(self, alive_sock, cmd_sock):
        while self.running:
            try:
                ready = select.select([alive_sock, cmd_sock], [], [], 0.5)[0]
            except OSError:
                break
            for sock in ready:
                try:
                    data, addr = sock.recvfrom(4096)
                except OSError:
                    continue
                if self.args.allow and addr[0] != self.args.allow:
                    continue
                if sock is alive_sock:
                    # Antwort muss GENAU 2 Byte sein, sonst verwirft die
                    # Software sie (DeviceAliveResponseValid).
                    if data and data[0] == CMD_GET_ALIVE:
                        sock.sendto(bytes([CMD_GET_ALIVE, 0x00]), addr)
                        if addr[0] not in self.seen_alive_peers:
                            self.seen_alive_peers.add(addr[0])
                            log("Suchanfrage von %s beantwortet (27 00)" % addr[0])
                else:
                    self.handle_cmd(data, addr, sock)

    # -- Betrieb -----------------------------------------------------------

    def status(self):
        s = dict(self.stats)
        s["scharf"] = self.armed
        s["warteschlange"] = len(self.queue)
        s["virt_puffer"] = self.virtual_buffer
        s["dac_rate"] = self.dac_rate
        if self.streamer is not None:
            st = self.streamer.stats
            s["underruns"] = st["underruns"]
            s["usb_pakete"] = st["packets"]
            s["usb_fuellstand"] = st["fill"]
        return s

    def run(self):
        alive_sock = udp_socket(self.args.host, ALIVE_PORT)
        cmd_sock = udp_socket(self.args.host, CMD_PORT)
        data_sock = udp_socket(self.args.host, DATA_PORT, rcvbuf=1 << 21,
                               timeout=0.5)

        log("lauscht auf %s: %d (alive) / %d (Befehle) / %d (Samples)"
            % (self.args.host, ALIVE_PORT, CMD_PORT, DATA_PORT))
        log("meldet sich als '%s' unter %s, Seriennummer %s"
            % (self.args.model_name, self.ip, self.serial6.hex().upper()))
        log("virtueller Puffer %d Samples (%.0f ms bei %d pps), max-rgb %d"
            % (self.virtual_buffer,
               1000.0 * self.virtual_buffer / max(1, self.dac_rate),
               self.dac_rate, self.args.max_rgb))

        t = threading.Thread(target=self.data_loop, args=(data_sock,), daemon=True)
        t.start()
        if self.laser is None:
            log("kein Laser: Warteschlange wird mit %d Samples/s simuliert "
                "abgeraeumt" % self.dac_rate)
            threading.Thread(target=self.simulated_drain_loop, daemon=True).start()
        threading.Thread(target=self._report_loop, daemon=True).start()
        threading.Thread(target=self._streamer_message_loop, daemon=True).start()
        threading.Thread(target=self._watchdog_loop, daemon=True).start()
        try:
            self.control_loop(alive_sock, cmd_sock)
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False

    def simulated_drain_loop(self):
        """Nur fuer --no-usb: die Warteschlange so leeren, wie es der echte
        Laser taete.

        Ohne das laeuft der virtuelle Puffer in Stufe A sofort voll und die
        Flussregelung laesst sich gar nicht pruefen - man wuerde nur messen,
        dass niemand die Samples abholt.
        """
        last = time.time()
        while self.running:
            time.sleep(0.005)
            now = time.time()
            n = int((now - last) * self.dac_rate)
            if n <= 0:
                continue
            last = now
            with self.lock:
                for _ in range(min(n, len(self.queue))):
                    self.queue.popleft()

    def _streamer_message_loop(self):
        """Meldungen des Streamers ins Log holen.

        Der Streamer schreibt Fehler in eine Queue statt sie zu werfen -
        wird sie nicht ausgelesen, stirbt sein Thread lautlos und man sieht
        nur, dass keine USB-Pakete mehr rausgehen.
        """
        import queue as _queue
        while self.running:
            if self.streamer is None:
                time.sleep(0.5)
                continue
            try:
                msg = self.streamer.messages.get(timeout=0.5)
            except _queue.Empty:
                continue
            log("Streamer: %s" % msg)

    def _watchdog_loop(self):
        """Passt auf, dass der Streamer-Thread lebt.

        Er beendet sich bei einem USB-Fehler von sich aus (nachdem er den
        Ausgang abgeschaltet hat). Ohne diese Ueberwachung merkt man das nur
        daran, dass "usb_pakete" nicht mehr steigt - der Laser steht dann
        still, die Software sendet aber munter weiter.
        """
        while self.running:
            time.sleep(2.0)
            st = self.streamer
            if st is None or st._thread is None:
                continue
            if st._thread.is_alive():
                continue
            log("*** Streamer-Thread ist gestorben - Neustart ***")
            try:
                st._thread = None
                st.stop_event.clear()
                self.laser.clear_ringbuffer()
                st.start()
                log("Streamer neu gestartet")
            except Exception as exc:
                log("Streamer-Neustart fehlgeschlagen: %s" % exc)
                time.sleep(3.0)

    def _report_loop(self):
        last = None
        while self.running:
            time.sleep(5.0)
            s = self.status()
            key = (s["pakete"], s["scharf"])
            if key == last and s["pakete"] == 0:
                continue
            last = key
            log("Status: " + json.dumps(s, ensure_ascii=False))


def udp_socket(host, port, rcvbuf=None, timeout=None):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    if rcvbuf:
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        except OSError:
            pass
    s.bind((host, port))
    if timeout:
        s.settimeout(timeout)
    return s


def log(text):
    print("[netdev] %s" % text, flush=True)


def main():
    p = argparse.ArgumentParser(
        description="Gibt den Pi im LAN als Netzwerk-LaserCube aus.")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--no-usb", action="store_true", dest="no_usb",
                   help="Stufe A: nur das Protokoll, ohne Laser anzufassen")
    p.add_argument("--max-rgb", type=int, default=255, dest="max_rgb",
                   help="Helligkeitsdeckel 0..255")
    p.add_argument("--dark-after", type=float, default=0.4, dest="dark_after",
                   help="Sekunden ohne Sample-Pakete bis geblankt wird")
    p.add_argument("--min-span", type=float, default=0.02, dest="min_span",
                   help="Stehstrahl-Waechter, 0 schaltet ihn ab")
    # 6000 ist die Groesse eines echten Netzwerk-Cubes. Genau darauf ist die
    # Flussregelung der Original-Software eingestellt - sie fuellt den Puffer
    # ohnehin nur ein paar tausend Samples tief, die gemeldete Groesse kostet
    # also keine Latenz, sondern gibt nur Luft nach oben.
    p.add_argument("--buffer", type=int, default=6000,
                   help="virtuelle Puffergroesse in Samples")
    p.add_argument("--block", type=int, default=384,
                   help="Samples je Abruf durch den USB-Streamer")
    p.add_argument("--dac-rate", type=int, default=45000, dest="dac_rate")
    p.add_argument("--max-dac-rate", type=int, default=45000, dest="max_dac_rate",
                   help="was wir der Software als Obergrenze melden")
    p.add_argument("--model-name", default="LaserCube", dest="model_name")
    p.add_argument("--model-number", type=int, default=1, dest="model_number")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="jeden Befehl protokollieren")
    p.add_argument("--allow", default=None,
                   help="nur Pakete von dieser IP annehmen")
    args = p.parse_args()

    cube = NetworkCube(args)

    if not args.no_usb:
        import lasercube
        from lasercube import LaserCube

        laser = LaserCube(dac_rate=args.dac_rate)
        laser.open()
        lasercube.install_signal_handlers(laser)
        cube.laser = laser

        cube.fw_major = int(laser.info.get("fw_major", 3))
        cube.fw_minor = int(laser.info.get("fw_minor", 7))
        cube.dac_rate = laser.dac_rate
        serial = laser.info.get("serial") or ""
        try:
            raw = binascii.unhexlify(serial.strip())
            cube.serial6 = (raw[-6:] if len(raw) >= 6
                            else raw.rjust(6, b"\x00"))
        except (binascii.Error, ValueError):
            cube.serial6 = serial.encode("ascii", "replace")[:6].rjust(6, b"\x00")

        log("USB-Cube offen: FW %d.%d, Ringbuffer %s, Seriennummer %s"
            % (cube.fw_major, cube.fw_minor,
               laser.info.get("ringbuffer_size"), serial))

        cube.streamer = lasercube.LaserCubeStreamer(
            laser, frame_source=cube.frame_source,
            max_rgb=args.max_rgb, min_span=0.0,   # Waechter sitzt im Emulator
            chunk_samples=args.block)
        cube.streamer.start()
        # Der Ausgang bleibt ab hier EINGESCHALTET - siehe set_armed(). Es
        # geht trotzdem kein Licht raus: unscharf heisst geblankter Kreis.
        log("Streamer laeuft, unscharf (geblankt) - wartet auf 0x80 01 der Software")

        try:
            cube.run()
        finally:
            cube.streamer.stop()
            laser.close()
    else:
        log("Stufe A: --no-usb, es wird kein Laser angefasst")
        cube.run()


if __name__ == "__main__":
    main()
