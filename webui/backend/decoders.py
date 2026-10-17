"""Per-port decoders: raw sensor bytes (from the console's hex RX bursts) -> items.

feed(bytes) returns a list of (kind, payload):
  ("line", text)        a text line that is not a data frame (replies, errors, garbage)
  ("reading", values)   one decoded sample, {field: float}
  ("bad", text)         a rate-limited summary of framing/checksum errors
Each decoder keeps running totals in .stats for sampling to diff.
"""
import re, struct, time

VALUE_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)[=:](-?\d+(?:\.\d+)?)")  # KEY=value or KEY:value
MAX_LINE = 256  # a stream with no CRLF for this long is junk; drop it


def printable(raw): return "".join(chr(x) if 0x20 <= x < 0x7F else f"\\x{x:02x}" for x in raw)


class LineDecoder:
    """CRLF-terminated text. Lines matching `pattern` are data frames; their KEY=value (or KEY:value) pairs become fields."""

    def __init__(self, pattern=None):
        self.pattern = re.compile(pattern) if pattern else None
        self.buf = bytearray()
        self.stats = {"readings": 0, "lines": 0}

    def reset(self): self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        *done, rest = self.buf.split(b"\r\n")
        self.buf = bytearray(rest if len(rest) <= MAX_LINE else b"")
        out = []
        for raw in done:
            text = printable(raw)
            if self.pattern and self.pattern.search(text):
                self.stats["readings"] += 1
                out.append(("reading", {k: float(v) for k, v in VALUE_RE.findall(text)}))
            else:
                self.stats["lines"] += 1
                out.append(("line", text))
        return out


class WitMotionDecoder:
    """WitMotion binary protocol: 11-byte frames 55 <type> d0..d7 <sum>, little-endian int16 payloads.
    0x51 accel (+temperature), 0x52 angular rate, 0x53 angles; each 51/52/53 group is merged into one reading."""
    SCALES = {0x51: (("AX", 16 / 32768), ("AY", 16 / 32768), ("AZ", 16 / 32768), ("T", 1 / 100)),
              0x52: (("GX", 2000 / 32768), ("GY", 2000 / 32768), ("GZ", 2000 / 32768)),
              0x53: (("Roll", 180 / 32768), ("Pitch", 180 / 32768), ("Yaw", 180 / 32768))}

    def __init__(self, **_):
        self.buf, self.pending, self.last_bad, self.bad_since = bytearray(), {}, 0.0, [0, 0]
        self.stats = {"readings": 0, "frames_51": 0, "frames_52": 0, "frames_53": 0, "other_frames": 0,
                      "checksum_errors": 0, "junk_bytes": 0}

    def reset(self): self.buf, self.pending = bytearray(), {}

    def feed(self, data):
        self.buf += data
        out, b, st = [], self.buf, self.stats
        while True:
            i = b.find(0x55)
            if i < 0:
                st["junk_bytes"] += len(b); self.bad_since[1] += len(b); b.clear(); break
            if i:
                st["junk_bytes"] += i; self.bad_since[1] += i; del b[:i]
            if len(b) < 11: break
            frame = bytes(b[:11])
            if sum(frame[:10]) & 0xFF != frame[10]:
                st["checksum_errors"] += 1; self.bad_since[0] += 1; del b[:1]; continue
            del b[:11]
            kind = frame[1]
            if kind not in self.SCALES:
                st["other_frames"] += 1; continue
            st[f"frames_{kind:02x}"] += 1
            raw = struct.unpack("<hhhh", frame[2:10])
            part = {name: round(v * k, 4) for (name, k), v in zip(self.SCALES[kind], raw)}
            if self.pending.keys() & part.keys(): out.append(self._emit())
            self.pending.update(part)
            if kind == 0x53: out.append(self._emit())
        if any(self.bad_since) and time.time() - self.last_bad >= 1.0:
            out.append(("bad", f"{self.bad_since[0]} checksum errors, {self.bad_since[1]} junk bytes (baud mismatch, or a frame cut by console output)"))
            self.last_bad, self.bad_since = time.time(), [0, 0]
        return out

    def _emit(self):
        values, self.pending = self.pending, {}
        self.stats["readings"] += 1
        return "reading", values


def make(framing):
    framing = framing or {}
    if framing.get("type") == "witmotion": return WitMotionDecoder()
    return LineDecoder(framing.get("pattern"))
