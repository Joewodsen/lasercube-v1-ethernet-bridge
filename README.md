# Ethernet for the LaserCube V1

**Drive a first-generation, USB-only LaserCube from the original LaserOS
software over the network — by making a Raspberry Pi pretend to be a
network LaserCube.**

The LaserCube V1 (LaserDock, USB ID `1fc9:04d8`) has exactly one micro-USB
socket. No Wi-Fi, no LAN. The machine generating the images has to sit next to
the laser. Later LaserCube generations speak UDP and can be placed anywhere on
the network — this project gives the old one the same freedom, **without
changing anything on the computer running LaserOS.**

A Raspberry Pi hangs off the cube's USB port and impersonates a network cube:
it answers the discovery broadcast, serves the device-info packet, relays the
authentication challenge, and turns the incoming sample stream into USB bulk
transfers.

```
  Mac running stock LaserOS            Raspberry Pi                  LaserCube V1
  ─────────────────────────            ────────────                  ────────────
  broadcasts for cubes                 lasercube_netdev.py           galvos + diodes
       │  UDP 45456  discovery  ───►   answers as a cube                   ▲
       │  UDP 45457  commands   ◄──►   serves info, relays auth            │
       │  UDP 45458  samples    ───►   buffers and paces                   │
                                             │                             │
                                       lasercube.py  (ctypes)              │
                                             │                             │
                                       ldwrapper.so  (libusb)  ── USB ─────┘
```

**Status:** working. LaserOS 0.18.1 on macOS discovers the Pi, authenticates
it, and reports `network h/w device initialised OK.` Measured over 30 s at
45 000 points/s: 1 216 500 points, zero dropped packets, zero buffer
overruns, zero underruns, ~18 ms latency.

---

## How it works

The protocol of the networked LaserCubes is not a secret — Wicked Lasers
published the **client** side themselves, in
[`libLaserdockCore`](https://github.com/Wickedlasers/libLaserdockCore)
(`3rdparty/laserdocklib/src/LaserDockNetworkDevice.cpp`). That file tells you
every message the software sends and every reply it expects.

So this project does not translate or proxy anything at the application level.
It **is** the device, as far as LaserOS is concerned. Full wire details in
[`docs/protocol.md`](docs/protocol.md).

Three things turned out to be the interesting part.

### 1. Whole sample streams, not whole frames

The cube's ring buffer holds **768 samples** — about 17 ms at 45 000 pps. Any
network hiccup longer than that would starve it, and a starved galvo laser
means a *stationary burning beam*. That is the dangerous failure mode, not
darkness.

The stock software sends a raw sample stream and paces itself by how much free
space the device reports. So the Pi advertises a **virtual 6000-sample buffer**
backed by its own RAM:

```
network ──► queue on the Pi (6000 samples, reported as the device buffer)
                 │
                 ▼
            USB streamer ──► the real 768-sample ring ──► galvos
```

The big buffer now sits *behind* the network hop and absorbs the jitter, while
the USB streamer feeds the real ring from local memory. The reported free
space has to be honest: report too much and the queue overflows, too little
and the software throttles until the stream breaks.

6000 is the size of a genuine network cube, and it costs no latency — the
software only ever fills a few thousand samples deep.

### 2. The authentication is real

`libLaserdockCore` ships with the security check disabled: with no callback
installed, every device is accepted. **The shipping LaserOS has the check.** It
sends a *random* challenge to the cube's ATSHA204 crypto chip and verifies the
MAC that comes back.

That cannot be forged — and it does not have to be. The old USB cube has the
same chip. The Pi computes nothing: it **passes the challenge through to the
real hardware over USB and returns the genuine answer.** The key never leaves
the chip, which is exactly the point of the chip.

Two details had to be measured on the device and appear in no source:

| delay after `0xB0` | what comes back |
|---|---|
| 0 ms | the answer from the *previous* round |
| 20 ms | a short status packet |
| **~50 ms** | **the correct MAC** (length byte `0x23` = 35) |
| 100 ms+ | zeroes — already collected |

The cube keeps a queue of replies, so the driver drains it, sends the
challenge, then polls until a MAC-length reply appears. The payload also
starts one byte later than expected: `resp[3:38]`, *including* the length byte.

Sanity check that this is genuinely the chip and not a replay: given the
challenge hard-coded in `libLaserdockCore`, the cube answers byte-for-byte
with the response documented in that file's comments — while an unknown opcode
returns status `0xFF`.

### 3. "Disarmed" must never mean "output off"

An early version switched the cube's physical output off when the software
disarmed it. The cube then stops draining its ring buffer, the buffer fills
within a fraction of a second, the next bulk transfer blocks until timeout
(`libusb -7`), and the streamer thread exits. The laser is dead until the
service restarts.

The correct mode — and the safer one — is: **the output stays on as long as
the streamer runs.** Disarmed means a *blanked circle*: galvos moving, diode
dark.

---

## Safety

A LaserCube can damage eyes and ignite material. These interlocks are layered,
and each one is independently sufficient:

| mechanism | effect |
|---|---|
| the streamer never stops sending | no source, a broken frame, or an exception all produce a **blanked circle** — mirrors moving, beam dark |
| starts disarmed | light only after the software explicitly sends `0x80 01` |
| dead-man switch | no sample packets for 0.4 s → drawn black, regardless of arm state |
| brightness cap | `--max-rgb` clamps every channel globally; `0` guarantees darkness |
| standing-beam guard | if every lit sample stays within `--min-span` across a 200 ms window, the colours are stripped |
| dark is the failure state | send errors, `Ctrl-C`, `SIGTERM`, exceptions and normal exit all route through `disable_output()` via `atexit`, signal handlers and `finally` |
| watchdog | a streamer thread that dies is reported and restarted |
| `KillSignal=SIGTERM` | systemd must not `SIGKILL` past the shutdown path |

Emergency stop:

```bash
sudo systemctl stop lasercube-netdev
python3 lasercube_test.py --off
```

**Bring it up with the safety lens fitted and `--max-rgb 0` first.** Everything
in this repository was measured with the output blanked.

---

## Setup

On the Raspberry Pi (tested on a 3B+ with Raspberry Pi OS Lite, Debian 13,
aarch64 — but nothing here is Pi-specific):

```bash
sudo apt-get install -y build-essential libusb-1.0-0-dev pkg-config
git clone https://github.com/Joewodsen/lasercube-v1-ethernet-bridge.git ~/lasercube
cd ~/lasercube

# USB access without root
sudo cp 99-lasercube.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
# now unplug and replug the cube once

./build_wrapper_linux.sh          # builds build/ldwrapper.so natively

# check the cube is there and talking, without emitting light
python3 lasercube_test.py --info
```

Run it:

```bash
python3 lasercube_netdev.py --max-rgb 0     # protocol + USB, guaranteed dark
python3 lasercube_netdev.py                 # normal operation
```

As a service — edit `User`, `Group` and `WorkingDirectory` in
`lasercube-netdev.service` if your account is not `pi`:

```bash
sudo cp lasercube-netdev.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lasercube-netdev
journalctl -u lasercube-netdev -f
```

### On the machine running LaserOS

Nothing to install. But on **macOS 15 and newer you must grant LaserOS the
"Local Network" permission** (System Settings → Privacy & Security → Local
Network), then restart the app.

This is not optional and it fails silently: without it, LaserOS can still
broadcast and still *finds* the cube, but every unicast packet it sends to the
LAN is dropped by the OS. The symptom is a device that appears and then
reports `*** COMMS LOST ***` exactly four seconds later, with nothing
whatsoever arriving at the Pi. LaserOS ships without an
`NSLocalNetworkUsageDescription`, so it never visibly asks for the permission.
**Check this switch first whenever the cube stops being found** — macOS
updates like to reset it.

---

## Verifying it

`test_netdev.py` impersonates the original software from another machine —
discovery broadcast, the 64-byte info packet, the initialisation sequence, the
security challenge, and a paced sample stream with the same flow control
LaserOS uses. It sends black points and leaves the output disarmed unless you
ask otherwise.

```bash
python3 test_netdev.py --seconds 20            # finds the bridge by broadcast
python3 test_netdev.py --host 192.168.0.50 --seconds 30 --rate 45000
```

Hardware-free unit tests:

```bash
python3 test_packing.py      # sample format against the C++ reference values
python3 test_streamer.py     # streamer against a simulated cube
```

---

## Options

| flag | default | meaning |
|---|---|---|
| `--max-rgb` | 255 | global brightness cap, `0` guarantees darkness |
| `--buffer` | 6000 | virtual buffer size in samples |
| `--dark-after` | 0.4 | seconds without sample packets before blanking |
| `--min-span` | 0.02 | standing-beam guard, `0` disables it |
| `--dac-rate` | 45000 | point rate |
| `--max-dac-rate` | 45000 | ceiling reported to the software — see below |
| `--model-name` | LaserCube | what the device calls itself |
| `--allow` | – | only accept packets from this IP |
| `--no-usb` | off | protocol only, never touches the laser |
| `--verbose` | off | log every command |

**Do not raise `--max-dac-rate` to 64000.** The cube *reports* 64 000 pps but
can only be fed at roughly 55 000 over USB, and is permanently starved above
that. 45 000 is the measured sweet spot; 50 000 works with less headroom.

---

## Repository layout

| | |
|---|---|
| `lasercube_netdev.py` | the network device emulator |
| `lasercube.py` | USB driver: device access, sample format, streamer, safety logic |
| `ldwrapper.c` | minimal libusb wrapper — read the rules at the top before touching it |
| `build_wrapper_linux.sh` | builds `build/ldwrapper.so` against the system libusb |
| `lasercube_test.py` | commissioning tool: `--lsusb`, `--probe`, `--info`, `--dark`, `--stats`, `--off` |
| `test_netdev.py` | protocol test that impersonates LaserOS |
| `test_packing.py`, `test_streamer.py` | unit tests, no hardware needed |
| `99-lasercube.rules` | udev rule for USB access without root |
| `lasercube-netdev.service` | systemd unit |
| `docs/protocol.md` | the wire protocol, byte for byte |
| `docs/de/` | the original German design notes and findings |

### A note on `ldwrapper.c`

It exists because an earlier attempt to talk to the cube from `pyusb` caused a
kernel panic on macOS. The wrapper does exactly what the vendor library does
and nothing else. The rules in its header are not style preferences:

- never `libusb_set_configuration()` — triggers re-enumeration on macOS, and
  `pyusb` calls it implicitly on first access
- never `libusb_reset_device()`
- never touch isochronous transfers — interface 1's *default* alt setting is
  isochronous (`EP 0x04`); this code deliberately selects alt setting 1 (bulk).
  A script that naively grabs "the first OUT endpoint on interface 1" lands on
  the isochronous one, which is why simple approaches fail
- synchronous transfers only, always with finite timeouts
- release interfaces, close the context

The file is plain `stdint`/`stdio`/`string` + `libusb.h`, so it compiles
unchanged on Linux, natively, against the system libusb.

---

## Credits and licence

The protocol was derived from
[Wickedlasers/libLaserdockCore](https://github.com/Wickedlasers/libLaserdockCore)
(**LGPL-3.0**), which Wicked Lasers published as open source — this project
would not exist without it. Sidney San Martín's
[proof-of-concept client](https://gist.github.com/s4y/0675595c2ff5734e927d68caf652e3af)
was a useful second reading of the same protocol.

This code is released under **LGPL-3.0** to match. libusb is LGPL-2.1 and is
linked dynamically against the system library.

Not affiliated with, endorsed by, or supported by Wicked Lasers. This is
interoperability work on hardware and software that was legitimately acquired:
nothing here circumvents the cube's authentication — the genuine chip in the
genuine device answers the challenge itself.

**Use at your own risk.** Lasers are dangerous. Read the safety section.
