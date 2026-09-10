#!/usr/bin/env python3
"""
Selbsttest fuer den Streamer gegen einen SIMULIERTEN Cube - ohne Hardware.

Der Streamer ist das sicherheitskritische Stueck: er muss den Sample-Strom
aufrechterhalten und darf niemals einen stehenden hellen Punkt ausgeben.
Diese Eigenschaften hier zu pruefen ist deutlich billiger, als sie am echten
Laser zu entdecken.

Aufruf:  ./run.sh test_streamer.py
"""

import struct
import sys
import threading
import time

sys.path.insert(0, ".")

import lasercube
from lasercube import BYTES_PER_SAMPLE, LaserCubeStreamer, make_circle

FAILED = []


def check(name, got, want):
    if got == want:
        print("  ok    %s" % name)
    else:
        print("  FEHL  %s\n          erhalten %r\n          erwartet %r" % (name, got, want))
        FAILED.append(name)


def check_true(name, cond, hint=""):
    check(name + (" " + hint if hint else ""), bool(cond), True)


class FakeCube(object):
    """Simuliert einen LaserCube: zaehlt Samples und leert den Puffer in Echtzeit."""

    def __init__(self, dac_rate=30000, ringbuffer_size=6000):
        self.dac_rate = dac_rate
        self.info = {"ringbuffer_size": ringbuffer_size,
                     "bulk_packet_samples": 64}
        self.lock = threading.Lock()
        self.sent = []              # alle gesendeten Samples als (rg, b, x, y)
        self.total_samples = 0
        self.output = False
        self.cleared = 0
        self._fill = 0.0
        self._last = time.time()

    def _drain(self):
        now = time.time()
        self._fill = max(0.0, self._fill - (now - self._last) * self.dac_rate)
        self._last = now

    def clear_ringbuffer(self):
        with self.lock:
            self.cleared += 1
            self._fill = 0.0

    def enable_output(self):
        self.output = True

    def disable_output(self):
        self.output = False

    def ringbuffer_free(self):
        with self.lock:
            self._drain()
            return int(self.info["ringbuffer_size"] - self._fill)

    def send_samples(self, buf):
        n = len(buf) // BYTES_PER_SAMPLE
        with self.lock:
            self._drain()
            self._fill += n
            self.total_samples += n
            for i in range(n):
                self.sent.append(struct.unpack_from("<HHHH", buf, i * BYTES_PER_SAMPLE))
            # Speicher begrenzen, sonst laeuft der Test voll
            if len(self.sent) > 200000:
                del self.sent[:100000]


def lit_points(fake):
    """Alle Samples, die tatsaechlich Licht abgeben."""
    return [s for s in fake.sent if s[0] or s[1]]


def run_stream(frame_source, seconds=1.0, **kwargs):
    fake = FakeCube()
    st = LaserCubeStreamer(fake, frame_source=frame_source, **kwargs)
    st.start()
    time.sleep(seconds)
    st.stop()
    return fake, st


def main():
    print("Streamer gegen simulierten Cube\n")

    # --- 1. Strom laeuft ueberhaupt, und mit plausibler Rate ---------------
    fake, st = run_stream(None, 1.0)
    rate = fake.total_samples / 1.0
    check_true("Strom laeuft (Pakete gesendet)", st.stats["packets"] > 0)
    check_true("Rate liegt nahe der DAC-Rate", 0.5 * fake.dac_rate < rate < 1.6 * fake.dac_rate,
               "(%.0f pps bei %d pps Soll)" % (rate, fake.dac_rate))
    check("Ausgang wurde eingeschaltet", st.stats["packets"] > 0 and fake.cleared > 0, True)
    check("Ausgang ist nach stop() aus", fake.output, False)

    # --- 2. Latenz bleibt beschraenkt -------------------------------------
    check_true("Fuellstand bleibt unter dem Ziel + einem Chunk",
               st.stats["fill"] <= st.target_fill + st.chunk_samples,
               "(Fuellstand %d, Ziel %d)" % (st.stats["fill"], st.target_fill))

    # --- 3. Ohne Frame-Quelle kommt garantiert kein Licht ------------------
    check("ohne Frame-Quelle ist alles dunkel", len(lit_points(fake)), 0)

    # --- 4. Frame-Quelle wird tatsaechlich benutzt -------------------------
    calls = [0]

    def source():
        calls[0] += 1
        return make_circle(radius=0.8, num_points=100, r=200)

    fake2, st2 = run_stream(source, 1.0)
    check_true("Frame-Quelle wird abgefragt", calls[0] > 0, "(%d Aufrufe)" % calls[0])
    check_true("Licht kommt an", len(lit_points(fake2)) > 0)

    # --- 5. Helligkeitsdeckel greift --------------------------------------
    fake3, _ = run_stream(source, 0.5, max_rgb=40)
    reds = set(s[0] & 0xFF for s in lit_points(fake3))
    check_true("max_rgb=40 deckelt wirklich", reds and max(reds) <= 40,
               "(hoechster Rotwert %s)" % (max(reds) if reds else "-"))

    # --- 6. Punkt-Waechter: heller Einzelpunkt wird geblankt ---------------
    def evil_source():
        return [(0.0, 0.0, 255, 255, 255)] * 100

    fake4, st4 = run_stream(evil_source, 0.5)
    check_true("stehender heller Punkt wird verworfen", st4.stats["rejected"] > 0,
               "(%d verworfen)" % st4.stats["rejected"])
    check("es kam trotzdem KEIN Licht raus", len(lit_points(fake4)), 0)
    check_true("es wurde ersatzweise weitergesendet", fake4.total_samples > 0)

    # --- 7. Werfende Frame-Quelle bricht den Strom nicht ab ----------------
    def broken_source():
        raise RuntimeError("kaputt")

    fake5, st5 = run_stream(broken_source, 0.5)
    check_true("werfende Quelle stoppt den Strom nicht", fake5.total_samples > 0)
    check("werfende Quelle erzeugt kein Licht", len(lit_points(fake5)), 0)
    check_true("Fehler wurde gemeldet", not st5.messages.empty())

    # --- 8. Leere Frame-Quelle -> geblankt --------------------------------
    fake6, _ = run_stream(lambda: [], 0.5)
    check_true("leere Quelle stoppt den Strom nicht", fake6.total_samples > 0)
    check("leere Quelle erzeugt kein Licht", len(lit_points(fake6)), 0)

    # --- 9. Der Strom hat keine Luecken zwischen den Bildern --------------
    # Bei zyklischem Abspielen muss die Gesamtzahl gesendeter Samples ein
    # Vielfaches der Chunk-Groesse sein - kein halbes Paket, kein Leerlauf.
    check("Chunks sind vollstaendig", fake2.total_samples % st2.chunk_samples, 0)

    print()
    if FAILED:
        print("%d Test(s) fehlgeschlagen: %s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("Alle Tests bestanden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
