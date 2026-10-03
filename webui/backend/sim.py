"""Simulated Nautilus UART LAB bench: the STM32 console firmware plus the MEGA dummy sensor.

Byte-level stand-in for the ST-Link VCP, so the real console client is exercised:
- SimNautilus mirrors Core/Src/uart_lab.c: echo, prompt, every command and reply string,
  the 'ports' table, 10 ms / 64 B burst framing, the 5 s 0x00 summary, peek, counters, and
  the quirks (double-space send payload, failed cfg still updates the stored config,
  'data + parity does not fit' leaves the port open on its old settings).
- SimMega mirrors tools/dummy_sensor/dummy_sensor.ino on uart4: 9600 8N1, streams $NAUT
  frames at 1 Hz from power-up, answers ID? READ START STOP RATE PING ECHO.
- The wire between them is modelled from the two framings (MEGA fixed at 9600 8N1):
  gross baud mismatch -> 0x00 flood + FE, near miss -> garbage + FE, 8E1/8O1/9N1 -> garbage
  + FE (+PE), 7N1 -> clean text but FE, 7E1/7O1 -> clean text but PE. Any uart4 re-init
  lets its TX pin float, which leaves a junk byte in the MEGA's line buffer, so the next
  sensor command gets "ERR unknown cmd ..." once.
Not modelled: ORE, console RX loss while printing, the typing holdback.
"""
import math, random, re, time
from collections import deque

PCLK_HZ, HSI_HZ, CSI_HZ, LSE_HZ = 48_000_000, 64_000_000, 4_000_000, 32_768
KCLK_NAMES = ["pclk", "hsi", "csi", "lse"]
PRESC = [1, 2, 4, 6, 8, 10, 12, 16, 32, 64, 128, 256]
STOPS = ["0.5", "1", "1.5", "2"]
PROFILES = [
    ("waveshare8ch", "Waveshare 8CH analog-in, Modbus RTU over RS485",
     "baud=9600 data=8 parity=n stop=1 presc=1 clk=pclk fifo=1", True),
    ("nmea-gps", "generic NMEA-0183 GPS module", "baud=9600 data=8 parity=n stop=1 presc=1 clk=pclk fifo=1", False),
    ("ttl-115k2", "generic 115200 8N1 TTL device (modem/console)",
     "baud=115200 data=8 parity=n stop=1 presc=1 clk=pclk fifo=1", False),
]
HELP = [
    "\r\n====================== UART LAB CONSOLE ========================\r\n",
    " ports                     list ports, configs and counters\r\n",
    " cfg <port> k=v ...        reconfigure + reopen a port. keys:\r\n",
    "     baud=<n> data=7|8|9 parity=n|e|o stop=0.5|1|1.5|2\r\n",
    "     presc=1..256 clk=pclk|hsi|csi|lse fifo=0|1 swap=0|1\r\n",
    " bind <port> <profile>     apply a named device profile\r\n",
    " profiles                  list built-in device profiles\r\n",
    " open <port> / close <port>\r\n",
    " send <port> <text>        send text + CRLF\r\n",
    " sendn <port> <text>       send text, no line ending\r\n",
    " sendhex <port> <hh> ...   send raw bytes, e.g. sendhex usart2 01 03\r\n",
    " mon <port> 0|1            echo received bytes to this console\r\n",
    " peek <port>               show latest received burst (use w/ mon 0)\r\n",
    " rs485 <port> 0|1          DE-pin direction control around TX\r\n",
    " clear <port>              zero the RX/TX/error counters\r\n",
    " ?                         this help\r\n",
    " note: clk on uart4/usart2 also retunes usart3+group (shared mux);\r\n",
    "       the console is re-initialised automatically. lse: usart6 only.\r\n",
    "================================================================\r\n",
]
FLUSH_IDLE, FLUSH_LEN, RBUF_LEN = 0.010, 64, 96


def c_atol(s):
    m = re.match(r"\s*([+-]?\d+)", s)
    return int(m.group(1)) if m else 0


class SimMega:
    BAUD, BYTE_T = 9600, 10 / 9600

    def __init__(self, t0):
        self.t0, self.streaming, self.period = t0, True, 1.0
        self.last_stream, self.seq = t0, 0
        self.line = bytearray()
        self.rx = deque()        # (t, byte) arriving from the STM32
        self.tx = deque()        # (t, byte) leaving towards the STM32
        self.tx_free = t0

    def send_line(self, t, s):
        start = max(t, self.tx_free)
        for i, b in enumerate(s.encode() + b"\r\n"): self.tx.append((start + i * self.BYTE_T, b))
        self.tx_free = start + (len(s) + 2) * self.BYTE_T

    def reading(self, t):
        ms = (t - self.t0) * 1000
        temp = 24.0 + 3.0 * math.sin(ms / 60000.0 * 2 * math.pi)
        hum = 55.0 + 8.0 * math.sin(ms / 90000.0 * 2 * math.pi + 1.0)
        s = f"$NAUT,T={temp:.2f},H={hum:.2f},SEQ={self.seq}*"; self.seq += 1
        return s

    def next_stream(self): return self.last_stream + self.period if self.streaming else math.inf

    def stream_tick(self, t):
        self.last_stream = t; self.send_line(t, self.reading(t))

    def receive(self, t, b):
        if b in (0x0D, 0x0A):
            if self.line:
                self.handle(t, self.line.decode("latin-1")); self.line = bytearray()
        elif len(self.line) < 63:
            self.line.append(b)

    def handle(self, t, cmd):
        if cmd == "ID?": self.send_line(t, "NAUTILUS-DUMMY-SENSOR v1 (MEGA2560)")
        elif cmd == "READ": self.send_line(t, self.reading(t))
        elif cmd == "START": self.streaming = True; self.send_line(t, "OK STREAM ON")
        elif cmd == "STOP": self.streaming = False; self.send_line(t, "OK STREAM OFF")
        elif cmd == "PING": self.send_line(t, "PONG")
        elif cmd.startswith("RATE "):
            self.period = max(100, c_atol(cmd[5:])) / 1000; self.send_line(t, f"OK RATE {int(self.period * 1000)}")
        elif cmd.startswith("ECHO "): self.send_line(t, cmd[5:])
        else: self.send_line(t, "ERR unknown cmd - ID? READ START STOP RATE PING ECHO")


class Port:
    def __init__(self, name, baud, rs485=False, de=False, group="234578"):
        self.name, self.group, self.de = name, group, de
        self.baud, self.data, self.parity, self.stop, self.presc_idx, self.kclk = baud, 8, "n", "1", 0, 0
        self.fifo, self.swap, self.rs485 = True, False, rs485
        self.open, self.monitor = False, True
        self.rx = self.tx = self.ore = self.fe = self.ne = self.pe = self.zeros = self.dropped = 0
        self.rbuf, self.last_rx, self.lastfrm, self.frames, self.last_zero_report = bytearray(), 0.0, b"", 0, None


class SimNautilus:
    """Transport interface: write(bytes), read(timeout) -> bytes, close()."""

    def __init__(self, fault=lambda: False, seed=1):
        self.t0 = time.time()
        self.fault = fault  # callable -> True while the sensor's TX wire is "disconnected"
        self.rng = random.Random(seed)
        self.out = bytearray()
        self.line = ""
        self.kclk = {"234578": 0, "16910": 0}
        self.ports = {p.name: p for p in (Port("uart4", 115200), Port("usart2", 9600, rs485=True, de=True),
                                          Port("usart6", 19200, group="16910"))}
        self.mega = SimMega(self.t0)
        self.prev_mega_byte_t, self.acc = -1.0, 0.0
        for p in self.ports.values(): self.apply(p, self.t0)
        self.mega.rx.clear()  # boot-time TX float happens before the MEGA's sketch is listening

    # ---- transport ----
    def write(self, data):
        now = time.time(); self.advance(now)
        for b in data:
            c = chr(b)
            if c in "\r\n":
                if self.line:
                    line, self.line = self.line, ""
                    self.emit("\r\n"); self.handle_command(line, now)
            elif b in (0x08, 0x7F):
                if self.line: self.line = self.line[:-1]; self.emit("\b \b")
            elif len(self.line) < 95:
                self.line += c; self.emit(c)

    def read(self, timeout):
        end = time.time() + timeout
        while True:
            self.advance(time.time())
            if self.out or time.time() >= end: break
            time.sleep(min(0.005, max(0.0, end - time.time())))
        data, self.out = bytes(self.out), bytearray()
        return data

    def close(self): pass

    def emit(self, s): self.out += s.encode("latin-1")

    # ---- clocks ----
    def kernel_hz(self, p):
        return [PCLK_HZ, HSI_HZ, CSI_HZ, LSE_HZ][self.kclk[p.group]]

    def actual_baud(self, p):
        f = self.kernel_hz(p) // PRESC[p.presc_idx]
        if p.baud == 0: return 0
        div = (f + p.baud // 2) // p.baud
        return 0 if div < 0x10 or div > 0xFFFF else f // div

    # ---- port_apply ----
    def apply(self, p, now):
        frame = p.data + (p.parity != "n")
        if frame not in (7, 8, 9):
            self.emit(f"ERR {p.name}: data={p.data} + parity does not fit a 7/8/9-bit frame\r\n"); return False
        self.kclk[p.group] = p.kclk
        if p.name == "uart4": self.mega.rx.append((now, 0xFF))  # TX pin floats during DeInit
        if self.actual_baud(p) == 0:
            p.open = False
            self.emit(f"ERR {p.name}: HAL_UART_Init failed - baud unreachable with clk={KCLK_NAMES[p.kclk]} "
                      f"presc={PRESC[p.presc_idx]}?\r\n")
            return False
        p.rbuf = bytearray(); p.open = True
        return True

    # ---- time-ordered bench events ----
    def advance(self, now):
        u4, mega = self.ports["uart4"], self.mega
        while True:
            t_rx = mega.rx[0][0] if mega.rx else math.inf
            t_tx = mega.tx[0][0] if mega.tx else math.inf
            t_st = mega.next_stream()
            if t_st < now - 5:  # don't replay a long idle stretch frame by frame
                mega.last_stream = now - mega.period; t_st = mega.next_stream()
            t_fl = min((p.last_rx + FLUSH_IDLE for p in self.ports.values() if p.rbuf), default=math.inf)
            t = min(t_rx, t_tx, t_st, t_fl)
            if t > now: return
            if t == t_rx: mega.receive(t, mega.rx.popleft()[1])
            elif t == t_st: mega.stream_tick(t)
            elif t == t_tx: self.mega_to_stm(u4, t, mega.tx.popleft()[1])
            else:
                for p in self.ports.values():
                    if p.rbuf and p.last_rx + FLUSH_IDLE <= t: self.flush(p, t)

    def mega_to_stm(self, p, t, b):
        in_run = t - self.prev_mega_byte_t <= SimMega.BYTE_T * 1.01
        self.prev_mega_byte_t = t
        if self.fault() or not p.open: return
        ratio = self.actual_baud(p) / SimMega.BAUD
        out = []
        if abs(ratio - 1) <= 0.03:
            frame = p.data + (p.parity != "n")
            if p.data == 8 and p.parity == "n": out = [b]
            elif p.data == 7 and p.parity == "n": out = [b & 0x7F]; p.fe += 1   # MEGA's bit 7 read as the stop bit
            elif p.data == 7:                                                  # MEGA's bit 7 read as parity
                out = [b]; ones = bin(b & 0x7F).count("1")
                if (ones % 2 == 1) == (p.parity == "e"): p.pe += 1
            elif frame == 9:  # MEGA's stop bit read as bit 9, the next start bit as our stop bit
                if in_run: out = [self.junk()]; p.fe += 1; p.pe += p.parity != "n" and self.rng.random() < 0.5
                else:
                    out = [b]
                    if p.parity != "n" and (bin(b).count("1") % 2 == 0) == (p.parity == "e"): p.pe += 1
        elif ratio >= 8:  # every low run of the 9600 frame looks like a whole 0x00 frame + FE
            bits = [0] + [(b >> i) & 1 for i in range(8)] + [1]
            runs = sum(1 for i, v in enumerate(bits) if v == 0 and (i == 0 or bits[i - 1] == 1))
            out = [0] * runs; p.fe += runs
        else:
            self.acc += ratio; n = int(self.acc); self.acc -= n
            out = [self.junk() for _ in range(n)]; p.fe += n
        for x in out: self.stm_rx(p, t, x)

    def junk(self):
        while (b := self.rng.randrange(1, 256)) in (0x0A, 0x0D): pass
        return b

    def stm_rx(self, p, t, b):
        if p.rbuf and t - p.last_rx >= FLUSH_IDLE: self.flush(p, p.last_rx + FLUSH_IDLE)
        p.rx += 1; p.last_rx = t
        if len(p.rbuf) >= RBUF_LEN: self.flush(p, t)
        p.rbuf.append(b)
        if len(p.rbuf) >= FLUSH_LEN: self.flush(p, t)

    def flush(self, p, t):
        buf, p.rbuf = bytes(p.rbuf), bytearray()
        if not buf: return
        if not any(buf):
            p.zeros += len(buf)
            if p.last_zero_report is None: p.last_zero_report = self.t0
            if t - p.last_zero_report >= 5.0:
                p.last_zero_report = t
                self.emit(f"[{p.name}] {p.zeros} x 0x00 suppressed (RX line low: wiring/baud?)\r\n")
            return
        p.lastfrm, p.frames = buf, p.frames + 1
        if p.monitor: self.print_frame("RX", p, buf)

    def print_frame(self, tag, p, buf):
        hexes = "".join(f" {x:02X}" for x in buf)
        text = "".join(chr(x) if 0x20 <= x < 0x7F else "." for x in buf)
        self.emit(f"[{p.name}] {tag} {len(buf)}B:{hexes}  |{text}|\r\n")

    # ---- STM32 -> device ----
    def port_tx(self, p, data, now):
        if not p.open: self.emit(f"{p.name} is closed\r\n"); return
        if p.name == "uart4":
            ratio = self.actual_baud(p) / SimMega.BAUD
            clean = abs(ratio - 1) <= 0.03 and p.data == 8 and p.parity == "n" and p.stop != "0.5"
            byte_t = 10 / max(self.actual_baud(p), 1)
            for i, b in enumerate(data):
                self.mega.rx.append((now + (i + 1) * byte_t, b if clean else (b | 0x80)))
        p.tx += len(data)
        self.emit(f"[{p.name}] TX {len(data)}B\r\n")

    # ---- console commands (handle_command) ----
    def handle_command(self, line, now):
        raw = None
        if line.startswith("send ") or line.startswith("sendn "):
            sp = line.find(" ", 5 if line[4] == " " else 6)
            if sp >= 0: raw = line[sp + 1:]
        tok = re.findall(r"[^ \t]+", line)[:20]
        if not tok: return
        if tok[0] in ("?", "help"): self.emit("".join(HELP) + "> "); return
        if tok[0] == "ports": self.cmd_ports(); self.emit("> "); return
        if tok[0] == "profiles":
            for name, desc, cfg, rs485 in PROFILES:
                self.emit(f" {name:<12} {desc}{' [rs485]' if rs485 else ''}\r\n      {cfg}\r\n")
            self.emit("> "); return
        p = self.ports.get(tok[1]) if len(tok) >= 2 else None
        if not p: self.emit("usage: <cmd> <uart4|usart2|usart6> ... - '?' for help\r\n> "); return
        n, cmd = len(tok), tok[0]
        if cmd == "cfg":
            if n < 3: self.emit("cfg needs at least one k=v\r\n")
            elif self.apply_kv(p, tok[2:]) and self.apply(p, now):
                self.emit(f"{p.name} reconfigured: {p.baud} baud (actual {self.actual_baud(p)}), clk={KCLK_NAMES[p.kclk]}\r\n")
        elif cmd == "bind" and n >= 3:
            prof = next((x for x in PROFILES if x[0] == tok[2]), None)
            if not prof: self.emit("unknown profile - try 'profiles'\r\n")
            elif self.apply_kv(p, prof[2].split()):
                p.rs485 = prof[3] and p.de
                if self.apply(p, now): self.emit(f"{p.name} bound to '{prof[0]}' ({prof[1]})\r\n")
        elif cmd == "open":
            if self.apply(p, now): self.emit(f"{p.name} open\r\n")
        elif cmd == "close":
            p.open = False
            if p.name == "uart4": self.mega.rx.append((now, 0xFF))
            self.emit(f"{p.name} closed\r\n")
        elif cmd == "send" and raw is not None: self.port_tx(p, (raw + "\r\n").encode("latin-1"), now)
        elif cmd == "sendn" and raw is not None: self.port_tx(p, raw.encode("latin-1"), now)
        elif cmd == "sendhex" and n >= 3:
            out = []
            for h in tok[2:66]:
                if not re.fullmatch(r"[+-]?(0[xX])?[0-9a-fA-F]+", h) or not 0 <= int(h, 16) <= 0xFF:
                    self.emit(f"ERR: '{h}' is not a hex byte\r\n"); break
                out.append(int(h, 16))
            else: self.port_tx(p, bytes(out), now)
        elif cmd == "mon" and n >= 3:
            p.monitor = tok[2][0] == "1"; self.emit(f"{p.name} monitor {'ON' if p.monitor else 'OFF'}\r\n")
        elif cmd == "peek":
            if not p.lastfrm: self.emit(f"[{p.name}] nothing received yet\r\n")
            else:
                self.print_frame("last RX", p, p.lastfrm)
                self.emit(f"        {p.frames} bursts, {p.rx} bytes total\r\n")
        elif cmd == "rs485" and n >= 3:
            if not p.de: self.emit(f"{p.name} has no DE pin wired\r\n")
            else: p.rs485 = tok[2][0] == "1"; self.emit(f"{p.name} rs485 {'ON' if p.rs485 else 'OFF'}\r\n")
        elif cmd == "clear":
            p.rx = p.tx = p.ore = p.fe = p.ne = p.pe = p.zeros = p.dropped = 0
            self.emit(f"{p.name} counters cleared\r\n")
        else: self.emit("unknown command - type ? for help\r\n")
        self.emit("> ")

    def apply_kv(self, p, toks):
        """Mirrors apply_kv_tokens: fields are written as they parse, so a later bad token
        leaves the earlier ones in the stored config."""
        for t in toks:
            if "=" not in t: self.emit(f"ERR: '{t}' is not k=v\r\n"); return False
            k, v = t.split("=", 1)
            if k == "baud":
                b = c_atol(v)
                if b < 300 or b > 12_500_000: self.emit("ERR: baud out of range\r\n"); return False
                p.baud = b
            elif k == "data":
                d = c_atol(v)
                if d < 7 or d > 9: self.emit("ERR: data=7|8|9\r\n"); return False
                p.data = d
            elif k == "parity":
                if v[:1] not in ("n", "e", "o") or not v: self.emit("ERR: parity=n|e|o\r\n"); return False
                p.parity = v[0]
            elif k == "stop":
                if v not in STOPS: self.emit("ERR: stop=0.5|1|1.5|2\r\n"); return False
                p.stop = v
            elif k == "presc":
                if c_atol(v) not in PRESC: self.emit("ERR: presc=1|2|4|6|8|10|12|16|32|64|128|256\r\n"); return False
                p.presc_idx = PRESC.index(c_atol(v))
            elif k == "clk":
                if v not in KCLK_NAMES: self.emit("ERR: clk=pclk|hsi|csi|lse\r\n"); return False
                if v == "lse" and p.name != "usart6":
                    self.emit("ERR: lse would kill the shared-mux console - usart6 only\r\n"); return False
                p.kclk = KCLK_NAMES.index(v)
            elif k == "fifo": p.fifo = v[:1] == "1"
            elif k == "swap": p.swap = v[:1] == "1"
            else: self.emit(f"ERR: unknown key '{k}'\r\n"); return False
        return True

    def cmd_ports(self):
        for p in self.ports.values():
            self.emit(f"\r\n {p.name:<6} {'OPEN' if p.open else 'closed':<6} {p.baud} {p.data}{p.parity.upper()}{p.stop}  "
                      f"presc=/{PRESC[p.presc_idx]} clk={KCLK_NAMES[p.kclk]}({self.kernel_hz(p)}Hz) "
                      f"actual={self.actual_baud(p)}\r\n")
            self.emit(f"        fifo={int(p.fifo)} swap={int(p.swap)} rs485={int(p.rs485)} mon={int(p.monitor)}  "
                      f"rx={p.rx} tx={p.tx}  ORE={p.ore} FE={p.fe} NE={p.ne} PE={p.pe}  "
                      f"zeros={p.zeros} dropped={p.dropped}\r\n")
