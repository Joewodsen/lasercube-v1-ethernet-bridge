#!/usr/bin/env python3
"""
Prueft den Netzwerk-Cube-Emulator, indem er die Original-Software nachspielt.

Laeuft auf dem MAC und redet ueber das Netz mit lasercube_netdev.py auf dem
Pi. Bildet genau den Ablauf nach, den libLaserdockCore geht:

    1. Broadcast 0x27 auf 45456, Antwort muss GENAU "27 00" sein
    2. 0x77 auf 45457, Antwort muss GENAU 64 Byte sein und sich mit dem
       Struct-Format der Software auspacken lassen
    3. Anmeldereihenfolge: Ausgang aus, Pufferantworten ein, ILDA-Rate setzen
    4. Sample-Pakete auf 45458 und pruefen, ob der Pufferstand zurueckkommt

Standardmaessig werden nur SCHWARZE Punkte gesendet (r=g=b=0) und der Ausgang
bleibt aus. --arm und --bright schalten das ausdruecklich frei.

    python3 test_netdev.py                     # Suche im LAN, dunkel
    python3 test_netdev.py --host 192.168.0.50 --seconds 10
"""

import argparse
import math
import re
import socket
import struct
import sys
import time

ALIVE_PORT, CMD_PORT, DATA_PORT = 45456, 45457, 45458

# Offsets exakt wie handleFullInfoPkt() in LaserDockNetworkDevice.cpp.
# Achtung: der bekannte Proof-of-Concept-Gist liegt bei den Bytes 2-4 um
# eins daneben (er liest die Protokollversion als fw_major). Ab Byte 10
# stimmen beide ueberein. Massgeblich ist der C++-Parser.
INFO = struct.Struct("<xxxBBB4xIIxHHBbB11xB26x")

OK, FAIL = "  OK  ", " FEHL "
failures = []


def check(name, cond, detail=""):
    print("[%s] %s%s" % (OK if cond else FAIL, name,
                         ("  -  " + detail) if detail else ""))
    if not cond:
        failures.append(name)
    return cond


def broadcast_addresses():
    """Gerichtete Broadcast-Adressen aller aktiven IPv4-Interfaces.

    Genau das macht RequestDeviceAlive(): es laeuft ueber die Interfaces und
    sendet an entry.broadcast(), NICHT an 255.255.255.255. Der Unterschied
    ist praktisch relevant - ein aktives VPN faengt die globale
    Broadcast-Adresse ab, die gerichtete geht weiter ueber das LAN.
    """
    out = []
    try:
        import subprocess
        txt = subprocess.run(["ifconfig"], capture_output=True, text=True).stdout
        for m in re.finditer(r"broadcast (\d+\.\d+\.\d+\.\d+)", txt):
            if m.group(1) not in out:
                out.append(m.group(1))
    except Exception:
        pass
    out.append("255.255.255.255")
    return out


def discover(timeout=2.0):
    """Broadcast 0x27 auf 45456 - genau wie RequestDeviceAlive()."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.4)
    targets = broadcast_addresses()
    found = {}
    end = time.time() + timeout
    while time.time() < end:
        for bc in targets:
            try:
                s.sendto(bytes([0x27]), (bc, ALIVE_PORT))
            except OSError:
                pass
        try:
            while True:
                data, addr = s.recvfrom(64)
                found[addr[0]] = data
        except socket.timeout:
            pass
    s.close()
    return found


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=None, help="IP des Pi (sonst Broadcast-Suche)")
    p.add_argument("--seconds", type=float, default=8.0)
    p.add_argument("--rate", type=int, default=30000)
    p.add_argument("--points", type=int, default=0,
                   help="Punkte je Bild (0 = aus Rate/fps errechnen)")
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--arm", action="store_true", help="Ausgang einschalten")
    p.add_argument("--bright", type=int, default=0, help="Helligkeit 0..255")
    args = p.parse_args()
    if not args.points:
        # Wer weniger schickt als die DAC-Rate verlangt, misst nicht die
        # Bruecke, sondern nur seine eigene Unterversorgung: die
        # Warteschlange laeuft leer und der Streamer blankt auf.
        args.points = int(args.rate / args.fps)

    print("=== 1. Suche per Broadcast (0x27 auf %d) ===" % ALIVE_PORT)
    print("    Ziele: %s" % ", ".join(broadcast_addresses()))
    found = discover()
    for ip, data in found.items():
        ok = len(data) == 2 and data[0] == 0x27 and data[1] == 0
        check("Alive-Antwort von %s" % ip, ok, "%d Byte: %s" % (len(data), data.hex()))
    host = args.host or (sorted(found)[0] if found else None)
    if not host:
        print("\nKein Geraet gefunden und kein --host angegeben.")
        return 1
    if not found:
        print("  (nichts per Broadcast gefunden - weiter mit --host %s)" % host)
    print("  -> benutze %s\n" % host)

    cmd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cmd.settimeout(2.0)
    data_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    data_sock.settimeout(2.0)

    def ask(payload, sock=None, port=CMD_PORT):
        sock = sock or cmd
        sock.sendto(bytes(payload), (host, port))
        try:
            return sock.recvfrom(1024)[0]
        except socket.timeout:
            return None

    print("=== 2. Info-Paket (0x77) ===")
    info = ask([0x77])
    if not check("Antwort kommt", info is not None):
        return 1
    if not check("Laenge ist genau 64", len(info) == 64, "%d Byte" % len(info)):
        return 1
    check("Opcode-Echo 0x77", info[0] == 0x77)
    check("Ergebnisbyte 0", info[1] == 0x00)
    check("Protokollversion 0", info[2] == 0x00)
    f = INFO.unpack(info)
    (fw_maj, fw_min, flags, dac, maxdac, free, size, batt, temp, con, model) = f
    out_en = bool(flags & 1)
    serial = ":".join("%02x" % b for b in info[26:32])
    ip_in_pkt = ".".join(str(b) for b in info[32:36])
    name = info[38:].split(b"\0", 1)[0].decode("ascii", "replace")
    print("      Firmware       %d.%d" % (fw_maj, fw_min))
    print("      Ausgang        %s   (Flags 0x%02x)" % (out_en, flags))
    print("      DAC-Rate       %d (max %d)" % (dac, maxdac))
    print("      Puffer         %d frei von %d" % (free, size))
    print("      Akku / Temp    %d%% / %d C" % (batt, temp))
    print("      Verbindung     %d  Modell %d '%s'" % (con, model, name))
    print("      Seriennummer   %s" % serial)
    print("      IP im Paket    %s" % ip_in_pkt)
    check("Modellname gesetzt", bool(name))
    check("max. DAC-Rate <= 50000 (64000 waere gefaehrlich)", maxdac <= 50000,
          "%d" % maxdac)
    check("Puffergroesse plausibel", 500 <= size <= 20000, "%d Samples" % size)
    print()

    print("=== 3. Anmeldereihenfolge wie ldNetworkHardware::initialize() ===")
    check("Ausgang aus (0x80 00)", ask([0x80, 0x00]) == bytes([0x80, 0x00]))
    check("Pufferantworten ein (0x78 01)", ask([0x78, 0x01]) == bytes([0x78, 0x00]))
    r = ask([0x82] + list(struct.pack("<I", args.rate)))
    check("ILDA-Rate setzen (0x82)", r == bytes([0x82, 0x00]))
    r = ask([0x8a])
    check("Pufferstand (0x8a)", r is not None and len(r) == 4 and r[0] == 0x8a,
          ("frei %d" % struct.unpack_from("<H", r, 2)[0]) if r and len(r) == 4 else "")
    print()

    print("=== 4. Sicherheits-Challenge (0xb0/0xb1) ===")
    import binascii
    chal = binascii.unhexlify(
        "01e02e0000409c0000232708000000a1210000ea350000754f0000901f00004039"
        "00009c6d0000f22d0000a26f000073c4")
    r = ask([0xb0] + list(chal))
    check("Challenge quittiert", r == bytes([0xb0, 0x00]),
          r.hex() if r else "keine Antwort")
    r = ask([0xb1])
    check("Antwort abholbar", r is not None and len(r) == 38 and r[0] == 0xb1,
          ("%d Byte" % len(r)) if r else "keine Antwort")
    print()

    if args.arm:
        print("!!! ARM: der Ausgang wird eingeschaltet, Helligkeit %d" % args.bright)
        check("Ausgang ein (0x80 01)", ask([0x80, 0x01]) == bytes([0x80, 0x00]))
    else:
        print("(ohne --arm: Ausgang bleibt aus, Punkte sind schwarz)")
    print()

    print("=== 5. Sample-Strom fuer %.0f s ===" % args.seconds)
    print("      %d Punkte/Bild x %.0f Bilder/s = %d Punkte/s (DAC-Rate %d)"
          % (args.points, args.fps, int(args.points * args.fps), args.rate))
    sent_pkts = sent_pts = 0
    replies = 0
    free_seen = []
    msg = frame = 0
    buf_free = 0
    end = time.time() + args.seconds
    period = 1.0 / args.fps
    data_sock.settimeout(0.0)
    while time.time() < end:
        t0 = time.time()
        pts = []
        for i in range(args.points):
            a = 2 * math.pi * i / args.points
            wob = 0.75 + 0.15 * math.sin(6 * a + t0 * 2)
            x = int((math.cos(a) * wob / 2 + 0.5) * 0xFFF)
            y = int((math.sin(a) * wob / 2 + 0.5) * 0xFFF)
            c = args.bright << 4
            pts.append(struct.pack("<HHHHH", x, y, c, c // 2, 0))
        body = b"".join(pts)
        for off in range(0, len(body), 140 * 10):
            # Flusskontrolle wie processSamples() in LaserDockNetworkDevice:
            # nie mehr schicken, als das Geraet als frei gemeldet hat. Ohne
            # das misst man nur, wie schnell man selbst senden kann.
            while buf_free < 140:
                try:
                    r = data_sock.recv(64)
                    if len(r) == 4 and r[0] == 0x8a:
                        replies += 1
                        buf_free = struct.unpack_from("<H", r, 2)[0]
                        free_seen.append(buf_free)
                except (BlockingIOError, socket.timeout):
                    # Nichts Neues da - fortschreiben, was seither
                    # ausgegeben worden sein muss, und kurz warten.
                    time.sleep(0.002)
                    buf_free += int(0.002 * args.rate)
                if time.time() > end:
                    break
            chunk = body[off:off + 140 * 10]
            data_sock.sendto(bytes([0xa9, 0x00, msg & 0xFF, frame & 0xFF]) + chunk,
                             (host, DATA_PORT))
            buf_free -= len(chunk) // 10
            msg += 1
            sent_pkts += 1
            sent_pts += len(chunk) // 10
        frame += 1
        try:
            while True:
                r = data_sock.recv(64)
                if len(r) == 4 and r[0] == 0x8a:
                    replies += 1
                    buf_free = struct.unpack_from("<H", r, 2)[0]
                    free_seen.append(buf_free)
        except (BlockingIOError, socket.timeout):
            pass
        dt = period - (time.time() - t0)
        if dt > 0:
            time.sleep(dt)

    data_sock.settimeout(2.0)
    print("      gesendet       %d Pakete, %d Punkte, %d Bilder" % (sent_pkts, sent_pts, frame))
    print("      Pufferantworten %d" % replies)
    if free_seen:
        print("      Puffer frei    min %d  max %d  Mittel %d"
              % (min(free_seen), max(free_seen), sum(free_seen) // len(free_seen)))
    check("Pufferantworten kommen zurueck", replies > sent_pkts * 0.5,
          "%d von %d" % (replies, sent_pkts))
    check("Puffer laeuft nicht dauerhaft voll",
          bool(free_seen) and min(free_seen) > 0,
          "min %d" % min(free_seen) if free_seen else "keine Daten")
    check("Punktrate erreicht das Soll",
          sent_pts >= args.rate * args.seconds * 0.9,
          "%d von %d" % (sent_pts, int(args.rate * args.seconds)))
    print()

    print("=== 6. Aufraeumen ===")
    check("Ausgang aus", ask([0x80, 0x00]) == bytes([0x80, 0x00]))
    check("Pufferantworten aus", ask([0x78, 0x00]) == bytes([0x78, 0x00]))
    info = ask([0x77])
    if info and len(info) == 64:
        check("Info meldet Ausgang aus", info[5] & 1 == 0)

    print()
    if failures:
        print("FEHLGESCHLAGEN (%d): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("Alle Pruefungen bestanden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
