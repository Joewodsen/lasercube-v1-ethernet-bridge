#!/bin/bash
#
# Baut den Wrapper nativ auf Linux (Raspberry Pi).
#
#   ./build_wrapper_linux.sh
#
# Ergebnis: build/ldwrapper.so
#
# Warum das hier so viel kuerzer ist als build_wrapper.sh: Auf macOS muss der
# Wrapper x86_64 sein (Rosetta) und gegen die mitgelieferte vendor/libusb
# linken. Auf Linux gibt es weder Rosetta noch Architekturkonflikt - wir bauen
# nativ gegen die System-libusb. ldwrapper.c bleibt dabei unveraendert; sie
# benutzt nur stdint/stdio/string und libusb.h.

set -euo pipefail
cd "$(dirname "$0")"

if ! command -v gcc >/dev/null; then
    echo "FEHLER: gcc fehlt.  sudo apt-get install -y build-essential" >&2
    exit 1
fi

if ! pkg-config --exists libusb-1.0; then
    echo "FEHLER: libusb-1.0 nicht gefunden." >&2
    echo "        sudo apt-get install -y libusb-1.0-0-dev pkg-config" >&2
    exit 1
fi

mkdir -p build

# ldwrapper.c schreibt #include "libusb.h", der Header liegt unter Debian aber
# in /usr/include/libusb-1.0/. pkg-config --cflags loest genau das auf, ohne
# dass die Quelldatei angefasst werden muss.
CFLAGS="$(pkg-config --cflags libusb-1.0)"
LIBS="$(pkg-config --libs libusb-1.0)"

echo "libusb: $(pkg-config --modversion libusb-1.0)"
echo "bauen  ..."

# shellcheck disable=SC2086
gcc -shared -fPIC -std=c11 -O2 -Wall -Wextra \
    $CFLAGS \
    ldwrapper.c \
    $LIBS \
    -o build/ldwrapper.so

echo "fertig: build/ldwrapper.so"
file build/ldwrapper.so | sed 's/^/  /'

# Gegenprobe: laesst sich die Bibliothek laden und sind die Symbole da?
python3 - <<'EOF'
import ctypes
lib = ctypes.CDLL("build/ldwrapper.so")
for sym in ("ld_open", "ld_close", "ld_send", "ld_get_u8", "ld_set_u8",
            "ld_get_u32", "ld_set_u32", "ld_serial", "ld_descriptor_dump"):
    getattr(lib, sym)
print("  Symbole vollstaendig, Bibliothek ladbar.")
EOF
