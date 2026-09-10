# The LaserCube network protocol, byte for byte

Everything here is derived from
[`libLaserdockCore`](https://github.com/Wickedlasers/libLaserdockCore) —
`3rdparty/laserdocklib/src/LaserDockNetworkDevice.cpp` and
`ldCore/src/Hardware/ldNetworkHardwareManager.cpp` — plus measurements against
a real LaserCube V1 over USB. Where the two disagree, the C++ parser that
actually ships is authoritative and is what this document follows.

Three UDP ports, all bound on `0.0.0.0`:

| port | purpose |
|---|---|
| **45456** | discovery. The software broadcasts here; the device answers |
| **45457** | commands |
| **45458** | sample data |

Replies always go back to the port the request arrived on, so an ordinary
`sendto()` on the bound socket is correct.

---

## Discovery (45456)

The software walks every active, non-point-to-point IPv4 interface and sends a
single byte `0x27` to that interface's **directed** broadcast address — not to
`255.255.255.255`. The difference matters in practice: an active VPN swallows
the global broadcast address while the directed one still reaches the LAN.

The reply must be **exactly two bytes**:

```
0x27 0x00
```

Anything else is discarded without comment (`DeviceAliveResponseValid()` checks
length == 2, byte 0 == `0x27`, byte 1 == 0). The software then creates a device
object for the sender's address and continues on port 45457.

---

## The info packet (`0x77`) — the critical one

Polled every 250 ms while idle and every 2.5 s while active. If no reply
arrives for **4 seconds**, the device is considered disconnected. The answer
must be **exactly 64 bytes** or it is ignored silently.

| offset | size | content |
|---|---|---|
| 0 | 1 | `0x77` opcode echo |
| 1 | 1 | `0x00` = result OK. Anything non-zero is read as "command failed" |
| 2 | 1 | `0x00` = info packet protocol version. Must be 0 |
| 3 | 1 | firmware major |
| 4 | 1 | firmware minor |
| 5 | 1 | flags — see below |
| 6–9 | 4 | unused |
| 10–13 | 4 | DAC rate, uint32 little-endian |
| 14–17 | 4 | maximum DAC rate, uint32 LE |
| 18 | 1 | unused |
| 19–20 | 2 | free samples in the receive buffer, uint16 LE |
| 21–22 | 2 | total receive buffer size, uint16 LE |
| 23 | 1 | battery percent |
| 24 | 1 | temperature in °C, int8 |
| 25 | 1 | connection type **minus one** (so `2` means Ethernet client) |
| 26–31 | 6 | serial number, six raw bytes |
| 32–35 | 4 | own IP address, four bytes |
| 36 | 1 | unused |
| 37 | 1 | model number |
| 38–63 | 26 | model name, ASCII, NUL-terminated |

Flags at offset 5, for firmware ≥ 0.13:

| bit | meaning |
|---|---|
| 0 | output enabled |
| 1 | interlock |
| 2 | temperature warning |
| 3 | over temperature |
| 4–7 | packet error count |

Older firmware uses a different bit layout (interlock at bit 3, warning at
bit 4, over-temperature at bit 5). Reporting a version ≥ 0.13 avoids having to
implement it.

> **Watch out:** the widely referenced proof-of-concept gist unpacks this
> packet with a struct that is off by one byte at offsets 2–4 — it reads the
> protocol version as the firmware major. Everything from offset 10 onwards
> agrees with the C++ parser.

---

## Commands (45457)

Every reply is `opcode, 0x00 [, payload…]`.

| opcode | meaning | reply |
|---|---|---|
| `0x77` | get full info | the 64-byte packet above |
| `0x78` | buffer-size replies on data packets on/off (byte 1) | `78 00` |
| `0x80` | set output (byte 1 = 0/1) | `80 00` |
| `0x81` | get output | `81 00 <state>` |
| `0x82` | set ILDA rate (4 bytes uint32 LE) | `82 00` |
| `0x83` / `0x84` | get rate / max rate | `xx 00 <uint32 LE>` |
| `0x8A` | free samples in the ring buffer | `8a 00 <uint16 LE>` |
| `0x8D` | clear ring buffer | `8d 00` |
| `0xA0` | set DAC buffer threshold | `a0 00` |
| `0x97` | set non-volatile model info (factory) | `97 00` |
| `0xB0` / `0xB1` | security challenge / response | see below |

On connect the software always runs this sequence: output **off** → buffer
replies **on** → set ILDA rate. Samples only start after that.

---

## Sample data (45458)

```
0xA9 | 0x00 | msg_num u8 | frame_num u8 | point | point | ...
```

Each point is **10 bytes**: five uint16 little-endian values in the order
`x, y, r, g, b`. At most 140 points per packet (1404 bytes, inside a 1500-byte
MTU). `msg_num` increments per packet, `frame_num` per frame; both wrap at 255.

With `0x78` enabled, the device answers **each** data packet on the same port
with `8a 00 <free uint16 LE>`. That is the flow control: the software never
sends more than the device reported as free.

### Converting to the USB sample format

The original software builds the *same* compressed USB sample it would send
over the wire and merely widens it for the network:

```c
uint16_t x = s.x;                        // taken over unchanged
uint16_t y = s.y;                        // taken over unchanged
uint16_t r = ((s.rg      & 0xff) << 4);  // 8 bit -> 12 bit
uint16_t g = (((s.rg>>8) & 0xff) << 4);
uint16_t b = (s.b << 4);
```

So the reverse is lossless and trivial:

```python
usb_x = x & 0x0FFF          # NO X mirroring here
usb_y = y & 0x0FFF
r8, g8, b8 = r >> 4, g >> 4, b >> 4
sample = struct.pack("<HHHH", r8 | (g8 << 8), b8, usb_x, usb_y)
```

**The X axis must not be mirrored on this path.** Drivers that talk to the cube
directly over USB have to mirror X (`4095 - x`, as `ldCompressedSample.cpp`
does), but the software has already applied it before putting samples on the
network. Mirror again and the image comes out backwards.

---

## The security challenge (`0xB0` / `0xB1`)

`libLaserdockCore` ships this check **disabled** — with no callback installed
every device is accepted, and the file contains a hard-coded example challenge
plus the response a USB cube gives to it. The shipping LaserOS has the check
enabled and sends a **random** challenge instead:

```
01                                  wake the chip
e0 2e 00 00 40 9c 00 00 23 27 08 00 00 00
                                    fixed header, ATSHA204 opcode 0x08 = MAC
<32 bytes>                          the random challenge, new every start
<2 bytes>                           CRC16
```

Flow: the software sends `0xB0` + challenge, expects an ack `b0 00`, then sends
`0xB1` to collect the answer, which must come back as:

```
0xB1 | 0x00 | 0x00 | <35 bytes>
```

Both zero bytes are checked; the remaining 35 bytes are the payload.

This cannot be computed without the key inside the device's ATSHA204 — which
is the entire point of that chip. It also does not need to be: the USB cube has
the same chip, so the request is passed through over USB and the genuine answer
is returned.

### What USB measurement adds

The cube supports `0xB0`/`0xB1` over its bulk command endpoint (an unknown
opcode such as `0xBF` returns status `0xFF`, so status `0x00` means genuinely
supported). Two things are not documented anywhere:

**The chip needs time, and the cube queues replies.**

| delay after `0xB0` | what `0xB1` returns |
|---|---|
| 0 ms | the answer from the *previous* round |
| 20 ms | a short status packet (length 4) |
| **~50 ms** | **the correct MAC** (length byte `0x23` = 35) |
| 100 ms+ | zeroes — already collected |

So: drain the queue, send the challenge, then poll until a reply whose length
byte is `0x23` appears.

**The payload starts at offset 3** in the 64-byte USB reply:

```
[0] 0xB1 echo   [1] status   [2] 0x00   [3] 0x23 length   [4..] MAC + CRC
```

The 35 bytes the software wants are `resp[3:38]` — *including* the length byte.
That matches the values written in the `libLaserdockCore` comments exactly,
which is also the proof that the pass-through is genuine rather than a replay.

---

## Buffer sizing

A real network cube reports about **6000 samples**. A LaserCube V1's ring
buffer holds **768** — roughly 17 ms at 45 000 pps, far too little to absorb
network jitter, and an underrun on a galvo laser means a stationary beam.

The bridge therefore reports a virtual 6000-sample buffer backed by its own
RAM and drains it into the real 768-sample ring locally. The reported free
count must be honest in both directions: too high and the queue overflows, too
low and the software throttles until the stream breaks.

Measured: with 3000 advertised the queue overflowed regularly (~1.2 % of
samples lost); with 6000 it is clean. It costs no latency, because the software
only fills a few thousand samples deep regardless — measured end-to-end latency
stays around 18 ms.
