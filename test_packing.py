#!/usr/bin/env python3
"""
Selbsttest fuer das Sample-Packing - laeuft OHNE Hardware.

Die Sollwerte sind aus dem C++-Original abgeleitet:
  ldCompressedSample.cpp   rg = red | (green << 8);  b = blue;
                           x  = MAX_COORD - GetUInt16(x);  y = GetUInt16(y);
  LaserdockSample.h        struct { uint16 rg, b, x, y; }  -> 8 Byte LE

Aufruf:  ./run.sh test_packing.py
(laeuft auch mit jedem anderen Python - es wird nichts geladen ausser struct)
"""

import struct
import sys

sys.path.insert(0, ".")

from lasercube import (MAX_COORD, frame_is_safe, make_circle, pack_frame,
                       pack_point)

FAILED = []


def check(name, got, want):
    if got == want:
        print("  ok    %s" % name)
    else:
        print("  FEHL  %s\n          erhalten %r\n          erwartet %r" % (name, got, want))
        FAILED.append(name)


def unpack(sample: bytes):
    return struct.unpack("<HHHH", sample)   # rg, b, x, y


def main():
    print("Sample-Packing gegen die C++-Sollwerte\n")

    # Mitte: GetUInt16(0.0) = int(0.5 * 4095) = 2047, X gespiegelt = 4095-2047
    centre_x = MAX_COORD - int(0.5 * MAX_COORD)
    centre_y = int(0.5 * MAX_COORD)

    check("Sample ist 8 Byte", len(pack_point(0, 0, 0, 0, 0)), 8)

    check("rot   -> rg=0x00ff, b=0",
          unpack(pack_point(0, 0, 255, 0, 0))[:2], (0x00FF, 0x0000))
    check("gruen -> rg=0xff00, b=0",
          unpack(pack_point(0, 0, 0, 255, 0))[:2], (0xFF00, 0x0000))
    check("blau  -> rg=0x0000, b=0x00ff",
          unpack(pack_point(0, 0, 0, 0, 255))[:2], (0x0000, 0x00FF))
    check("weiss -> rg=0xffff, b=0x00ff",
          unpack(pack_point(0, 0, 255, 255, 255))[:2], (0xFFFF, 0x00FF))

    check("Mitte (0,0)", unpack(pack_point(0, 0, 0, 0, 0))[2:], (centre_x, centre_y))
    check("X wird gespiegelt: x=+1 -> 0",
          unpack(pack_point(1.0, 0, 0, 0, 0))[2], 0)
    check("X wird gespiegelt: x=-1 -> 4095",
          unpack(pack_point(-1.0, 0, 0, 0, 0))[2], MAX_COORD)
    check("y=+1 -> 4095", unpack(pack_point(0, 1.0, 0, 0, 0))[3], MAX_COORD)
    check("y=-1 -> 0", unpack(pack_point(0, -1.0, 0, 0, 0))[3], 0)

    check("Ueberlauf wird geklemmt: x=+5 -> 0",
          unpack(pack_point(5.0, 0, 0, 0, 0))[2], 0)
    check("Farbe wird geklemmt: r=999 -> 255",
          unpack(pack_point(0, 0, 999, 0, 0))[0], 0x00FF)

    # Helligkeitsdeckel
    frame = pack_frame([(0, 0, 255, 255, 255)], max_rgb=40)
    check("max_rgb=40 deckelt alle Kanaele", unpack(frame)[:2], (40 | (40 << 8), 40))

    check("pack_frame Laenge", len(pack_frame(make_circle(num_points=64))), 64 * 8)

    # Punkt-Waechter
    print()
    check("Waechter: normaler Kreis ist sicher",
          frame_is_safe(make_circle(radius=0.8, num_points=32, r=255)), True)
    check("Waechter: ein heller Punkt ist NICHT sicher",
          frame_is_safe([(0.0, 0.0, 255, 0, 0)] * 50), False)
    check("Waechter: enger heller Fleck ist NICHT sicher",
          frame_is_safe(make_circle(radius=0.001, num_points=32, r=255)), False)
    check("Waechter: dunkler Punkt ist sicher (kein Licht)",
          frame_is_safe([(0.0, 0.0, 0, 0, 0)] * 50), True)
    check("Waechter: dunkler Kreis ist sicher",
          frame_is_safe(make_circle(radius=0.9, num_points=32)), True)
    check("Waechter: min_span=0 schaltet ab",
          frame_is_safe([(0.0, 0.0, 255, 0, 0)], min_span=0), True)

    print()
    if FAILED:
        print("%d Test(s) fehlgeschlagen: %s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("Alle Tests bestanden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
