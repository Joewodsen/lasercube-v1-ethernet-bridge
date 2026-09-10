/*
 * ldwrapper.c - minimale C-Schicht zwischen Python (ctypes) und libusb
 * fuer den LaserCube der ersten Generation (LaserDock, Micro-USB).
 *
 * Protokoll rekonstruiert aus Wickedlasers/laserdocklib,
 * lib/src/LaserdockDevice.cpp (LGPL-3.0).
 *
 * ===========================================================================
 * WARUM ES DIESE DATEI GIBT
 * ===========================================================================
 * Ein frueherer Versuch, den Cube direkt per pyusb aus arm64-Python
 * anzusprechen, hat den Mac mit einer Kernel Panic (Pink Screen) hart
 * abgeschossen. Diese Datei macht deshalb GENAU die Sequenz, die
 * laserdocklib macht - und sonst nichts.
 *
 * DIE REGELN, DIE DEN KERNEL SCHUETZEN. NICHT AUFWEICHEN:
 *
 *  1. NIE libusb_set_configuration().
 *     Loest auf macOS eine Re-Enumeration des Geraets aus. laserdocklib ruft
 *     es nie auf, pyusb dagegen implizit beim ersten Zugriff. Hauptverdaechtiger
 *     fuer die damalige Panic.
 *
 *  2. NIE libusb_reset_device().
 *     Gleiche Kategorie, und nie noetig.
 *
 *  3. NIE isochron. Interface 1 hat sehr wahrscheinlich ein isochrones
 *     Alt-Setting (das Geraet kennt den Befehl 0x86 "iso packet sample count").
 *     libusb + Isochron + macOS ist bekanntes Panic-Terrain. Wir setzen gezielt
 *     Alt-Setting 1 und benutzen danach ausschliesslich Bulk auf 0x01/0x81/0x03.
 *
 *  4. NUR synchrone Transfers (libusb_bulk_transfer). Keine async-URBs, keine
 *     Transfer-Queues - dann kann beim Abbruch auch nichts im Kernel haengen.
 *
 *  5. IMMER endliche Timeouts. laserdocklib benutzt 0 (= unendlich); ein
 *     haengender Transfer waere dann nicht mehr abbrechbar.
 *
 *  6. Interfaces sauber freigeben und Kontext schliessen. laserdocklib hat
 *     seine release()-Funktion auskommentiert und laesst die Interfaces belegt.
 *
 * ===========================================================================
 */

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "libusb.h"

/* --- Geraet ------------------------------------------------------------- */

#define LD_VID 0x1fc9
#define LD_PID 0x04d8

/* Interface 0 = Steuerung, Interface 1 Alt-Setting 1 = Daten. */
#define LD_IFACE_CTL       0
#define LD_IFACE_DATA      1
#define LD_IFACE_DATA_ALT  1

/* Bulk-Endpoints. In laserdocklib als (1|OUT), (1|IN), (3|OUT) geschrieben. */
#define LD_EP_CTL_OUT      0x01
#define LD_EP_CTL_IN       0x81
#define LD_EP_DATA_OUT     0x03

#define LD_TIMEOUT_CTL_MS   1000
#define LD_TIMEOUT_DATA_MS  2000
#define LD_DATA_RETRIES     3

/* --- Fehlercodes (immer negativ, 0 = ok) -------------------------------- */

#define LD_OK                0
#define LD_ERR_INIT         -1   /* libusb_init fehlgeschlagen               */
#define LD_ERR_NO_DEVICE    -2   /* kein 1fc9:04d8 gefunden                  */
#define LD_ERR_OPEN         -3   /* libusb_open fehlgeschlagen               */
#define LD_ERR_CLAIM_CTL    -4   /* Interface 0 nicht belegbar               */
#define LD_ERR_CLAIM_DATA   -5   /* Interface 1 nicht belegbar               */
#define LD_ERR_ALTSETTING   -6   /* Alt-Setting 1 nicht setzbar              */
#define LD_ERR_NOT_OPEN    -10   /* ld_open() wurde nicht (erfolgreich) gerufen */
#define LD_ERR_ARG         -11   /* unsinnige Argumente                      */
#define LD_ERR_TRANSFER    -12   /* libusb-Transferfehler                    */
#define LD_ERR_SHORT       -13   /* zu wenige Bytes uebertragen              */
#define LD_ERR_STATUS      -14   /* Geraet meldet Fehler (resp[1] != 0)      */

/* --- Zustand ------------------------------------------------------------ */

static libusb_context       *g_ctx      = NULL;
static libusb_device_handle *g_h_ctl    = NULL;
static libusb_device_handle *g_h_data   = NULL;
static int                   g_claimed_ctl  = 0;
static int                   g_claimed_data = 0;
static int                   g_last_rv  = 0;   /* letzter libusb-Rueckgabewert */

/*
 * laserdocklib oeffnet das Geraet ZWEIMAL und legt je ein Interface auf ein
 * eigenes Handle. Das ist unueblich; wir nutzen per Default ein einziges
 * Handle fuer beide Interfaces. Sollte die Inbetriebnahme daran scheitern,
 * ist -DLD_TWO_HANDLES die erste Variante zum Ausprobieren - dann verhaelt
 * sich der Wrapper exakt wie das Original.
 */

/* --- intern ------------------------------------------------------------- */

static void ld_teardown(void)
{
    if (g_h_ctl && g_claimed_ctl) {
        libusb_release_interface(g_h_ctl, LD_IFACE_CTL);
        g_claimed_ctl = 0;
    }
    if (g_h_data && g_claimed_data) {
        libusb_release_interface(g_h_data, LD_IFACE_DATA);
        g_claimed_data = 0;
    }
    if (g_h_data && g_h_data != g_h_ctl) {
        libusb_close(g_h_data);
    }
    g_h_data = NULL;
    if (g_h_ctl) {
        libusb_close(g_h_ctl);
        g_h_ctl = NULL;
    }
    if (g_ctx) {
        libusb_exit(g_ctx);
        g_ctx = NULL;
    }
}

/*
 * Ein Befehls-Roundtrip ueber Interface 0: Anfrage raus, 64 Byte Antwort rein.
 * Die Antwort ist immer 64 Byte lang, resp[1] == 0 bedeutet Erfolg.
 * Bewusst BULK, nicht Control - das Geraet erwartet das so.
 */
static int ld_roundtrip(const uint8_t *req, int reqlen, uint8_t *resp64)
{
    uint8_t packet[64];
    int transferred = 0;
    int rv;

    if (!g_h_ctl) return LD_ERR_NOT_OPEN;
    if (reqlen < 1 || reqlen > (int)sizeof(packet)) return LD_ERR_ARG;

    memset(packet, 0, sizeof(packet));
    memcpy(packet, req, (size_t)reqlen);

    rv = libusb_bulk_transfer(g_h_ctl, LD_EP_CTL_OUT, packet, reqlen,
                              &transferred, LD_TIMEOUT_CTL_MS);
    g_last_rv = rv;
    if (rv != 0) return LD_ERR_TRANSFER;
    if (transferred != reqlen) return LD_ERR_SHORT;

    memset(packet, 0, sizeof(packet));
    transferred = 0;
    rv = libusb_bulk_transfer(g_h_ctl, LD_EP_CTL_IN, packet, (int)sizeof(packet),
                              &transferred, LD_TIMEOUT_CTL_MS);
    g_last_rv = rv;
    if (rv != 0) return LD_ERR_TRANSFER;
    if (transferred != (int)sizeof(packet)) return LD_ERR_SHORT;
    if (packet[1] != 0) return LD_ERR_STATUS;

    if (resp64) memcpy(resp64, packet, sizeof(packet));
    return LD_OK;
}

/* --- oeffentliche API --------------------------------------------------- */

int ld_last_rv(void)
{
    return g_last_rv;
}

int ld_is_open(void)
{
    return g_h_ctl != NULL;
}

/*
 * Geraet suchen, oeffnen, beide Interfaces belegen, Alt-Setting setzen.
 * Setzt KEINE Konfiguration und resettet nichts (siehe Regeln oben).
 */
int ld_open(void)
{
    int rv;

    if (g_h_ctl) return LD_OK;   /* idempotent */

    rv = libusb_init(&g_ctx);
    g_last_rv = rv;
    if (rv != 0) {
        g_ctx = NULL;
        return LD_ERR_INIT;
    }

    /* Sucht die Geraeteliste durch und ruft libusb_open - mehr nicht. */
    g_h_ctl = libusb_open_device_with_vid_pid(g_ctx, LD_VID, LD_PID);
    if (!g_h_ctl) {
        libusb_exit(g_ctx);
        g_ctx = NULL;
        return LD_ERR_NO_DEVICE;
    }

#ifdef LD_TWO_HANDLES
    {
        libusb_device *dev = libusb_get_device(g_h_ctl);
        rv = libusb_open(dev, &g_h_data);
        g_last_rv = rv;
        if (rv != 0) {
            g_h_data = NULL;
            ld_teardown();
            return LD_ERR_OPEN;
        }
    }
#else
    g_h_data = g_h_ctl;
#endif

    rv = libusb_claim_interface(g_h_ctl, LD_IFACE_CTL);
    g_last_rv = rv;
    if (rv != 0) {
        ld_teardown();
        return LD_ERR_CLAIM_CTL;
    }
    g_claimed_ctl = 1;

    rv = libusb_claim_interface(g_h_data, LD_IFACE_DATA);
    g_last_rv = rv;
    if (rv != 0) {
        ld_teardown();
        return LD_ERR_CLAIM_DATA;
    }
    g_claimed_data = 1;

    /*
     * Alt-Setting 1 von Interface 1 ist das BULK-Setting. Alt-Setting 0 ist
     * vermutlich das isochrone - das wird nie angefasst.
     */
    rv = libusb_set_interface_alt_setting(g_h_data, LD_IFACE_DATA,
                                          LD_IFACE_DATA_ALT);
    g_last_rv = rv;
    if (rv != 0) {
        ld_teardown();
        return LD_ERR_ALTSETTING;
    }

    return LD_OK;
}

/* Immer sicher aufrufbar, auch mehrfach und auch wenn nie geoeffnet wurde. */
void ld_close(void)
{
    ld_teardown();
}

/*
 * Beliebigen Befehl absetzen und die VOLLE 64-Byte-Antwort zurueckgeben.
 *
 * Gedacht fuer Befehle, deren Antwort nicht in das Schema u8/u32 passt -
 * konkret die Challenge an den Sicherheitschip (0xB0/0xB1), die 35 Byte
 * Nutzdaten zurueckliefert.
 *
 * Anders als ld_roundtrip() wird das Statusbyte NICHT geprueft, sondern
 * unveraendert durchgereicht: bei diesen Befehlen ist es selbst Teil der
 * Antwort, die der Aufrufer auswerten muss.
 *
 * Es gelten dieselben Regeln wie ueberall in dieser Datei: nur synchrone
 * Bulk-Transfers, endliche Timeouts, kein set_configuration, kein Reset.
 */
int ld_cmd_raw(const uint8_t *req, int reqlen, uint8_t *resp64)
{
    uint8_t packet[64];
    int transferred = 0;
    int rv;

    if (!g_h_ctl) return LD_ERR_NOT_OPEN;
    if (!req || !resp64) return LD_ERR_ARG;
    if (reqlen < 1 || reqlen > (int)sizeof(packet)) return LD_ERR_ARG;

    memset(packet, 0, sizeof(packet));
    memcpy(packet, req, (size_t)reqlen);

    rv = libusb_bulk_transfer(g_h_ctl, LD_EP_CTL_OUT, packet, reqlen,
                              &transferred, LD_TIMEOUT_CTL_MS);
    g_last_rv = rv;
    if (rv != 0) return LD_ERR_TRANSFER;
    if (transferred != reqlen) return LD_ERR_SHORT;

    memset(packet, 0, sizeof(packet));
    transferred = 0;
    rv = libusb_bulk_transfer(g_h_ctl, LD_EP_CTL_IN, packet, (int)sizeof(packet),
                              &transferred, LD_TIMEOUT_CTL_MS);
    g_last_rv = rv;
    if (rv != 0) return LD_ERR_TRANSFER;
    if (transferred != (int)sizeof(packet)) return LD_ERR_SHORT;

    memcpy(resp64, packet, sizeof(packet));
    return LD_OK;
}

int ld_get_u8(uint8_t cmd, uint8_t *out)
{
    uint8_t resp[64];
    uint8_t req[1];
    int rv;

    if (!out) return LD_ERR_ARG;
    req[0] = cmd;
    rv = ld_roundtrip(req, 1, resp);
    if (rv != LD_OK) return rv;
    *out = resp[2];
    return LD_OK;
}

int ld_set_u8(uint8_t cmd, uint8_t val)
{
    uint8_t req[2];
    req[0] = cmd;
    req[1] = val;
    return ld_roundtrip(req, 2, NULL);
}

int ld_get_u32(uint8_t cmd, uint32_t *out)
{
    uint8_t resp[64];
    uint8_t req[1];
    int rv;

    if (!out) return LD_ERR_ARG;
    req[0] = cmd;
    rv = ld_roundtrip(req, 1, resp);
    if (rv != LD_OK) return rv;

    /* little-endian, explizit zusammengesetzt statt memcpy */
    *out = (uint32_t)resp[2]
         | ((uint32_t)resp[3] << 8)
         | ((uint32_t)resp[4] << 16)
         | ((uint32_t)resp[5] << 24);
    return LD_OK;
}

int ld_set_u32(uint8_t cmd, uint32_t val)
{
    uint8_t req[5];
    req[0] = cmd;
    req[1] = (uint8_t)(val & 0xFF);
    req[2] = (uint8_t)((val >> 8) & 0xFF);
    req[3] = (uint8_t)((val >> 16) & 0xFF);
    req[4] = (uint8_t)((val >> 24) & 0xFF);
    return ld_roundtrip(req, 5, NULL);
}

/*
 * Samples an den Laser. Ein Sample sind 8 Byte (rg, b, x, y als uint16 LE).
 * Bei Timeout wird begrenzt wiederholt - wie im Original.
 */
int ld_send(const void *samples, uint32_t count)
{
    int length;
    int transferred = 0;
    int strikes = LD_DATA_RETRIES;
    int rv = 0;

    if (!g_h_data) return LD_ERR_NOT_OPEN;
    if (!samples || count == 0) return LD_ERR_ARG;

    length = (int)(count * 8u);

    do {
        transferred = 0;
        rv = libusb_bulk_transfer(g_h_data, LD_EP_DATA_OUT,
                                  (unsigned char *)samples, length,
                                  &transferred, LD_TIMEOUT_DATA_MS);
        if (rv == LIBUSB_ERROR_TIMEOUT) strikes--;
    } while (rv == LIBUSB_ERROR_TIMEOUT && strikes > 0);

    g_last_rv = rv;
    if (rv != 0) return LD_ERR_TRANSFER;
    if (transferred != length) return LD_ERR_SHORT;
    return LD_OK;
}

int ld_serial(char *buf, int buflen)
{
    struct libusb_device_descriptor desc;
    int rv;

    if (!buf || buflen < 2) return LD_ERR_ARG;
    buf[0] = '\0';
    if (!g_h_ctl) return LD_ERR_NOT_OPEN;

    rv = libusb_get_device_descriptor(libusb_get_device(g_h_ctl), &desc);
    g_last_rv = rv;
    if (rv != 0) return LD_ERR_TRANSFER;
    if (desc.iSerialNumber == 0) return LD_OK;   /* keine Seriennummer, kein Fehler */

    rv = libusb_get_string_descriptor_ascii(g_h_ctl, desc.iSerialNumber,
                                            (unsigned char *)buf, buflen);
    g_last_rv = rv;
    if (rv < 0) {
        buf[0] = '\0';
        return LD_ERR_TRANSFER;
    }
    return LD_OK;
}

/* --- Deskriptor-Dump ---------------------------------------------------- */

static const char *ld_xfer_name(uint8_t attributes)
{
    switch (attributes & 0x03) {
        case LIBUSB_TRANSFER_TYPE_CONTROL:     return "control";
        case LIBUSB_TRANSFER_TYPE_ISOCHRONOUS: return "ISOCHRON";
        case LIBUSB_TRANSFER_TYPE_BULK:        return "bulk";
        case LIBUSB_TRANSFER_TYPE_INTERRUPT:   return "interrupt";
    }
    return "?";
}

/*
 * Liest die Deskriptorstruktur OHNE libusb_open - das Geraet wird dabei nicht
 * angefasst. Damit laesst sich vorab pruefen, wo die isochronen Endpoints
 * sitzen, bevor irgendetwas geoeffnet wird.
 *
 * Rueckgabe: Anzahl gefundener LaserCubes, oder ein negativer Fehlercode.
 */
int ld_descriptor_dump(char *buf, int buflen)
{
    libusb_context *ctx = NULL;
    libusb_device **list = NULL;
    ssize_t count, i;
    int found = 0;
    int rv;
    char *p;
    int left;

    if (!buf || buflen < 64) return LD_ERR_ARG;
    buf[0] = '\0';
    p = buf;
    left = buflen;

#define LD_EMIT(...)                                        \
    do {                                                    \
        int _n = snprintf(p, (size_t)left, __VA_ARGS__);    \
        if (_n > 0) {                                       \
            if (_n >= left) { p += left - 1; left = 1; }    \
            else { p += _n; left -= _n; }                   \
        }                                                   \
    } while (0)

    rv = libusb_init(&ctx);
    g_last_rv = rv;
    if (rv != 0) return LD_ERR_INIT;

    count = libusb_get_device_list(ctx, &list);
    if (count < 0) {
        libusb_exit(ctx);
        return LD_ERR_NO_DEVICE;
    }

    for (i = 0; i < count; i++) {
        struct libusb_device_descriptor desc;
        int c;

        if (libusb_get_device_descriptor(list[i], &desc) != 0) continue;
        if (desc.idVendor != LD_VID || desc.idProduct != LD_PID) continue;
        found++;

        LD_EMIT("LaserCube %04x:%04x  Bus %d  Adresse %d  USB %x.%02x\n",
                desc.idVendor, desc.idProduct,
                libusb_get_bus_number(list[i]),
                libusb_get_device_address(list[i]),
                (desc.bcdUSB >> 8) & 0xff, desc.bcdUSB & 0xff);
        LD_EMIT("  Konfigurationen: %d\n", desc.bNumConfigurations);

        for (c = 0; c < desc.bNumConfigurations; c++) {
            struct libusb_config_descriptor *cfg = NULL;
            int ifc;

            if (libusb_get_config_descriptor(list[i], (uint8_t)c, &cfg) != 0) {
                LD_EMIT("  Konfiguration %d: nicht lesbar\n", c);
                continue;
            }

            LD_EMIT("  Konfiguration %d (bConfigurationValue=%d, %d Interfaces)\n",
                    c, cfg->bConfigurationValue, cfg->bNumInterfaces);

            for (ifc = 0; ifc < cfg->bNumInterfaces; ifc++) {
                const struct libusb_interface *itf = &cfg->interface[ifc];
                int alt;

                LD_EMIT("    Interface %d (%d Alt-Settings)\n",
                        ifc, itf->num_altsetting);

                for (alt = 0; alt < itf->num_altsetting; alt++) {
                    const struct libusb_interface_descriptor *d =
                        &itf->altsetting[alt];
                    int e;

                    LD_EMIT("      Alt %d: class %02x subclass %02x proto %02x, %d Endpoints\n",
                            d->bAlternateSetting, d->bInterfaceClass,
                            d->bInterfaceSubClass, d->bInterfaceProtocol,
                            d->bNumEndpoints);

                    for (e = 0; e < d->bNumEndpoints; e++) {
                        const struct libusb_endpoint_descriptor *ep =
                            &d->endpoint[e];
                        LD_EMIT("        EP 0x%02x  %-9s  %s  maxpkt %d\n",
                                ep->bEndpointAddress,
                                ld_xfer_name(ep->bmAttributes),
                                (ep->bEndpointAddress & 0x80) ? "IN " : "OUT",
                                ep->wMaxPacketSize);
                    }
                }
            }
            libusb_free_config_descriptor(cfg);
        }
    }

    if (found == 0) LD_EMIT("Kein LaserCube (%04x:%04x) gefunden.\n", LD_VID, LD_PID);

#undef LD_EMIT

    libusb_free_device_list(list, 1);
    libusb_exit(ctx);
    return found;
}
