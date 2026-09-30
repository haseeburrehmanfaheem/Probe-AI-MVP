"""Probe cube (Nucleo) transports and the line-protocol client used by the agent tools.

Every board API is a text command defined in board_config.json. A call sends the
rendered command and waits for a reply matching the API's "expect" pattern (or the
global error pattern). Sampling turns streaming on, parses KEY=value data lines,
turns streaming off and returns per-field statistics.
"""
import math, random, re, threading, time
from collections import deque

VALUE_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(-?\d+(?:\.\d+)?)")


class SerialTransport:
    def __init__(self, cfg):
        import serial
        self.ser = serial.Serial(cfg["port"], cfg.get("baud_rate", 115200), timeout=cfg.get("timeout_seconds", 0.1))
        time.sleep(cfg.get("open_delay_seconds", 0.5)); self.ser.reset_input_buffer()
    def write_line(self, line, ending): self.ser.write((line + ending).encode()); self.ser.flush()
    def read_line(self):
        raw = self.ser.readline(); return raw.decode("utf-8", errors="replace").strip() if raw else None
    def close(self): self.ser.close()


class SimTransport:
    """Stand-in for the Nucleo while it isn't connected. Speaks the placeholder API in
    board_config.json and emulates a WT61PC that only produces data at 9600 8N1."""
    SENSOR_UART = (9600, "N", 1)

    def __init__(self, motion):
        self.motion = motion  # callable -> bool, toggled from the UI
        self.out = deque(); self.cfg = {"baud": 115200, "parity": "N", "stop_bits": 1, "ack": 1}
        self.streaming = False; self.next_t = 0.0; self.flip = False

    def write_line(self, line, ending):
        cmd, *args = line.strip().split(",")
        setters = {"SET_BAUD": ("baud", int, lambda v: v in (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400)),
                   "SET_PARITY": ("parity", str, lambda v: v in ("N", "E", "O")),
                   "SET_STOP_BITS": ("stop_bits", int, lambda v: v in (1, 2)),
                   "SET_ACK": ("ack", int, lambda v: v in (0, 1))}
        if cmd == "PING": self.out.append("PONG")
        elif cmd == "GET_CONFIG": self.out.append("CONFIG," + ",".join(f"{k}={v}" for k, v in self.cfg.items()))
        elif cmd in ("STREAM_ON", "STREAM_OFF"): self.streaming = cmd == "STREAM_ON"; self.out.append(f"ACK,{cmd}")
        elif cmd in setters and args:
            key, cast, valid = setters[cmd]
            try: value = cast(args[0])
            except ValueError: value = None
            if value is None or not valid(value): self.out.append(f"ERR,INVALID_VALUE,{cmd},{args[0]}")
            else: self.cfg[key] = value; self.out.append(f"ACK,{cmd},{value}")
        else: self.out.append(f"ERR,UNKNOWN_COMMAND,{line.strip()}")

    def read_line(self):
        if self.out: return self.out.popleft()
        now = time.time()
        uart_ok = (self.cfg["baud"], self.cfg["parity"], self.cfg["stop_bits"]) == self.SENSOR_UART
        if self.streaming and uart_ok and now >= self.next_t:
            self.next_t = now + 0.05; self.flip = not self.flip
            return self._accel(now) if self.flip else self._gyro(now)
        time.sleep(0.01); return None

    def _accel(self, t):
        g = random.gauss
        if self.motion():
            w = 2 * math.pi * 0.8 * t
            ax, ay, az = 0.6 * math.sin(w) + g(0, 0.05), 0.4 * math.cos(w) + g(0, 0.05), 1.0 - 0.3 * abs(math.sin(w)) + g(0, 0.05)
        else:
            ax, ay, az = g(0.01, 0.004), g(-0.02, 0.004), g(1.0, 0.005)
        return f"ACCEL,g,AX={ax:.3f},AY={ay:.3f},AZ={az:.3f}"

    def _gyro(self, t):
        g = random.gauss
        if self.motion():
            w = 2 * math.pi * 0.8 * t
            gx, gy, gz = 150 * math.cos(w) + g(0, 5), 90 * math.sin(w) + g(0, 5), g(0, 10)
        else:
            gx, gy, gz = g(0, 0.3), g(0, 0.3), g(0, 0.3)
        return f"GYRO,dps,GX={gx:.3f},GY={gy:.3f},GZ={gz:.3f}"

    def close(self): pass


def field_stats(readings):
    fields = {}
    for r in readings:
        for k, v in r.items(): fields.setdefault(k, []).append(v)
    stats = {}
    for k, vs in fields.items():
        mean = sum(vs) / len(vs)
        stats[k] = {"n": len(vs), "min": round(min(vs), 4), "max": round(max(vs), 4), "mean": round(mean, 4),
                    "std": round(math.sqrt(sum((v - mean) ** 2 for v in vs) / len(vs)), 4),
                    "abs_max": round(max(abs(v) for v in vs), 4)}
    return stats


class Board:
    """Thread-safe client; blocking methods are meant to be run via asyncio.to_thread."""

    def __init__(self, config):
        self.config = config; self.t = None; self.lock = threading.Lock(); self.sim_motion = False
        self.data_re = re.compile(config["sampling"]["data_line_pattern"])
        self.err_re = re.compile(config.get("error_pattern", "^ERR"))

    @property
    def transport_type(self): return self.config["transport"]["type"]

    def _transport(self):
        if self.t is None:
            self.t = SimTransport(lambda: self.sim_motion) if self.transport_type == "sim" else SerialTransport(self.config["transport"])
        return self.t

    def _reset(self):
        if self.t:
            try: self.t.close()
            except Exception: pass
        self.t = None

    def call(self, name, args=None, on_io=None):
        with self.lock: return self._call(name, args or {}, on_io or (lambda d, l: None))

    def _call(self, name, args, on_io):
        api = self.config["apis"].get(name)
        if api is None: return {"ok": False, "error": f"unknown board API '{name}'"}
        params = api.get("params", {})
        missing = [p for p in params if p not in args]
        if missing: return {"ok": False, "error": f"missing args {missing}"}
        for p, spec in params.items():
            if "enum" in spec and str(args[p]) not in [str(v) for v in spec["enum"]]:
                return {"ok": False, "error": f"{p} must be one of {spec['enum']}"}
        line = api["command"].format(**args)
        try:
            t = self._transport()
            t.write_line(line, self.config.get("line_ending", "\n")); on_io("tx", line)
            ok_re = re.compile(api.get("expect", "^ACK"))
            deadline = time.time() + api.get("timeout_seconds", self.config.get("command_timeout_seconds", 2))
            replies = []
            while time.time() < deadline:
                reply = t.read_line()
                if not reply or self.data_re.search(reply): continue
                on_io("rx", reply); replies.append(reply)
                if self.err_re.search(reply): return {"ok": False, "sent": line, "replies": replies}
                if ok_re.search(reply): return {"ok": True, "sent": line, "replies": replies}
        except Exception:
            self._reset(); raise
        return {"ok": False, "sent": line, "replies": replies, "error": "timed out waiting for a reply"}

    def sample(self, seconds, on_io=None, on_reading=None):
        on_io = on_io or (lambda d, l: None); on_reading = on_reading or (lambda r: None)
        s = self.config["sampling"]
        with self.lock:
            if s.get("start_api"):
                started = self._call(s["start_api"], {}, on_io)
                if not started["ok"]: return {"ok": False, "error": "could not start streaming", "start": started}
            readings = []; end = time.time() + seconds
            try:
                while time.time() < end:
                    line = self.t.read_line()
                    if not line or not self.data_re.search(line): continue
                    values = {k: float(v) for k, v in VALUE_RE.findall(line)}
                    if values: readings.append(values); on_reading(values)
            except Exception:
                self._reset(); raise
            if s.get("stop_api"): self._call(s["stop_api"], {}, on_io)
        return {"ok": True, "seconds": seconds, "samples": len(readings), "fields": field_stats(readings)}

    def close(self):
        with self.lock: self._reset()
