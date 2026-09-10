#!/usr/bin/env python3
"""
LaserCube V1 (LaserDock, Micro-USB) - Python-Treiber.

Spricht ueber ctypes mit dem Wrapper (build/ldwrapper.dylib auf macOS,
build/ldwrapper.so auf Linux), der seinerseits libusb benutzt.
Protokoll rekonstruiert aus Wickedlasers/laserdocklib.

WICHTIG - Architektur, NUR auf macOS:
    Dort laedt dieses Modul eine x86_64-dylib und muss deshalb mit einem
    x86_64-Python laufen. Immer ueber laser/run.sh starten:

        ./run.sh lasercube_test.py --info

    Der Homebrew-Python 3.13 ist arm64 und kann die dylib NICHT laden.
    Hintergrund: ein frueherer Versuch mit direktem libusb-Zugriff aus
    arm64-Python hat den Mac mit einer Kernel Panic abgeschossen. Siehe README.

Auf Linux (z.B. dem Raspberry Pi) entfaellt das komplett: der Wrapper wird
mit ./build_wrapper_linux.sh nativ gegen die System-libusb gebaut und laeuft
im normalen python3. Kein run.sh noetig.

Der Code muss Python 3.9 vertragen (/usr/bin/python3 ist 3.9.6) - also keine
match-Statements und keine "X | Y"-Typunions.
"""

import atexit
import ctypes
import math
import os
import platform
import queue
import signal
import struct
import sys
import threading
import time

# ---- Befehls-Opcodes (aus LaserdockDevice.cpp) ----------------------------

CMD_SET_OUTPUT          = 0x80
CMD_GET_OUTPUT          = 0x81
CMD_SET_DAC_RATE        = 0x82
CMD_GET_DAC_RATE        = 0x83
CMD_MAX_DAC_RATE        = 0x84
CMD_SAMPLE_ELEMENT_CNT  = 0x85
CMD_ISO_PACKET_CNT      = 0x86   # nur zur Kenntnis - isochron wird nie benutzt
CMD_MIN_DAC_VALUE       = 0x87
CMD_MAX_DAC_VALUE       = 0x88
CMD_RINGBUFFER_SIZE     = 0x89
CMD_RINGBUFFER_FREE     = 0x8A
CMD_VERSION_MAJOR       = 0x8B
CMD_VERSION_MINOR       = 0x8C
CMD_CLEAR_RINGBUFFER    = 0x8D
CMD_BULK_PACKET_SAMPLES = 0x8E

MAX_COORD = 4095          # 12 Bit DAC
BYTES_PER_SAMPLE = 8

# Am Geraet gemessen (FW 3.7, Ringbuffer 768):
#   30000 pps  0 Underruns
#   45000 pps  0 Underruns, 100% vom Soll   <- Standard, mit Reserve
#   50000 pps  0 Underruns, 100% vom Soll   <- geht auch, weniger Reserve
#   64000 pps  reisst ab, real nur ~55000 - das Geraet MELDET 64000, kann es aber
#              ueber USB nicht beliefert bekommen. Nicht einstellen.
# Hoehere Rate = mehr Punkte pro Bild = sauberere Ecken und dichtere Linien.
DEFAULT_DAC_RATE = 45000
MEASURED_MAX_DAC_RATE = 50000

# Fehlercodes des Wrappers, gespiegelt aus ldwrapper.c
_ERRORS = {
    -1:  "libusb_init fehlgeschlagen",
    -2:  "kein LaserCube (1fc9:04d8) gefunden - angeschlossen und eingeschaltet?",
    -3:  "libusb_open fehlgeschlagen",
    -4:  "Interface 0 nicht belegbar - laeuft noch eine andere Laser-Software?",
    -5:  "Interface 1 nicht belegbar - laeuft noch eine andere Laser-Software?",
    -6:  "Alt-Setting 1 auf Interface 1 nicht setzbar",
    -10: "Geraet ist nicht geoeffnet",
    -11: "unsinnige Argumente",
    -12: "USB-Transferfehler",
    -13: "zu wenige Bytes uebertragen",
    -14: "Geraet meldet Fehler (Statusbyte != 0)",
}

# Der Wrapper heisst je nach System anders. ldwrapper.c ist portabel (nur
# stdint/stdio/string + libusb.h), auf Linux wird er nativ gegen die
# System-libusb gebaut - dort gibt es weder Rosetta noch Architekturkonflikt.
_LIB_NAME = "ldwrapper.dylib" if sys.platform == "darwin" else "ldwrapper.so"
_LIB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "build", _LIB_NAME)
_BUILD_HINT = ("./build_wrapper.sh" if sys.platform == "darwin"
               else "./build_wrapper_linux.sh")


class LaserCubeError(Exception):
    pass


# ---- Wrapper laden --------------------------------------------------------

def _load_library():
    """Laedt den Wrapper und uebersetzt die typischen Ladefehler in Klartext."""
    if not os.path.exists(_LIB_PATH):
        raise LaserCubeError(
            "%s fehlt (%s).\n"
            "Erst bauen:  %s" % (_LIB_NAME, _LIB_PATH, _BUILD_HINT))
    try:
        lib = ctypes.CDLL(_LIB_PATH)
    except OSError as exc:
        msg = str(exc)
        # Der Architektur-Hinweis gilt nur auf macOS: dort ist der Wrapper
        # x86_64 und braucht Rosetta. Auf Linux wird nativ gebaut.
        if (sys.platform == "darwin"
                and ("incompatible architecture" in msg
                     or "mach-o" in msg.lower())):
            raise LaserCubeError(
                "Falsche Architektur: Python laeuft als '%s', die dylib ist "
                "x86_64.\n"
                "Immer ueber laser/run.sh starten (nutzt "
                "'arch -x86_64 /usr/bin/python3').\n"
                "Original-Fehler: %s" % (platform.machine(), msg))
        raise LaserCubeError("%s nicht ladbar: %s" % (_LIB_NAME, msg))

    lib.ld_open.restype = ctypes.c_int
    lib.ld_open.argtypes = []
    lib.ld_close.restype = None
    lib.ld_close.argtypes = []
    lib.ld_is_open.restype = ctypes.c_int
    lib.ld_is_open.argtypes = []
    lib.ld_last_rv.restype = ctypes.c_int
    lib.ld_last_rv.argtypes = []
    lib.ld_get_u8.restype = ctypes.c_int
    lib.ld_get_u8.argtypes = [ctypes.c_uint8, ctypes.POINTER(ctypes.c_uint8)]
    lib.ld_set_u8.restype = ctypes.c_int
    lib.ld_set_u8.argtypes = [ctypes.c_uint8, ctypes.c_uint8]
    lib.ld_get_u32.restype = ctypes.c_int
    lib.ld_get_u32.argtypes = [ctypes.c_uint8, ctypes.POINTER(ctypes.c_uint32)]
    lib.ld_set_u32.restype = ctypes.c_int
    lib.ld_set_u32.argtypes = [ctypes.c_uint8, ctypes.c_uint32]
    lib.ld_send.restype = ctypes.c_int
    lib.ld_send.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    lib.ld_cmd_raw.restype = ctypes.c_int
    lib.ld_cmd_raw.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p]
    lib.ld_serial.restype = ctypes.c_int
    lib.ld_serial.argtypes = [ctypes.c_char_p, ctypes.c_int]
    lib.ld_descriptor_dump.restype = ctypes.c_int
    lib.ld_descriptor_dump.argtypes = [ctypes.c_char_p, ctypes.c_int]
    return lib


_lib = None


def _get_lib():
    global _lib
    if _lib is None:
        _lib = _load_library()
    return _lib


def _check(rv: int, what: str):
    if rv != 0:
        raise LaserCubeError("%s: %s (Code %d, libusb %d)"
                             % (what, _ERRORS.get(rv, "unbekannter Fehler"),
                                rv, _get_lib().ld_last_rv()))


def descriptor_dump() -> str:
    """Deskriptorstruktur aller angeschlossenen LaserCubes als Text.

    Oeffnet das Geraet NICHT - liest nur, was das Betriebssystem beim
    Enumerieren ohnehin schon gelesen hat. Damit laesst sich vorab pruefen,
    wo isochrone Endpoints sitzen, bevor irgendetwas belegt wird.
    """
    buf = ctypes.create_string_buffer(16384)
    rv = _get_lib().ld_descriptor_dump(buf, len(buf))
    if rv < 0:
        _check(rv, "Deskriptoren lesen")
    return buf.value.decode("utf-8", "replace")


# ---- Punkte packen --------------------------------------------------------

def _to_u12(v: float) -> int:
    """Float -1.0..+1.0 auf den 12-Bit-DAC-Bereich abbilden."""
    if v < -1.0:
        v = -1.0
    elif v > 1.0:
        v = 1.0
    return int((v + 1.0) / 2.0 * MAX_COORD)


def _clamp8(v) -> int:
    v = int(v)
    if v < 0:
        return 0
    if v > 255:
        return 255
    return v


def pack_point(x: float, y: float, r, g, b) -> bytes:
    """Ein Sample: 8 Byte, little-endian, Reihenfolge rg / b / x / y.

    x, y : Float -1.0..+1.0, (0, 0) ist die Mitte
    r,g,b: 0..255

    Die X-Achse wird gespiegelt - genauso macht es ldCompressedSample.cpp
    in libLaserdockCore.
    """
    rg = _clamp8(r) | (_clamp8(g) << 8)
    return struct.pack("<HHHH", rg, _clamp8(b),
                       MAX_COORD - _to_u12(x), _to_u12(y))


def pack_point_raw(x: int, y: int, r, g, b) -> bytes:
    """Sample mit ROHEN Registerwerten - ohne Klemmen, ohne X-Spiegelung.

    Nur zum Ausmessen des Geraets gedacht. Das Uebertragungsformat gibt je
    Achse 16 Bit her (0..65535), der DAC ist aber 12 Bit - das Geraet meldet
    per 0x87/0x88 selbst 0..4095. Damit laesst sich pruefen, was die Firmware
    mit zu grossen Werten macht: maskieren (der Strahl springt auf die andere
    Seite) oder begrenzen (er bleibt am Rand stehen).

    Fuer alles andere pack_point() benutzen - das klemmt und spiegelt richtig.
    """
    return struct.pack("<HHHH", _clamp8(r) | (_clamp8(g) << 8), _clamp8(b),
                       int(x) & 0xFFFF, int(y) & 0xFFFF)


def pack_frame(points, max_rgb: int = 255) -> bytes:
    """Liste von (x, y, r, g, b) in einen Sample-Puffer packen.

    max_rgb deckelt die Helligkeit global - waehrend der Inbetriebnahme
    bewusst niedrig halten.
    """
    out = []
    for x, y, r, g, b in points:
        if max_rgb < 255:
            r = min(_clamp8(r), max_rgb)
            g = min(_clamp8(g), max_rgb)
            b = min(_clamp8(b), max_rgb)
        out.append(pack_point(x, y, r, g, b))
    return b"".join(out)


def _shift_colours(points, shift: int):
    """Farbdaten gegenueber den Positionen rotieren.

    Doppelt hier im Treiber, damit er ohne laser_render lauffaehig bleibt -
    die ausfuehrliche Begruendung steht in laser_render.shift_colours().
    """
    n = len(points)
    if not shift or n == 0:
        return points
    out = []
    for i in range(n):
        p = points[i]
        c = points[(i + shift) % n]
        out.append((p[0], p[1], c[2], c[3], c[4]))
    return out


def frame_is_safe(points, min_span: float = 0.02) -> bool:
    """Punkt-Waechter: schuetzt vor einem stehenden Strahl.

    Ein Galvo-Laser erzeugt kein Bild, sondern bewegt einen einzelnen Punkt.
    Bleibt der Punkt stehen und ist dabei hell, brennt er sich als Fleck in
    das an - das ist die gefaehrlichste Fehlfunktion ueberhaupt.

    Liefert False, wenn alle leuchtenden Punkte auf einem Fleck von weniger
    als min_span (in -1..+1-Einheiten) liegen. min_span=0 schaltet die
    Pruefung ab, falls ein Einzelpunkt-Effekt wirklich gewollt ist.
    """
    if min_span <= 0:
        return True
    lit = [(x, y) for x, y, r, g, b in points if (r or g or b)]
    if not lit:
        return True   # alles dunkel ist immer sicher
    xs = [p[0] for p in lit]
    ys = [p[1] for p in lit]
    return (max(xs) - min(xs)) >= min_span or (max(ys) - min(ys)) >= min_span


def make_circle(radius: float = 0.9, num_points: int = 200,
                r=0, g=0, b=0):
    """Kreis als Punktliste. Standard ist RGB=0, also ein DUNKLER Kreis -
    die Galvos bewegen sich, es kommt aber kein Licht heraus."""
    pts = []
    for i in range(num_points):
        a = 2.0 * math.pi * i / num_points
        pts.append((radius * math.cos(a), radius * math.sin(a), r, g, b))
    return pts


# ---- Geraet ---------------------------------------------------------------

class LaserCube:
    """Zugriff auf den LaserCube.

    Nach open() ist der Ausgang AUS. Licht ist immer ein eigener, expliziter
    Schritt (enable_output()).
    """

    def __init__(self, dac_rate: int = DEFAULT_DAC_RATE):
        self.dac_rate = dac_rate
        self.info = {}
        self._open = False
        self._lib = _get_lib()
        # Alle Befehle laufen ueber DENSELBEN Bulk-Endpoint (Interface 0).
        # Der Streamer-Thread holt dort regelmaessig den Fuellstand, waehrend
        # andere Threads eigene Befehle absetzen - ohne diese Sperre wuerden
        # sich zwei Roundtrips ihre Antworten gegenseitig wegnehmen.
        # send_samples() braucht sie nicht: das laeuft ueber Interface 1.
        self._cmd_lock = threading.RLock()
        atexit.register(self.close)

    # -- Low-Level ---------------------------------------------------------
    def _get_u32(self, cmd: int, what: str) -> int:
        out = ctypes.c_uint32(0)
        with self._cmd_lock:
            _check(self._lib.ld_get_u32(cmd, ctypes.byref(out)), what)
        return out.value

    def _get_u8(self, cmd: int, what: str) -> int:
        out = ctypes.c_uint8(0)
        with self._cmd_lock:
            _check(self._lib.ld_get_u8(cmd, ctypes.byref(out)), what)
        return out.value

    def _set_u8(self, cmd: int, val: int, what: str):
        with self._cmd_lock:
            _check(self._lib.ld_set_u8(cmd, val), what)

    def _set_u32(self, cmd: int, val: int, what: str):
        with self._cmd_lock:
            _check(self._lib.ld_set_u32(cmd, val), what)

    def command_raw(self, payload: bytes) -> bytes:
        """Beliebigen Befehl absetzen, volle 64-Byte-Antwort zurueckgeben.

        Fuer Befehle, deren Antwort nicht u8 oder u32 ist. Der eigentliche
        Anwendungsfall ist die Challenge an den Sicherheitschip (0xB0/0xB1):
        die Original-Software prueft sie kryptografisch, und der Schluessel
        steckt im ATSHA204 des Geraets - beantworten kann sie also nur der
        Cube selbst.

        Das Statusbyte (Byte 1) wird NICHT geprueft, sondern mit
        zurueckgegeben: bei diesen Befehlen gehoert es zur Antwort.
        """
        if not self._open:
            raise LaserCubeError("Geraet ist nicht geoeffnet")
        if not payload or len(payload) > 64:
            raise LaserCubeError("Befehl muss 1..64 Byte lang sein")
        buf = ctypes.create_string_buffer(64)
        with self._cmd_lock:
            _check(self._lib.ld_cmd_raw(bytes(payload), len(payload), buf),
                   "Rohbefehl 0x%02x" % payload[0])
        return buf.raw[:64]

    # -- Auf/Zu -----------------------------------------------------------
    def open(self):
        """Oeffnen und initialisieren. Der Ausgang bleibt dabei aus.

        Reihenfolge wie in ldUSBHardware.cpp.
        """
        if self._open:
            return self
        _check(self._lib.ld_open(), "LaserCube oeffnen")
        self._open = True
        try:
            self.info["fw_major"] = self._get_u32(CMD_VERSION_MAJOR, "FW-Major lesen")
            self.info["fw_minor"] = self._get_u32(CMD_VERSION_MINOR, "FW-Minor lesen")

            self.clear_ringbuffer()

            self.info["bulk_packet_samples"] = self._get_u32(
                CMD_BULK_PACKET_SAMPLES, "Bulk-Paketgroesse lesen")
            self.info["max_dac_rate"] = self._get_u32(CMD_MAX_DAC_RATE, "max. DAC-Rate lesen")
            self.info["dac_min"] = self._get_u32(CMD_MIN_DAC_VALUE, "min. DAC-Wert lesen")
            self.info["dac_max"] = self._get_u32(CMD_MAX_DAC_VALUE, "max. DAC-Wert lesen")
            self.info["ringbuffer_size"] = self._get_u32(
                CMD_RINGBUFFER_SIZE, "Ringbuffer-Groesse lesen")
            self.info["ringbuffer_free"] = self.ringbuffer_free()

            # Ausgang explizit aus, BEVOR die Rate gesetzt wird.
            self.disable_output()

            max_rate = self.info["max_dac_rate"] or self.dac_rate
            if self.dac_rate > max_rate:
                self.dac_rate = max_rate
            self.set_dac_rate(self.dac_rate)
            self.info["dac_rate"] = self._get_u32(CMD_GET_DAC_RATE, "DAC-Rate lesen")

            buf = ctypes.create_string_buffer(256)
            if self._lib.ld_serial(buf, len(buf)) == 0:
                self.info["serial"] = buf.value.decode("ascii", "replace")
            else:
                self.info["serial"] = ""
        except Exception:
            self.close()
            raise
        return self

    def close(self):
        """Ausgang aus, Puffer leeren, USB freigeben. Idempotent, wirft nie."""
        if not self._open:
            return
        self._open = False
        for fn in (lambda: self._lib.ld_set_u8(CMD_SET_OUTPUT, 0),
                   lambda: self._lib.ld_set_u8(CMD_CLEAR_RINGBUFFER, 0)):
            try:
                fn()
            except Exception:
                pass
        try:
            self._lib.ld_close()
        except Exception:
            pass

    def __enter__(self):
        return self.open()

    def __exit__(self, *args):
        self.close()

    # -- Befehle -----------------------------------------------------------
    def enable_output(self):
        self._set_u8(CMD_SET_OUTPUT, 1, "Ausgang einschalten")

    def disable_output(self):
        self._set_u8(CMD_SET_OUTPUT, 0, "Ausgang ausschalten")

    def output_enabled(self) -> bool:
        return self._get_u8(CMD_GET_OUTPUT, "Ausgangszustand lesen") == 1

    def clear_ringbuffer(self):
        self._set_u8(CMD_CLEAR_RINGBUFFER, 0, "Ringbuffer leeren")

    def set_dac_rate(self, rate: int):
        self._set_u32(CMD_SET_DAC_RATE, rate, "DAC-Rate setzen")
        self.dac_rate = rate

    def ringbuffer_free(self) -> int:
        return self._get_u32(CMD_RINGBUFFER_FREE, "freien Ringbuffer lesen")

    def send_samples(self, buf: bytes):
        """Rohen Sample-Puffer senden. Laenge muss durch 8 teilbar sein."""
        if not self._open:
            raise LaserCubeError("Geraet ist nicht geoeffnet")
        if len(buf) % BYTES_PER_SAMPLE:
            raise LaserCubeError("Puffer ist kein Vielfaches von %d Byte"
                                 % BYTES_PER_SAMPLE)
        count = len(buf) // BYTES_PER_SAMPLE
        if count == 0:
            return
        _check(self._lib.ld_send(buf, count), "Samples senden")


# ---- Streamer -------------------------------------------------------------

class LaserCubeStreamer:
    """Haelt den Sample-Strom aufrecht - der Kern der Betriebssicherheit.

    Der Thread hoert nie auf zu senden. Liefert die Frame-Quelle nichts oder
    etwas Unsicheres, geht ein GEBLANKTER Kreis raus statt gar nichts: die
    Galvos laufen weiter, der Strahl bleibt dunkel. Ein stehender Punkt kann
    so nicht entstehen.

    Flussregelung ueber eine lokale Fuellstandsschaetzung, die regelmaessig
    per 0x8A am Geraet korrigiert wird. Blockierendes Vollschreiben waere
    einfacher, wuerde den Puffer aber dauerhaft voll halten - bei 6000 Samples
    und 30000 pps sind das 200 ms Latenz, viel zu traege fuer Beat-Effekte.
    """

    # Zielfuellstand in Samples: ~50 ms bei 30000 pps
    DEFAULT_TARGET_FILL = 1500
    REFRESH_EVERY = 16          # alle N Pakete den echten Fuellstand holen
    UNDERRUN_FRACTION = 0.10    # darunter gilt der Puffer als leergelaufen

    def __init__(self, laser: LaserCube, frame_source=None,
                 target_fill: int = None, max_rgb: int = 255,
                 min_span: float = 0.02, chunk_samples: int = None,
                 colour_shift: int = 0):
        self.laser = laser
        self.frame_source = frame_source
        self.max_rgb = max_rgb
        self.min_span = min_span
        # Farbversatz gegen Haken an den Linienenden: die Spiegel hinken dem
        # befohlenen Wert hinterher, die Diode schaltet sofort. Siehe
        # laser_render.shift_colours. An diesem Geraet gemessen: -8.
        self.colour_shift = int(colour_shift or 0)

        self.ringbuffer_size = laser.info.get("ringbuffer_size") or 6000
        bulk = laser.info.get("bulk_packet_samples") or 64

        # Groessere Pakete = weniger Einzeltransfers = weniger Overhead. Bei
        # hohen DAC-Raten ist genau dieser Overhead die Grenze, nicht die
        # USB-Bandbreite. Der Chunk muss aber ein Vielfaches der
        # Bulk-Paketgroesse sein und darf den Puffer nicht ueberfahren.
        if chunk_samples:
            self.chunk_samples = max(bulk, (chunk_samples // bulk) * bulk)
        else:
            # Am Geraet gemessen: mit bulk*2 (128) bleibt der Durchsatz bei
            # hohen Raten deutlich unter dem Soll, mit bulk*6 (384) erreicht
            # er 100%. Mehr als der halbe Ringbuffer geht nicht, sonst
            # ueberfaehrt ein einzelner Chunk den Zielfuellstand.
            self.chunk_samples = max(bulk, min(bulk * 6,
                                               (self.ringbuffer_size // 2 // bulk) * bulk))

        self.target_fill = target_fill or min(self.DEFAULT_TARGET_FILL,
                                              int(self.ringbuffer_size * 0.5))
        # Zielfuellstand plus ein Chunk muss in den Ringbuffer passen.
        headroom = self.ringbuffer_size - self.chunk_samples
        if self.target_fill > headroom:
            self.target_fill = max(self.chunk_samples, headroom)

        self.stop_event = threading.Event()
        self.messages = queue.Queue()
        self.stats = {"packets": 0, "frames": 0, "underruns": 0,
                      "rejected": 0, "fill": 0}

        self._blank = pack_frame(make_circle(radius=0.9, num_points=200))
        self._frame = self._blank
        self._pos = 0
        self._fill = 0.0
        self._last_tick = time.time()
        self._thread = None

    # -- Steuerung ---------------------------------------------------------
    def start(self):
        if self._thread is not None:
            return
        self.laser.clear_ringbuffer()
        self.laser.enable_output()
        self._fill = 0.0
        self._last_tick = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0):
        self.stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        try:
            self.laser.disable_output()
        except LaserCubeError:
            pass

    # -- intern ------------------------------------------------------------
    def _report(self, text: str):
        try:
            self.messages.put_nowait(text)
        except queue.Full:
            pass

    def _next_frame(self):
        """Naechstes Bild holen. Alles Zweifelhafte wird zu einem dunklen Kreis."""
        if self.frame_source is None:
            return self._blank
        try:
            points = self.frame_source()
        except Exception as exc:
            self._report("Frame-Quelle hat geworfen: %s" % exc)
            return self._blank
        if not points:
            return self._blank
        if isinstance(points, (bytes, bytearray)):
            # Bereits fertig gepackt - Weg fuers Ausmessen mit Rohwerten
            # (pack_point_raw). Punkt-Waechter und Helligkeitsdeckel greifen
            # dabei NICHT, das ist Absicht und nur fuer Messzwecke gedacht.
            if len(points) % BYTES_PER_SAMPLE:
                self._report("Roh-Frame ist kein Vielfaches von %d Byte"
                             % BYTES_PER_SAMPLE)
                return self._blank
            return bytes(points)
        if not frame_is_safe(points, self.min_span):
            self.stats["rejected"] += 1
            self._report("Frame verworfen: leuchtende Punkte liegen auf einem "
                         "Fleck (stehender Strahl) - geblankt gesendet")
            return self._blank
        try:
            if self.colour_shift:
                points = _shift_colours(points, self.colour_shift)
            return pack_frame(points, self.max_rgb)
        except Exception as exc:
            self._report("Frame nicht packbar: %s" % exc)
            return self._blank

    def _drain_estimate(self):
        """Fuellstand um das fortschreiben, was das Geraet seither ausgegeben hat."""
        now = time.time()
        dt = now - self._last_tick
        self._last_tick = now
        self._fill = max(0.0, self._fill - dt * self.laser.dac_rate)

    def _chunk(self) -> bytes:
        """Naechstes Stueck aus dem aktuellen Bild, zyklisch.

        Ist das Bild einmal durch, wird das naechste geholt - so entsteht ein
        nahtloser Strom ohne Luecke zwischen den Bildern.
        """
        need = self.chunk_samples
        out = []
        while need > 0:
            total = len(self._frame) // BYTES_PER_SAMPLE
            if total == 0:
                self._frame = self._blank
                continue
            take = min(need, total - self._pos)
            start = self._pos * BYTES_PER_SAMPLE
            out.append(self._frame[start:start + take * BYTES_PER_SAMPLE])
            self._pos += take
            need -= take
            if self._pos >= total:
                self._pos = 0
                self._frame = self._next_frame()
                self.stats["frames"] += 1
        return b"".join(out)

    def _run(self):
        underrun_floor = self.ringbuffer_size * self.UNDERRUN_FRACTION
        while not self.stop_event.is_set():
            self._drain_estimate()

            if self._fill >= self.target_fill:
                # Puffer ist voll genug - kurz warten statt zu draengeln.
                self.stop_event.wait(0.002)
                continue

            chunk = self._chunk()
            try:
                self.laser.send_samples(chunk)
            except LaserCubeError as exc:
                # Senden kaputt: dunkel ist der sichere Zustand.
                self._report("Senden fehlgeschlagen, Ausgang wird abgeschaltet: %s" % exc)
                try:
                    self.laser.disable_output()
                except LaserCubeError:
                    pass
                return

            n = len(chunk) // BYTES_PER_SAMPLE
            self._fill += n
            self.stats["packets"] += 1

            if self.stats["packets"] % self.REFRESH_EVERY == 0:
                try:
                    free = self.laser.ringbuffer_free()
                except LaserCubeError as exc:
                    self._report("Fuellstand nicht lesbar: %s" % exc)
                    continue
                self._fill = float(max(0, self.ringbuffer_size - free))
                self._last_tick = time.time()
                self.stats["fill"] = int(self._fill)
                if self._fill < underrun_floor:
                    self.stats["underruns"] += 1


# ---- Not-Aus --------------------------------------------------------------

def _install_signal_handlers(laser: LaserCube):
    """Ctrl-C und SIGTERM fuehren immer ueber das Abschalten des Ausgangs."""
    def handler(signum, frame):
        laser.close()
        raise KeyboardInterrupt()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


install_signal_handlers = _install_signal_handlers
