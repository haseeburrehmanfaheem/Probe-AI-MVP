"""Board APIs on top of the Nautilus UART LAB console, as defined in board_config.json.

An API renders a console command, sends it, and succeeds only if a reply line matches
its "expect" pattern (success forms are whitelisted; anything else is a failure).
APIs with "sensor_expect" are 'send <port> ...' commands: after the console accepts
them, the sensor's answer arrives later in RX bursts and is matched separately. A sensor
"ERR ..." answer is retried (stale bytes in its line buffer after a port re-init).

A port-level cfg failure ("ERR <port>: ...") still changes the firmware's stored config,
and the next successful cfg would silently apply it, so the last good config is restored.

Sampling starts the stream, collects data lines, stops it, and diffs the port's link
counters from 'ports' before and after (read while the stream is off, because a long
console print during a frame overruns the lab port's FIFO).
"""
import math, re, threading, time
from console import NautilusConsole, parse_ports

VALUE_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(-?\d+(?:\.\d+)?)")
LINK_KEYS = ["rx", "ORE", "FE", "NE", "PE", "zeros", "dropped"]
TEXT_ARG = re.compile(r"[\x21-\x7e]+(?: [\x21-\x7e]+)*")


def fill(template, values, escape=False):
    """Substitute {name} for known names only, so regex quantifiers like {2} survive."""
    def sub(m):
        if m.group(1) not in values: return m.group(0)
        v = str(values[m.group(1)])
        return re.escape(v) if escape else v
    return re.sub(r"\{(\w+)\}", sub, template)


class SerialTransport:
    def __init__(self, cfg):
        import serial
        # serial_for_url also accepts "socket://host:port" for a serial port forwarded over TCP
        self.ser = serial.serial_for_url(cfg["port"], cfg.get("baud_rate", 115200), timeout=cfg.get("timeout_seconds", 0.02))
        time.sleep(cfg.get("open_delay_seconds", 0.3)); self.ser.reset_input_buffer()
    def write(self, data): self.ser.write(data); self.ser.flush()
    def read(self, timeout): return self.ser.read(max(1, self.ser.in_waiting))
    def close(self): self.ser.close()


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
        self.config = config; self.con = None; self.lock = threading.Lock(); self.sim_fault = False
        self.vars = config.get("vars", {})
        self.port = self.vars.get("port")
        self.data_re = re.compile(config["sampling"]["data_line_pattern"])
        self.sensor_err_re = re.compile(config.get("sensor_error_pattern", "^ERR"))
        self.good_cfg = {}  # port -> last config known to be in the hardware
        self.on_io = lambda d, line, bad=False: None

    @property
    def transport_type(self): return self.config["transport"]["type"]

    # ---- connection ----
    def _console(self):
        if self.con is None:
            if self.transport_type == "sim":
                from sim import SimNautilus
                t = SimNautilus(fault=lambda: self.sim_fault)
            else:
                t = SerialTransport(self.config["transport"])
            self.con = NautilusConsole(t, self._async)
            try:
                self.con.quiet(idle=0.1, max_wait=1.0)
                for cmd in self.config.get("init_commands", []): self._command(fill(cmd, self.vars))
                self._ports()
            except Exception:
                self._reset(); raise
        return self.con

    def _reset(self):
        if self.con:
            try: self.con.t.close()
            except Exception: pass
        self.con = None

    def close(self):
        with self.lock: self._reset()

    def _async(self, kind, port, text):
        if kind == "sensor" and not self.data_re.search(text):
            self.on_io("sensor", f"{port}: {text}", bool(self.sensor_err_re.search(text)))
        elif kind == "notice":
            self.on_io("sensor", text, True)

    def _command(self, cmd, expect=None):
        self.on_io("tx", cmd)
        r = self.con.command(cmd, timeout=self.config.get("command_timeout_seconds", 2))
        ok = r.echo_ok and (expect is None or any(expect.search(l) for l in r.lines))
        for line in r.lines:
            if line.strip(): self.on_io("rx", line, not ok)
        if not r.echo_ok: self.on_io("rx", f"(echo mismatch: {r.echo!r})", True)
        return ok, r

    def _ports(self):
        ok, r = self._command("ports", re.compile(r"^ \w+\s+(OPEN|closed) "))
        ports = parse_ports(r.lines) if ok else {}
        self._ports_to_good(ports)
        return ports

    # ---- APIs ----
    def call(self, name, args=None, on_io=None):
        with self.lock:
            self.on_io = on_io or (lambda d, line, bad=False: None)
            try:
                self._console()
                return self._call(name, dict(args or {}))
            except Exception:
                self._reset(); raise

    def _check_args(self, api, args):
        for p, spec in api.get("params", {}).items():
            if p not in args: return f"missing arg '{p}'"
            v = args[p]
            if "enum" in spec:
                match = next((e for e in spec["enum"] if str(e).lower() == str(v).strip().lower()), None)
                if match is None: return f"{p} must be one of {spec['enum']}"
                args[p] = match
            elif spec.get("type") == "integer":
                try: args[p] = int(float(v)); assert args[p] == float(v)
                except (ValueError, TypeError, AssertionError): return f"{p} must be an integer"
            elif not TEXT_ARG.fullmatch(str(v)) or len(str(v)) > spec.get("max_length", 60):
                return f"{p} must be printable ASCII with single spaces, at most {spec.get('max_length', 60)} characters"
        return None

    def _call(self, name, args):
        api = self.config["apis"].get(name)
        if api is None: return {"ok": False, "error": f"unknown board API '{name}'"}
        if err := self._check_args(api, args): return {"ok": False, "error": err}
        values = {**self.vars, **args}
        cmd = fill(api["command"], values)
        expect = re.compile(fill(api.get("expect", "."), values, escape=True))
        port = fill(api.get("port", "{port}"), self.vars)
        mark, sensor_err = self.con.mark(), None

        for attempt in range(self.config.get("sensor_retries", 1) + 1):
            sent_at = self.con.mark()
            ok, r = self._command(cmd, expect)
            result = {"ok": ok, "sent": cmd, "reply": r.lines}
            if not ok:
                result["error"] = "the console did not accept the command" if r.echo_ok else "the console garbled the command"
                if api.get("kind") == "cfg" and any(l.startswith(f"ERR {port}:") for l in r.lines):
                    result.update(self._restore(port))
                break
            if api.get("kind") == "cfg":
                self.con.streams.pop(port, None)  # bytes from the old framing would corrupt the next line
                for k, v in (t.split("=", 1) for t in cmd.split()[2:]):
                    self.good_cfg.setdefault(port, {})[k] = int(v) if k in ("baud", "data") else v
            if api.get("parse") == "ports":
                result["ports"] = parse_ports(r.lines); del result["reply"]
                self._ports_to_good(result["ports"])
            if "sensor_expect" not in api: break
            kind, line = self.con.wait_sensor(sent_at, port, re.compile(fill(api["sensor_expect"], values, escape=True)),
                                              self.sensor_err_re, api.get("timeout_seconds", self.config.get("sensor_timeout_seconds", 2.5)))
            if kind == "ok":
                result["sensor_reply"] = line
                if sensor_err: result["retried_after"] = sensor_err
                break
            result["ok"] = False
            if kind == "err":
                sensor_err = line; result["error"] = f"the sensor answered {line!r}"
                continue
            result["error"] = "no reply from the sensor"
            break
        if not result["ok"]:
            other = [l for l in self.con.lines_since(mark, port) if not self.data_re.search(l)]
            frames = sum(1 for l in self.con.lines_since(mark, port) if self.data_re.search(l))
            if other: result["sensor_lines"] = other[-8:]
            if frames: result["data_frames_seen"] = frames
            if notes := self.con.notices_since(mark, port): result["notices"] = notes[-3:]
        return result

    def _ports_to_good(self, ports):
        for name, p in ports.items():
            if p["open"]: self.good_cfg[name] = {k: p[k] for k in ("baud", "data", "parity", "stop")}

    def _restore(self, port):
        good = self.good_cfg.get(port)
        if not good: return {"restore": "no known good config to restore"}
        cmd = f"cfg {port} " + " ".join(f"{k}={good[k]}" for k in ("baud", "data", "parity", "stop"))
        ok, r = self._command(cmd, re.compile(rf"^{re.escape(port)} reconfigured: "))
        return {"restored_config": good} if ok else {"restore": "failed", "restore_reply": r.lines}

    # ---- sampling ----
    def sample(self, seconds, on_io=None, on_reading=None):
        on_reading = on_reading or (lambda r: None)
        s = self.config["sampling"]
        seq_field = s.get("sequence_field")
        with self.lock:
            self.on_io = on_io or (lambda d, line, bad=False: None)
            try:
                self._console()
                before = self._ports().get(self.port) if s.get("link_counters") else None
                started = self._call(s["start_api"], {}) if s.get("start_api") else {"ok": True}
                readings, other, seqs = [], [], []
                if started["ok"]:
                    mark = self.con.mark(); end = time.time() + seconds
                    while time.time() < end:
                        self.con.pump(0.05)
                        for line in self.con.lines_since(mark, self.port):
                            if self.data_re.search(line):
                                values = {k: float(v) for k, v in VALUE_RE.findall(line)}
                                if seq_field in values: seqs.append(int(values.pop(seq_field)))
                                readings.append(values); on_reading(values)
                            else: other.append(line)
                        mark = self.con.mark()
                    if s.get("stop_api"): self._call(s["stop_api"], {})
                after = self._ports().get(self.port) if s.get("link_counters") else None
            except Exception:
                self._reset(); raise
        result = {"ok": started["ok"], "seconds": seconds, "samples": len(readings), "fields": field_stats(readings)}
        if not started["ok"]: result.update(error="could not start streaming", start=started)
        if seqs: result["seq_gaps"] = sum(max(0, b - a - 1) for a, b in zip(seqs, seqs[1:]))
        if other: result["garbled_lines"] = len(other); result["garbled_examples"] = other[:3]
        if before and after:
            result["link"] = {k: after.get(k, 0) - before.get(k, 0) for k in LINK_KEYS}
            result["port_config"] = {k: after[k] for k in ("open", "baud", "data", "parity", "stop", "actual_baud")}
        return result
