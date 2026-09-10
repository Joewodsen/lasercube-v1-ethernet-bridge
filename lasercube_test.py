#!/usr/bin/env python3
"""
Gestufte Inbetriebnahme des LaserCube.

Jede Stufe ist einzeln aufrufbar und fuer sich aussagekraeftig - damit ein
Fehler zugeordnet und nicht gesucht werden muss. Die Stufen bauen aufeinander
auf; erst wenn eine sauber laeuft, ist die naechste sinnvoll.

    ./run.sh lasercube_test.py --ioreg     Stufe 0  kein USB-Zugriff, nur OS-Abfrage
                                                    (auf Linux: sysfs, s. --lsusb)
    ./run.sh lasercube_test.py --probe     Stufe 1  Deskriptoren, ohne Geraet zu oeffnen
    ./run.sh lasercube_test.py --info      Stufe 2  oeffnen und auslesen, Laser bleibt aus
    ./run.sh lasercube_test.py --dark      Stufe 3  streamen mit RGB=0, kein Licht
    ./run.sh lasercube_test.py --stats     Stufe 4  wie --dark, mit Fuellstandsanzeige
    ./run.sh lasercube_test.py --circle    Stufe 5  Kreis, Helligkeit gedeckelt
    ./run.sh lasercube_test.py --off                Not-Aus

Muss Python 3.9 vertragen (/usr/bin/python3 ist 3.9.6).
"""

import argparse
import os
import re
import subprocess
import sys
import time

import lasercube
from lasercube import LaserCube, LaserCubeError, LaserCubeStreamer

LD_VID = 0x1fc9
LD_PID = 0x04d8

SAFETY_BANNER = """
  ACHTUNG - der Laser gibt gleich Licht ab.
  Schutzbrille bzw. Sicherheitslinse benutzen, Strahl auf eine unbrennbare
  Flaeche richten, nicht auf Personen oder spiegelnde Objekte.
  Abbruch jederzeit mit Ctrl-C - der Ausgang geht dann sofort aus.
"""


# ---- Stufe 0: ioreg -------------------------------------------------------

def stage_ioreg() -> int:
    """Fragt nur das Betriebssystem - kein libusb, kein USB-Zugriff."""
    print("Stufe 0: ioreg (kein USB-Zugriff)")
    try:
        out = subprocess.check_output(["ioreg", "-p", "IOUSB", "-l", "-w0"],
                                      stderr=subprocess.DEVNULL).decode("utf-8", "replace")
    except (OSError, subprocess.CalledProcessError) as exc:
        print("  ioreg nicht ausfuehrbar: %s" % exc)
        return 1

    # ioreg gibt die IDs dezimal aus.
    blocks = re.split(r"\n(?=\s*\+-o )", out)
    found = []
    others = []
    for block in blocks:
        vid = re.search(r'"idVendor"\s*=\s*(\d+)', block)
        pid = re.search(r'"idProduct"\s*=\s*(\d+)', block)
        if not vid:
            continue
        name = re.search(r'"USB Product Name"\s*=\s*"([^"]*)"', block)
        label = name.group(1) if name else "(ohne Namen)"
        v = int(vid.group(1))
        p = int(pid.group(1)) if pid else -1
        if v == LD_VID and p == LD_PID:
            found.append((label, v, p))
        else:
            others.append((label, v, p))

    if found:
        for label, v, p in found:
            print("  GEFUNDEN: %s  %04x:%04x" % (label, v, p))
        print("\n  Das ist das USB-Datenmodell. Weiter mit --probe.")
        return 0

    print("  Kein LaserCube (%04x:%04x) am USB." % (LD_VID, LD_PID))
    if others:
        print("  Angeschlossen sind stattdessen:")
        for label, v, p in others:
            print("    %-28s %04x:%04x" % (label[:28], v, p if p >= 0 else 0))
    print("\n  Cube anstecken und einschalten, dann erneut versuchen.")
    return 1


# ---- Stufe 0 auf Linux: sysfs ---------------------------------------------

def stage_lsusb() -> int:
    """Linux-Pendant zu stage_ioreg: fragt nur den Kernel, kein libusb.

    Liest /sys/bus/usb/devices statt das Programm 'lsusb' aufzurufen - damit
    braucht Stufe 0 kein zusaetzliches Paket (usbutils) und funktioniert auf
    einem frisch aufgesetzten Raspberry Pi OS Lite sofort.
    """
    import glob

    print("Stufe 0: sysfs (kein USB-Zugriff)")
    found = []
    others = []
    for path in sorted(glob.glob("/sys/bus/usb/devices/*/idVendor")):
        base = os.path.dirname(path)

        def read(name, default=""):
            try:
                with open(os.path.join(base, name)) as fh:
                    return fh.read().strip()
            except OSError:
                return default

        try:
            v = int(read("idVendor", "-1"), 16)
            p = int(read("idProduct", "-1"), 16)
        except ValueError:
            continue
        label = (read("product") or read("manufacturer") or "(ohne Namen)")
        if v == LD_VID and p == LD_PID:
            found.append((label, v, p, base))
        else:
            others.append((label, v, p))

    if found:
        for label, v, p, base in found:
            print("  GEFUNDEN: %s  %04x:%04x" % (label, v, p))
            print("            %s" % base)
        print("\n  Das ist das USB-Datenmodell. Weiter mit --probe.")
        return 0

    print("  Kein LaserCube (%04x:%04x) am USB." % (LD_VID, LD_PID))
    if others:
        print("  Angeschlossen sind stattdessen:")
        for label, v, p in others:
            print("    %-28s %04x:%04x" % (label[:28], v, p))
    print("\n  Cube anstecken und einschalten, dann erneut versuchen.")
    return 1


def stage_stufe0() -> int:
    """Waehlt die Stufe-0-Abfrage passend zum Betriebssystem."""
    if sys.platform == "darwin":
        return stage_ioreg()
    return stage_lsusb()


# ---- Stufe 1: Deskriptoren ------------------------------------------------

def stage_probe() -> int:
    """Liest die Deskriptoren, ohne das Geraet zu oeffnen."""
    print("Stufe 1: Deskriptoren lesen (Geraet wird nicht geoeffnet)\n")
    text = lasercube.descriptor_dump()
    print(text.rstrip())

    if "Kein LaserCube" in text:
        return 1

    print("\n  Erwartet wird:")
    print("    Interface 0        : Bulk EP 0x01 OUT und 0x81 IN")
    print("    Interface 1, Alt 1 : Bulk EP 0x03 OUT")

    if "ISOCHRON" in text:
        print("\n  Hinweis: es gibt isochrone Endpoints (oben markiert).")
        print("  Der Treiber fasst die nie an - er setzt Alt-Setting 1 und")
        print("  benutzt ausschliesslich Bulk. Das ist Absicht, siehe README.")
    return 0


# ---- Stufe 2: oeffnen und auslesen ----------------------------------------

def stage_info() -> int:
    """Oeffnet das Geraet und liest die Kennwerte. Der Laser bleibt aus."""
    print("Stufe 2: oeffnen und auslesen (Laser bleibt aus)\n")
    laser = LaserCube()
    try:
        laser.open()
        info = laser.info
        print("  Seriennummer        : %s" % (info.get("serial") or "(keine)"))
        print("  Firmware            : %s.%s" % (info.get("fw_major"), info.get("fw_minor")))
        print("  DAC-Rate            : %s pps (max %s)"
              % (info.get("dac_rate"), info.get("max_dac_rate")))
        print("  DAC-Wertebereich    : %s .. %s" % (info.get("dac_min"), info.get("dac_max")))
        print("  Ringbuffer          : %s Samples, davon frei %s"
              % (info.get("ringbuffer_size"), info.get("ringbuffer_free")))
        print("  Samples pro Bulk    : %s" % info.get("bulk_packet_samples"))
        print("  Ausgang eingeschaltet: %s" % laser.output_enabled())
        print("\n  Sehen die Werte plausibel aus, weiter mit --dark.")
        return 0
    finally:
        laser.close()


# ---- Stufe 3/4: dunkel streamen -------------------------------------------

def stage_stream(seconds: float, verbose: bool, frame_source, headline: str) -> int:
    laser = LaserCube()
    try:
        laser.open()
    except LaserCubeError as exc:
        print("  %s" % exc)
        return 1

    lasercube.install_signal_handlers(laser)
    streamer = LaserCubeStreamer(laser, frame_source=frame_source,
                                 max_rgb=ARGS.max_rgb, min_span=ARGS.min_span)
    print(headline)
    print("  Zielfuellstand %d Samples (~%.0f ms bei %d pps), Chunk %d Samples"
          % (streamer.target_fill,
             1000.0 * streamer.target_fill / max(1, laser.dac_rate),
             laser.dac_rate, streamer.chunk_samples))
    print()

    rc = 0
    try:
        streamer.start()
        start = time.time()
        next_tick = start + 1.0
        while time.time() - start < seconds:
            time.sleep(0.05)
            while not streamer.messages.empty():
                print("  MELDUNG: %s" % streamer.messages.get_nowait())
            if verbose and time.time() >= next_tick:
                next_tick += 1.0
                s = streamer.stats
                elapsed = time.time() - start
                print("  t=%4.1fs  Fuellstand %5d  Pakete %6d  Frames %5d  "
                      "Underruns %3d  ~%6.0f pps"
                      % (elapsed, s["fill"], s["packets"], s["frames"],
                         s["underruns"],
                         s["packets"] * streamer.chunk_samples / max(elapsed, 0.001)))
    except KeyboardInterrupt:
        print("\n  Abbruch - Ausgang wird abgeschaltet.")
    finally:
        streamer.stop()
        laser.close()

    s = streamer.stats
    print("\n  Ergebnis: %d Pakete, %d Frames, %d Underruns, %d verworfene Frames"
          % (s["packets"], s["frames"], s["underruns"], s["rejected"]))
    if s["packets"] == 0:
        print("  FEHLER: es wurde kein einziges Paket gesendet.")
        rc = 1
    elif s["underruns"] > 0:
        print("  WARNUNG: der Puffer ist leergelaufen - der Strom reisst ab.")
        print("  Bei Grafik spaeter wuerde das als Ruckeln sichtbar.")
    else:
        print("  Sauber: der Sample-Strom ist nicht abgerissen.")
    return rc


def stage_dark(seconds: float, verbose: bool) -> int:
    return stage_stream(
        seconds, verbose, None,
        "Stufe %s: geblanktes Streamen fuer %.0f s - RGB ist 0, es darf KEIN\n"
        "  Licht kommen. Die Galvos sind dabei hoerbar."
        % ("4" if verbose else "3", seconds))


# ---- Stufe 5: Kreis -------------------------------------------------------

def stage_circle(seconds: float, verbose: bool) -> int:
    print(SAFETY_BANNER)
    try:
        answer = input("  Weiter? [j/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer not in ("j", "ja", "y", "yes"):
        print("  Abgebrochen.")
        return 1

    colour = (ARGS.max_rgb, 0, 0)   # rot, bereits gedeckelt

    def frame_source():
        return lasercube.make_circle(radius=0.8, num_points=300,
                                     r=colour[0], g=colour[1], b=colour[2])

    return stage_stream(
        seconds, verbose, frame_source,
        "Stufe 5: roter Kreis fuer %.0f s, Helligkeit gedeckelt auf %d/255."
        % (seconds, ARGS.max_rgb))


# ---- Not-Aus --------------------------------------------------------------

def stage_off() -> int:
    print("Not-Aus: Ausgang abschalten und Ringbuffer leeren")
    laser = LaserCube()
    try:
        laser.open()
        laser.disable_output()
        laser.clear_ringbuffer()
        print("  Ausgang ist aus.")
        return 0
    except LaserCubeError as exc:
        print("  %s" % exc)
        return 1
    finally:
        laser.close()


# ---- CLI ------------------------------------------------------------------

ARGS = None


def main() -> int:
    global ARGS
    parser = argparse.ArgumentParser(
        description="Gestufte Inbetriebnahme des LaserCube V1 (USB).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ioreg", action="store_true", help="Stufe 0: nur OS-Abfrage")
    group.add_argument("--lsusb", action="store_true",
                       help="Stufe 0 auf Linux: nur Kernel-Abfrage ueber sysfs")
    group.add_argument("--probe", action="store_true", help="Stufe 1: Deskriptoren")
    group.add_argument("--info", action="store_true", help="Stufe 2: oeffnen und auslesen")
    group.add_argument("--dark", action="store_true", help="Stufe 3: dunkel streamen")
    group.add_argument("--stats", action="store_true", help="Stufe 4: dunkel streamen mit Anzeige")
    group.add_argument("--circle", action="store_true", help="Stufe 5: Kreis mit Licht")
    group.add_argument("--off", action="store_true", help="Not-Aus")
    parser.add_argument("--seconds", type=float, default=10.0,
                        help="Dauer der Streaming-Stufen (Standard 10)")
    parser.add_argument("--max-rgb", type=int, default=40, dest="max_rgb",
                        help="Helligkeitsdeckel 0..255 (Standard 40)")
    parser.add_argument("--min-span", type=float, default=0.02, dest="min_span",
                        help="Punkt-Waechter: minimale Ausdehnung, 0 schaltet ab")
    ARGS = parser.parse_args()

    try:
        if ARGS.ioreg or ARGS.lsusb:
            # --ioreg waehlt auf Linux automatisch die sysfs-Abfrage, damit der
            # in der README dokumentierte Stufe-0-Aufruf ueberall funktioniert.
            return stage_lsusb() if ARGS.lsusb else stage_stufe0()
        if ARGS.probe:
            return stage_probe()
        if ARGS.info:
            return stage_info()
        if ARGS.dark:
            return stage_dark(ARGS.seconds, False)
        if ARGS.stats:
            return stage_dark(ARGS.seconds, True)
        if ARGS.circle:
            return stage_circle(ARGS.seconds, True)
        if ARGS.off:
            return stage_off()
    except LaserCubeError as exc:
        print("\nFEHLER: %s" % exc)
        return 1
    except KeyboardInterrupt:
        print("\nAbgebrochen.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
