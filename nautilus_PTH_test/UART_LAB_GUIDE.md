# Nautilus UART Lab — Setup, Flashing & Command Guide

A runtime-configurable UART test bench on the STM32H723 (Nucleo-H723ZG),
driven from a text console, with an Arduino MEGA 2560 emulating a sensor.

- Console: **USART3** (ST-Link Virtual COM Port) @ 115200 8N1
- Lab ports: **uart4** (PD1 TX / PD0 RX), **usart2** (PD5 TX / PD6 RX, RS485 DE=PE4), **usart6** (PC6 TX / PC7 RX)

---

## 1. Hardware setup

| Elegoo MEGA 2560 | STM32H723 | Signal |
|---|---|---|
| pin 18 (TX1) | PD0 | Arduino → STM32 (UART4_RX) |
| pin 19 (RX1) | PD1 | STM32 → Arduino (UART4_TX) |
| GND | GND | common ground — mandatory |

Both boards powered over their own USB cables (that gives you both debug
consoles at once). Keep jumper wires short: the STM32's 3.3 V TX is just
above the 5 V MEGA's ~3.0 V input threshold.

## 2. Flashing

**STM32 (STM32CubeIDE):** open the `nautilus_PTH_test` project → build
(hammer icon) → Run → Debug As → STM32 C/C++ Application (or Run) → flash
completes over the on-board ST-Link. Press the black reset button after
flashing to get a clean banner.

**Arduino (Arduino IDE):** open
`nautilus_PTH_test/tools/dummy_sensor/dummy_sensor.ino` →
Tools → Board → *Arduino Mega or Mega 2560*, Processor *ATmega2560* →
select the MEGA's COM port (different from the ST-Link one) → Upload.
Open its Serial Monitor at **115200** to watch the `[TX]`/`[RX]` mirror.

## 3. Opening the STM32 console

1. Device Manager → Ports → note the *STMicroelectronics STLink Virtual COM
   Port* number (e.g. COM5).
2. Open it in VS Code Serial Monitor / PuTTY / Tera Term at **115200 8N1**,
   local echo **off**, send on CR or LF.
3. Press board reset → banner + port list + `> ` prompt appears.

While you type, monitor output is held back until you press Enter
(10 s timeout), so incoming data can't tear your command apart.

---

## 4. STM32 console commands

### Information

| Command | What it does |
|---|---|
| `?` | Show the help screen. |
| `ports` | List every port: state, framing, prescaler, kernel clock + real frequency, **actual achieved baud**, and counters (`rx tx ORE FE NE PE zeros dropped`). Run before/after a test to see what moved. |
| `profiles` | List built-in device profiles and the settings each applies. |

### Configuring a port

| Command | What it does |
|---|---|
| `cfg <port> k=v ...` | Change any mix of settings and re-open the port in one line. |
| `bind <port> <profile>` | Apply a named profile (canned `cfg` + RS485 flag). |
| `open <port>` | Re-apply the stored config (re-open after `close`). |
| `close <port>` | Fully de-initialise the port (silences it completely). |

`cfg` keys (combine freely in one command):

| Key | Values | Notes |
|---|---|---|
| `baud=` | 300…12500000 | reply shows *actual* achievable baud |
| `data=` | 7, 8, 9 | payload bits, excluding parity |
| `parity=` | n, e, o | none / even / odd |
| `stop=` | 0.5, 1, 1.5, 2 | |
| `presc=` | 1,2,4,6,8,10,12,16,32,64,128,256 | per-port UART clock divider |
| `clk=` | pclk, hsi, csi, lse | kernel clock source (see caveat below) |
| `fifo=` | 0, 1 | 16-byte HW FIFO (leave at 1) |
| `swap=` | 0, 1 | swap TX/RX pins (fixes crossed wiring in software) |

Examples:
```
cfg uart4 baud=9600                      match the Arduino sketch
cfg uart4 baud=19200 parity=e stop=2     8E2 framing test
cfg uart4 presc=16 clk=hsi               low-baud clocking experiment
bind usart2 waveshare8ch                 back to the real Modbus device
```

**Kernel-clock caveat:** USART2/3 + UART4 share one clock mux, USART6 has
its own. `clk=` on uart4/usart2 retunes the whole group including the
console — the firmware re-inits the console automatically so it stays at
115200. `clk=lse` (32.768 kHz) is allowed on usart6 only.

### Sending data

| Command | What it does |
|---|---|
| `send <port> <text>` | Send text + CRLF. Everything after the port name goes verbatim, spaces included: `send uart4 RATE 5000`. |
| `sendn <port> <text>` | Same, but **no** line ending (for devices that don't want one). |
| `sendhex <port> <hh> <hh> ...` | Raw bytes in hex — for binary protocols: `sendhex usart2 01 03 00 00 00 01 84 0A`. |

If the port has `rs485=1`, TX is automatically wrapped in DE-pin
assert → transmit → wait-complete → release.

### Watching data

| Command | What it does |
|---|---|
| `mon <port> 1` | Live mode: every received burst prints as hex + ASCII. |
| `mon <port> 0` | Quiet mode: port keeps receiving/counting, prints nothing. |
| `peek <port>` | Show the **latest** burst + totals — the companion to `mon 0`. |
| `clear <port>` | Zero all counters for a fresh measurement. |
| `rs485 <port> 0\|1` | Toggle DE direction control (usart2 has the pin). |

Notes:
- A "burst" ends after a 10 ms idle gap or 64 bytes — back-to-back messages
  can merge into one line; that's timing-based framing, not data corruption.
- All-`0x00` floods (line held low / gross baud mismatch) are never printed
  individually — you get one summary line per 5 s and a `zeros=` counter.

---

## 5. Arduino dummy-sensor commands (via `send uart4 ...`)

| Command | Reply |
|---|---|
| `ID?` | `NAUTILUS-DUMMY-SENSOR v1 (MEGA2560)` |
| `READ` | one `$NAUT,T=..,H=..,SEQ=..*` frame |
| `START` / `STOP` | stream on / off (`OK STREAM ON/OFF`) |
| `RATE <ms>` | stream period, min 100 (`OK RATE <ms>`) |
| `PING` | `PONG` (link sanity check) |
| `ECHO <text>` | `<text>` (loopback integrity check) |
| anything else | `ERR unknown cmd - ...` |

To exercise framing knobs, edit `SENSOR_BAUD` / `SENSOR_CONFIG`
(e.g. `SERIAL_8E1`) in the sketch, re-upload, and match with `cfg uart4`.

---

## 6. Recipes

**Bring-up (each new session):**
```
ports                        sanity: all OPEN, counters 0
cfg uart4 baud=9600          match the sketch
                             → $NAUT frames should appear within 1 s
send uart4 PING              → PONG
```
(The very first command after a baud change may get `ERR unknown cmd` —
stale garbage from the old baud sat in the Arduino's line buffer; the ERR
flushes it.)

**Polled mode (how real sensors are used):**
```
send uart4 STOP
send uart4 READ              one frame per request
```

**Soak test (leave running, check later):**
```
mon uart4 0
clear uart4
...hours later...
peek uart4                   latest frame + burst/byte totals
ports                        ORE/FE/NE/PE still 0 = clean link
```

**Known failure signatures (worth reproducing once on purpose):**

| Symptom on console | Meaning |
|---|---|
| `N x 0x00 suppressed`, `zeros=` climbing | baud mismatch, or RX wire disconnected/held low |
| total silence, `rx=0` | no connection (RX pull-up keeps a floating pin quiet), or device not transmitting |
| readable-ish garbage + `FE=` climbing | close-but-wrong baud, or wrong stop bits |
| `PE=` climbing | parity mismatch — fix with `cfg <port> parity=` |
| `ORE=` climbing | bytes arriving faster than the poll loop drains (long console prints at high baud) |

---

## 7. Current limitations (fine for bring-up)

Polled I/O with blocking console prints: a continuous high-baud stream can
tick `ORE` while a long line prints. The upgrade path when all ports run
simultaneously is DMA + idle-line RX per port; only `port_rx_service`
and `port_tx` in `uart_lab.c` need to change.
