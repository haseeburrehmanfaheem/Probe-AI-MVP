"""Simulated Nautilus UART LAB bench: the STM32 console firmware plus the MEGA dummy sensor.

Byte-level stand-in for the ST-Link VCP, so the real console client is exercised:
- SimNautilus mirrors Core/Src/uart_lab.c: echo, prompt, every command and reply string,
  the 'ports' table, 10 ms / 64 B burst framing, the 5 s 0x00 summary, peek, counters, and
  the quirks (double-space send payload, failed cfg still updates the stored config,
  'data + parity does not fit' leaves the port open on its old settings).
- SimMega mirrors tools/dummy_sensor/dummy_sensor.ino: 9600 8N1, streams $NAUT
  (or, as the motion sketch in webui/tools/motion_sensor, $MOTN level/flag frames)
  frames at 1 Hz from power-up, answers ID? READ START STOP RATE PING ECHO.
- SimWT61PC is a WitMotion WT61PC-TTL: binary 55 51/52/53 frames at 1 Hz (as the bench unit shipped), 9600 baud,
  FF AA register writes (unlock, rate, baud after save, calibration, yaw zero). Its pose
  follows the "Simulate tilt" button: a few seconds of roll/pitch/yaw sweeps.
- Devices are plugged into lab ports by layout() ({port: "pth" | "motion" | "wt61pc"}).
- The wire between them is modelled from the two framings (MEGA fixed at 9600 8N1):
  gross baud mismatch -> 0x00 flood + FE, near miss -> garbage + FE, 8E1/8O1/9N1 -> garbage
  + FE (+PE), 7N1 -> clean text but FE, 7E1/7O1 -> clean text but PE. Any port re-init
  lets its TX pin float, which leaves a junk byte in the MEGA's line buffer, so the next
  sensor command gets "ERR unknown cmd ..." once.
- Console overrun: the firmware polls USART3's 1-byte RDR and blocks while printing a burst
  (~87 us per char). More than one byte arriving during a print overruns it, and since
  service_console() never clears ORE the console goes deaf until reset. Modelled as: a
  multi-byte host write while a print is in progress makes the console deaf.
Not modelled: lab-port ORE, the typing holdback.
"""
import math, random, re, struct, time
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
    IDS = {"pth": "NAUTILUS-DUMMY-SENSOR v1 (MEGA2560)", "motion": "NAUTILUS-MOTION-SENSOR v1 (MEGA2560)"}

    def __init__(self, t0, kind="pth", motion=lambda: False):
        self.kind, self.motion = kind, motion  # which sketch is running; callable: whether something moves
        self.device = lambda: self.kind
        self.baud, self.prev_byte_t = self.BAUD, -1.0
        self.level, self.flag = 0.06, 0
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
        if self.device() == "motion":
            # PIR-style presence level: eases toward ~0.85 while something moves, decays to a noisy floor.
            target = 0.85 + 0.08 * math.sin(t * 7) if self.motion() else 0.06
            self.level = min(1.0, max(0.0, self.level + (target - self.level) * 0.35 + random.gauss(0, 0.015)))
            self.flag = 1 if self.level > 0.5 else 0 if self.level < 0.3 else self.flag
            s = f"$MOTN,L={self.level:.2f},M={self.flag},SEQ={self.seq}*"; self.seq += 1
            return s
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
        if cmd == "ID?": self.send_line(t, self.IDS.get(self.device(), self.IDS["pth"]))
        elif cmd == "READ": self.send_line(t, self.reading(t))
        elif cmd == "START":
            if not self.streaming: self.last_stream = t  # resume from now, don't replay the stopped stretch
            self.streaming = True; self.send_line(t, "OK STREAM ON")
        elif cmd == "STOP": self.streaming = False; self.send_line(t, "OK STREAM OFF")
        elif cmd == "PING": self.send_line(t, "PONG")
        elif cmd.startswith("RATE "):
            self.period = max(100, c_atol(cmd[5:])) / 1000; self.send_line(t, f"OK RATE {int(self.period * 1000)}")
        elif cmd.startswith("ECHO "): self.send_line(t, cmd[5:])
        else: self.send_line(t, "ERR unknown cmd - ID? READ START STOP RATE PING ECHO")

    def port_reinit(self, t): self.rx.append((t, 0xFF))  # the STM32's TX pin floats during DeInit


class SimWT61PC:
    """WitMotion WT61PC-TTL: always streaming 55 51/52/53 frames; config writes FF AA <reg> <lo> <hi>."""
    RATES = {0x01: 0.2, 0x02: 0.5, 0x03: 1, 0x04: 2, 0x05: 5, 0x06: 10, 0x07: 20, 0x08: 50, 0x09: 100, 0x0A: 125, 0x0B: 200}
    BAUDS = {0x02: 9600, 0x06: 115200}

    def __init__(self, t0, motion=lambda: False, seed=2):
        self.kind, self.motion, self.rng = "wt61pc", motion, random.Random(seed)
        self.baud, self.pending_baud, self.prev_byte_t = 9600, None, -1.0
        self.period, self.last_stream, self.streaming = 1.0, t0, True  # 1 Hz, like the bench unit as found
        self.rx, self.tx, self.tx_free = deque(), deque(), t0
        self.cmd, self.unlocked_until = bytearray(), 0.0
        self.pose, self.yaw_offset, self.motion_t0, self.last_t = [0.4, -0.3, 12.0], 0.0, None, t0
        self.rates = [0.0, 0.0, 0.0]

    @property
    def byte_t(self): return 10 / self.baud

    def next_stream(self): return self.last_stream + self.period

    def stream_tick(self, t):
        self.last_stream = t
        if self.tx and self.tx[-1][0] > t + self.period: return  # link saturated: drop this sample, like the real module
        self.update_pose(t)
        roll, pitch, yaw = self.pose
        yaw_out = (yaw - self.yaw_offset + 180) % 360 - 180
        r, p = math.radians(roll), math.radians(pitch)
        g = lambda: self.rng.gauss(0, 0.004)
        acc = [-math.sin(p) + g(), math.sin(r) * math.cos(p) + g(), math.cos(r) * math.cos(p) + g()]
        gyro = [w + self.rng.gauss(0, 0.08) for w in self.rates]
        temp = 26.4 + 0.3 * math.sin(t / 40)
        out = self.frame(0x51, [a / 16 * 32768 for a in acc] + [temp * 100])
        out += self.frame(0x52, [w / 2000 * 32768 for w in gyro] + [0])
        out += self.frame(0x53, [x / 180 * 32768 for x in (roll, pitch, yaw_out)] + [0])
        start = max(t, self.tx_free)
        for i, b in enumerate(out): self.tx.append((start + i * self.byte_t, b))
        self.tx_free = start + len(out) * self.byte_t

    @staticmethod
    def frame(kind, values):
        body = bytes([0x55, kind]) + struct.pack("<hhhh", *(max(-32768, min(32767, int(round(v)))) for v in values))
        return body + bytes([sum(body) & 0xFF])

    def update_pose(self, t):
        dt, self.last_t = max(t - self.last_t, 0.05), t
        prev = list(self.pose)
        if self.motion():
            if self.motion_t0 is None: self.motion_t0, self.motion_yaw0 = t, self.pose[2]
            u = t - self.motion_t0
            env = min(1.0, u / 0.6)  # ease in
            target = [48 * env * math.sin(2 * math.pi * u / 2.5), 38 * env * math.sin(2 * math.pi * u / 3.1 + 0.8),
                      self.motion_yaw0 + 70 * env * math.sin(2 * math.pi * u / 3.7)]
            k = 1.0
        else:
            self.motion_t0 = None
            target, k = [0.4, -0.3, self.pose[2]], 1 - math.exp(-dt / 0.35)  # settle back flat, keep the heading
        self.pose = [a + (b - a) * k + self.rng.gauss(0, 0.02) for a, b in zip(self.pose, target)]
        self.pose[2] += 0.02 * dt  # slow yaw drift (no magnetometer)
        self.rates = [(a - b) / dt for a, b in zip(self.pose, prev)]

    def receive(self, t, b):
        self.cmd.append(b)
        if len(self.cmd) > 5: del self.cmd[0]
        if len(self.cmd) == 5 and self.cmd[0] == 0xFF and self.cmd[1] == 0xAA:
            reg, lo, hi = self.cmd[2], self.cmd[3], self.cmd[4]; self.cmd.clear()
            self.write(t, reg, lo | hi << 8)

    def write(self, t, reg, value):
        if reg == 0x69 and value == 0xB588: self.unlocked_until = t + 10; return
        if t > self.unlocked_until: return  # locked: writes are ignored silently
        if reg == 0x00:
            if value == 0x0001: self.period, self.pending_baud = 0.1, 9600
            if self.pending_baud: self.baud, self.pending_baud = self.pending_baud, None
        elif reg == 0x03 and value in self.RATES: self.period = 1 / self.RATES[value]
        elif reg == 0x04 and value in self.BAUDS: self.pending_baud = self.BAUDS[value]  # takes effect on save
        elif reg == 0x01 and value == 0x04: self.yaw_offset = self.pose[2]

    def port_reinit(self, t): pass


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

    def __init__(self, fault=lambda: False, seed=1, layout=lambda: {"uart4": "pth"}, motion=lambda: False):
        self.t0 = time.time()
        self.fault = fault  # callable -> True while the sensor's TX wire is "disconnected"
        self.rng = random.Random(seed)
        self.out = bytearray()
        self.line = ""
        self.print_busy_until, self.console_deaf = 0.0, False
        self.kclk = {"234578": 0, "16910": 0}
        self.ports = {p.name: p for p in (Port("uart4", 115200), Port("usart2", 9600, rs485=True, de=True),
                                          Port("usart6", 19200, group="16910"))}
        self.layout, self.motion, self.devices = layout, motion, {}  # port name -> attached device
        self.acc = 0.0
        self.sync_devices(self.t0)
        for p in self.ports.values(): self.apply(p, self.t0)
        for d in self.devices.values(): d.rx.clear()  # boot-time TX float happens before the sketch is listening

    def sync_devices(self, now):
        for port, kind in self.layout().items():
            d = self.devices.get(port)
            if d is not None and d.kind == kind: continue
            if kind == "wt61pc": self.devices[port] = SimWT61PC(now, self.motion)
            elif d is not None and isinstance(d, SimMega): d.kind = kind  # same MEGA, other sketch
            else: self.devices[port] = SimMega(now, kind, self.motion)

    # ---- transport ----
    def write(self, data):
        now = time.time(); self.advance(now)
        if len(data) > 1 and now < self.print_busy_until: self.console_deaf = True  # USART3 overrun, ORE never cleared
        if self.console_deaf: return
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
        if p.name in self.devices: self.devices[p.name].port_reinit(now)
        if self.actual_baud(p) == 0:
            p.open = False
            self.emit(f"ERR {p.name}: HAL_UART_Init failed - baud unreachable with clk={KCLK_NAMES[p.kclk]} "
                      f"presc={PRESC[p.presc_idx]}?\r\n")
            return False
        p.rbuf = bytearray(); p.open = True
        return True

    # ---- time-ordered bench events ----
    def advance(self, now):
        self.sync_devices(now)
        for d in self.devices.values():
            if d.next_stream() < now - 5:  # don't replay a long idle stretch frame by frame
                d.last_stream = now - d.period
        while True:
            best, t = None, math.inf
            for name, d in self.devices.items():
                for kind, tt in (("rx", d.rx[0][0] if d.rx else math.inf), ("tx", d.tx[0][0] if d.tx else math.inf),
                                 ("st", d.next_stream() if d.streaming else math.inf)):
                    if tt < t: best, t = (kind, name, d), tt
            t_fl = min((p.last_rx + FLUSH_IDLE for p in self.ports.values() if p.rbuf), default=math.inf)
            if min(t, t_fl) > now: return
            if t_fl < t:
                for p in self.ports.values():
                    if p.rbuf and p.last_rx + FLUSH_IDLE <= t_fl: self.flush(p, t_fl)
                continue
            kind, name, d = best
            if kind == "rx": d.receive(t, d.rx.popleft()[1])
            elif kind == "st": d.stream_tick(t)
            else: self.dev_to_stm(self.ports[name], d, t, d.tx.popleft()[1])

    def dev_to_stm(self, p, d, t, b):
        in_run = t - d.prev_byte_t <= (10 / d.baud) * 1.01
        d.prev_byte_t = t
        if self.fault() or not p.open: return
        ratio = self.actual_baud(p) / d.baud
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
        out = f"[{p.name}] {tag} {len(buf)}B:{hexes}  |{text}|\r\n"
        self.print_busy_until = max(self.print_busy_until, time.time()) + len(out) * 10 / 115200  # blocking print
        self.emit(out)

    # ---- STM32 -> device ----
    def port_tx(self, p, data, now):
        if not p.open: self.emit(f"{p.name} is closed\r\n"); return
        if (d := self.devices.get(p.name)) is not None:
            ratio = self.actual_baud(p) / d.baud
            clean = abs(ratio - 1) <= 0.03 and p.data == 8 and p.parity == "n" and p.stop != "0.5"
            byte_t = 10 / max(self.actual_baud(p), 1)
            for i, b in enumerate(data):
                d.rx.append((now + (i + 1) * byte_t, b if clean else (b | 0x80)))
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
            if p.name in self.devices: self.devices[p.name].port_reinit(now)
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
