"""Client for the Nautilus UART LAB console (Core/Src/uart_lab.c) on USART3.

Framing, as the firmware prints it:
- Each typed character is echoed, then Enter prints "\\r\\n", the reply lines, and the
  prompt "> " (no newline). A command block is atomic: nothing async lands inside it.
- Async lines (RX bursts, 0x00 summaries) only appear between a prompt and the next
  echo. The first one is glued onto the prompt, so a line may start with "> ".
- RX bursts carry the sensor's bytes as hex. They are timing-framed (10 ms idle or
  64 bytes), so the bytes are fed to the port's decoder (decoders.py): CRLF text lines or
  binary frames. The ASCII column is never used: CR/LF and real dots both print as '.'.
"""
import re, time
from collections import deque
from decoders import LineDecoder

PROMPT = "> "
RX_BURST = re.compile(r"^\[(\w+)\] RX (\d+)B:((?: [0-9A-F]{2})+)  \|")
ZERO_NOTICE = re.compile(r"^\[(\w+)\] (\d+) x 0x00 suppressed")
PORT_LINE1 = re.compile(r"^ (\w+)\s+(OPEN|closed)\s+(\d+) ([789])([NEO])(0\.5|1\.5|1|2)  "
                        r"presc=/(\d+) clk=(\w+)\((\d+)Hz\) actual=(\d+)$")
KV = re.compile(r"(\w+)=(\d+)")


def parse_ports(lines):
    """'ports' reply -> {port: {state, baud, data, parity, stop, presc, clk, clk_hz, actual, counters...}}."""
    ports, current = {}, None
    for line in lines:
        if m := PORT_LINE1.match(line):
            name, state, baud, data, parity, stop, presc, clk, hz, actual = m.groups()
            current = ports[name] = {"open": state == "OPEN", "baud": int(baud), "data": int(data),
                                     "parity": parity.lower(), "stop": stop, "presc": int(presc),
                                     "clk": clk, "clk_hz": int(hz), "actual_baud": int(actual)}
        elif current is not None and line.strip() and (pairs := KV.findall(line)):
            current.update({k: int(v) for k, v in pairs}); current = None
    return ports


class Reply:
    def __init__(self, command):
        self.command, self.echo, self.lines, self.complete = command, None, [], False

    @property
    def echo_ok(self): return self.echo == self.command


class NautilusConsole:
    """Single-threaded: every method pumps the transport itself. Callers serialise access."""

    def __init__(self, transport, on_async=None):
        self.t = transport
        self.on_async = on_async or (lambda kind, port, text: None)
        self.buf = ""
        self.reply = None                      # Reply being collected, if a command is in flight
        self.decoders = {}                     # port -> decoder (decoders.py); LineDecoder until a sensor is attached
        self.sensor_lines = deque(maxlen=500)  # (seq, port, text): replies and non-data lines
        self.readings = deque(maxlen=4000)     # (seq, port, values): decoded data frames
        self.notices = deque(maxlen=50)        # (seq, port, text) for 0x00 summaries
        self.seq = 0
        self.last_byte = 0.0

    # ---- reading ----
    def pump(self, seconds=0.0):
        """Read and parse for at least `seconds` (one read if 0)."""
        end = time.time() + seconds
        while True:
            data = self.t.read(min(0.05, max(0.0, end - time.time())) or 0.01)
            if data:
                self.last_byte = time.time()
                self.buf += data.decode("latin-1")
                self._parse()
            if time.time() >= end: return

    def quiet(self, idle=0.03, max_wait=0.3):
        """Wait until the console has been silent for `idle` s, so a command is less likely
        to collide with an async burst print (the console has ~2 bytes of RX buffering)."""
        end = time.time() + max_wait
        while time.time() < end:
            self.pump(0.01)
            if time.time() - self.last_byte >= idle: return

    def _parse(self):
        while self.buf:
            if self.buf.startswith(PROMPT):
                self.buf = self.buf[len(PROMPT):]
                if self.reply and self.reply.echo is not None: self.reply.complete = True
                continue
            if self.buf == ">": return  # first half of a prompt
            i = self.buf.find("\r\n")
            if i < 0: return
            line, self.buf = self.buf[:i], self.buf[i + 2:]
            self._line(line)

    def _line(self, line):
        if m := RX_BURST.match(line):
            port = m.group(1)
            for kind, payload in self.decoder(port).feed(bytes.fromhex(m.group(3))):
                self.seq += 1
                if kind == "reading": self.readings.append((self.seq, port, payload)); continue
                if kind == "line": self.sensor_lines.append((self.seq, port, payload))
                self.on_async("sensor" if kind == "line" else "bad", port, payload)
            return
        if m := ZERO_NOTICE.match(line):
            self.seq += 1; self.notices.append((self.seq, m.group(1), line))
            self.on_async("notice", m.group(1), line)
            return
        r = self.reply
        if r is None or r.complete: return           # stray output (banner after a reset, ...)
        if r.echo is None: r.echo = line              # first non-async line after sending is the echo
        else: r.lines.append(line)

    # ---- commands ----
    def command(self, line, timeout=2.0, retries=2):
        """Send one console command and return its Reply. Retries when the echo does not
        match (characters lost while the firmware was printing)."""
        if "\r" in line or "\n" in line or "  " in line or line != line.strip() or not line:
            raise ValueError(f"console commands are single-spaced, one line: {line!r}")
        for attempt in range(retries + 1):
            self.quiet()
            self.reply = r = Reply(line)
            self._type(line)
            end = time.time() + timeout
            while not r.complete and time.time() < end: self.pump()
            self.reply = None
            if not r.complete:
                # Seen right after the port opens: a byte of the line (or its CR) goes missing and the console
                # never prompts. End whatever line it holds and try again before giving up.
                if attempt < retries: self.t.write(b"\r"); self.pump(0.3); continue
                raise TimeoutError(f"no prompt after {line!r} (is the console on this port?)")
            if r.echo_ok: return r
        return r

    def _type(self, line, echo_timeout=0.25):
        """Type a command one character at a time, waiting for each echo before the next.
        The console polls a 1-byte receive register and blocks while it prints RX bursts,
        so a whole line written at once can overrun it (and an uncleared overrun leaves the
        console deaf until reset). With one byte in flight there is nothing to overrun."""
        for i, ch in enumerate(line):
            self.t.write(ch.encode("latin-1"))
            end = time.time() + echo_timeout
            while not self.buf.endswith(line[:i + 1]) and time.time() < end: self.pump()
        self.t.write(b"\r")

    def decoder(self, port): return self.decoders.setdefault(port, LineDecoder())

    def set_decoder(self, port, decoder): self.decoders[port] = decoder

    def mark(self): return self.seq

    def readings_since(self, mark, port=None):
        return [v for s, p, v in self.readings if s > mark and (port is None or p == port)]

    def lines_since(self, mark, port=None):
        return [text for s, p, text in self.sensor_lines if s > mark and (port is None or p == port)]

    def notices_since(self, mark, port=None):
        return [text for s, p, text in self.notices if s > mark and (port is None or p == port)]

    def wait_sensor(self, mark, port, ok_re, err_re, timeout):
        """Wait for a sensor line on `port` after `mark` matching ok_re or err_re.
        Returns (kind, line) with kind in {"ok", "err", None}."""
        end = time.time() + timeout
        seen = mark
        while True:
            for s, p, text in list(self.sensor_lines):
                if s <= seen or p != port: continue
                seen = s
                if ok_re.search(text): return "ok", text
                if err_re and err_re.search(text): return "err", text
            if time.time() >= end: return None, None
            self.pump()
