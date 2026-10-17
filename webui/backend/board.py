"""Board APIs on top of the Nautilus UART LAB console, as defined in board_config.json.

An API renders a console command, sends it, and succeeds only if a reply line matches
its "expect" pattern (success forms are whitelisted; anything else is a failure).
APIs with "sensor_expect" are 'send <port> ...' commands: after the console accepts
them, the sensor's answer arrives later in RX bursts and is matched separately. A sensor
"ERR ..." answer is retried (stale bytes in its line buffer after a port re-init).
Sensor APIs come from the sensor definition (sensors/<id>/sensor.json) and run on the port
the component was given. "pre"/"post" run other APIs around one (WitMotion unlock/save),
and "verify_stream" listens for valid readings afterwards (sensors that never ACK).

A port-level cfg failure ("ERR <port>: ...") still changes the firmware's stored config,
and the next successful cfg would silently apply it, so the last good config is restored.

Sampling clears the port's link counters ('clear', a short reply), starts the stream, collects
decoded readings, stops it, and reads the counters with 'ports' afterwards. The long 'ports'
print is kept out of the measurement because, while bytes stream in (sensors like the WT61PC
never stop), it overruns the lab port's 16-byte FIFO: one ORE and one damaged frame. A live stream does the same
until stopped, and runs queued commands (UI buttons) between polls.
"""
import math, queue, re, threading, time
from concurrent.futures import Future
from console import NautilusConsole, parse_ports
import decoders
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
    """Thread-safe client; blocking methods are meant to be run via asyncio.to_thread.

    `tgt` everywhere is the component being brought up: {"port", "apis", "stream"}.
    """

    def __init__(self, config):
        self.config = config; self.con = None; self.lock = threading.Lock(); self.sim_fault = False
        self.sim_motion_until = 0.0                   # sim only: end of a simulated motion/tilt burst
        self.sim_layout = {"uart4": "pth"}            # sim only: port -> simulated device on that port
        self.framings = {}                            # port -> framing of the sensor attached there
        self.lab_ports = [p["port"] for p in config.get("cube", {}).get("lab_ports", [])] or ["uart4"]
        self.sensor_err_re = re.compile(config.get("sensor_error_pattern", "^ERR"))
        self.good_cfg = {}  # port -> last config known to be in the hardware
        self.on_io = lambda d, line, bad=False: None
        self.pending = queue.Queue()  # (job, Future) to run inside a running stream
        self.streaming = False
        self.live_sink = None         # on_reading of the live stream while one runs

    @property
    def transport_type(self): return self.config["transport"]["type"]

    # ---- connection ----
    def _console(self):
        if self.con is None:
            if self.transport_type == "sim":
                from sim import SimNautilus
                t = SimNautilus(fault=lambda: self.sim_fault, layout=lambda: self.sim_layout,
                                motion=lambda: time.time() < self.sim_motion_until)
            else:
                t = SerialTransport(self.config["transport"])
            self.con = NautilusConsole(t, self._async)
            for port, framing in self.framings.items(): self.con.set_decoder(port, decoders.make(framing))
            try:
                for attempt in range(3):
                    try:
                        self.con.quiet(idle=0.1, max_wait=1.0)
                        for cmd in self.config.get("init_commands", []):
                            for port in (self.lab_ports if "{port}" in cmd else [None]): self._command(fill(cmd, {"port": port}))
                        self._ports()
                        break
                    except TimeoutError:
                        # Right after the port opens the console may miss the first command (or hold a
                        # half-typed line from another terminal): end that line and try again.
                        if attempt == 2: raise
                        self.on_io("rx", "(no answer from the console yet, retrying)", True)
                        self.con.t.write(b"\r"); self.con.pump(0.3)
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

    def attach(self, port, sensor):
        """A sensor was assigned to `port`: decode its bytes with the sensor's framing (and, on the sim bench, plug in its device)."""
        with self.lock:
            self.framings[port] = sensor.get("framing")
            if sensor.get("sim_device"): self.sim_layout[port] = sensor["sim_device"]
            if self.con: self.con.set_decoder(port, decoders.make(self.framings[port]))

    def _async(self, kind, port, text):
        if kind == "sensor":
            self.on_io("sensor", f"{port}: {text}", bool(self.sensor_err_re.search(text)))
        elif kind == "bad":
            self.on_io("sensor", f"{port}: {text}", True)
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
    def call(self, name, args=None, on_io=None, tgt=None):
        with self.lock:
            self.on_io = on_io or (lambda d, line, bad=False: None)
            try:
                self._console()
                return self._call(name, dict(args or {}), self._tgt(tgt))
            except Exception:
                self._reset(); raise

    def enqueue(self, name, args, tgt):
        """Run an API from another thread while a stream holds the board; returns a Future."""
        tgt, args = self._tgt(tgt), dict(args or {})
        return self.enqueue_job(lambda: self._call(name, args, tgt))

    def enqueue_job(self, job):
        fut = Future(); self.pending.put((job, fut)); return fut

    def _tgt(self, tgt):
        tgt = dict(tgt or {})
        tgt.setdefault("port", self.lab_ports[0]); tgt.setdefault("apis", self.config["apis"]); tgt.setdefault("stream", {})
        return tgt

    @staticmethod
    def _key(v):
        return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)

    def _check_args(self, api, args):
        for p, spec in api.get("params", {}).items():
            if p not in args: return f"missing arg '{p}'"
            v = args[p]
            if "enum" in spec:
                def same(e):
                    if str(e).lower() == str(v).strip().lower(): return True
                    try: return float(e) == float(v)
                    except (TypeError, ValueError): return False
                match = next((e for e in spec["enum"] if same(e)), None)
                if match is None: return f"{p} must be one of {spec['enum']}"
                args[p] = match
            elif spec.get("type") == "integer":
                try: args[p] = int(float(v)); assert args[p] == float(v)
                except (ValueError, TypeError, AssertionError): return f"{p} must be an integer"
            elif spec.get("type") == "number":
                try: args[p] = float(v)
                except (ValueError, TypeError): return f"{p} must be a number"
            elif not TEXT_ARG.fullmatch(str(v)) or len(str(v)) > spec.get("max_length", 60):
                return f"{p} must be printable ASCII with single spaces, at most {spec.get('max_length', 60)} characters"
        return None

    def _step(self, step, tgt):
        if isinstance(step, dict):
            self.con.pump(float(step.get("sleep", 0))); return {"ok": True}
        return self._call(step, {}, tgt)

    def _call(self, name, args, tgt):
        if name == "stream_probe": return self._stream_probe(tgt, args)
        apis, port = tgt["apis"], tgt["port"]
        api = apis.get(name)
        if api is None: return {"ok": False, "error": f"unknown board API '{name}'"}
        if err := self._check_args(api, args): return {"ok": False, "error": err}
        values = {"port": port, **args}
        cmd_values = {**values, **{p: spec["map"][self._key(args[p])] for p, spec in api.get("params", {}).items() if "map" in spec}}
        for step in api.get("pre", []):
            r = self._step(step, tgt)
            if not r["ok"]: return {"ok": False, "error": f"{step} failed before {name}: {r.get('error')}", "step": r}
        cmd = fill(api["command"], cmd_values)
        expect = re.compile(fill(api.get("expect", "."), values, escape=True))
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
                self.con.decoder(port).reset()  # bytes from the old framing would corrupt the next frame
                for k, v in (t.split("=", 1) for t in cmd.split()[2:]):
                    self.good_cfg.setdefault(port, {})[k] = int(v) if k in ("baud", "data") else v
            if api.get("parse") == "ports":
                result["ports"] = parse_ports(r.lines); del result["reply"]
                self._ports_to_good(result["ports"])
            if "sensor_expect" not in api and not api.get("sensor_expect_reading"): break
            ok_re = re.compile(fill(api["sensor_expect"], values, escape=True)) if "sensor_expect" in api else None
            kind, line = self._wait_reply(sent_at, port, ok_re, api.get("timeout_seconds", self.config.get("sensor_timeout_seconds", 2.5)))
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
        if result["ok"]:
            for step in api.get("post", []):
                r = self._step(step, tgt)
                if not r["ok"]:
                    result.update(ok=False, error=f"{step} failed after {name}: {r.get('error')}", step=r); break
        if result["ok"] and api.get("verify_stream"):
            probe = self._stream_probe(tgt, {"seconds": api["verify_stream"]})
            result["stream"] = probe
            if not probe["ok"]: result.update(ok=False, error="the command was sent but no valid data arrived from the sensor afterwards")
        if not result["ok"]:
            other = self.con.lines_since(mark, port)
            frames = len(self.con.readings_since(mark, port))
            if other: result["sensor_lines"] = other[-8:]
            if frames: result["data_frames_seen"] = frames
            if notes := self.con.notices_since(mark, port): result["notices"] = notes[-3:]
        return result

    def _wait_reply(self, mark, port, ok_re, timeout):
        """Wait for the sensor's answer on `port`: a line matching ok_re (or, with ok_re None, any reading),
        or an error line. Returns (kind, text) with kind in {"ok", "err", None}."""
        end, seen = time.time() + timeout, mark
        while True:
            for s, p, text in list(self.con.sensor_lines):
                if s <= seen or p != port: continue
                seen = max(seen, s)
                if ok_re and ok_re.search(text): return "ok", text
                if self.sensor_err_re.search(text): return "err", text
            if ok_re is None and (r := self.con.readings_since(mark, port)):
                return "ok", ",".join(f"{k}={v}" for k, v in r[0].items())
            if time.time() >= end: return None, None
            self.con.pump()

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
    # One reader of the port at a time: either a short measurement (sample) or a live stream that runs
    # until stopped. While a live stream runs, API calls and measurements are queued as jobs and run
    # inside it (enqueue / sample), so the stream never has to stop for them.
    def _run_pending(self):
        ran = False
        while True:
            try: job, fut = self.pending.get_nowait()
            except queue.Empty: return ran
            ran = True
            try: fut.set_result(job())
            except Exception as e: fut.set_result({"ok": False, "error": f"{type(e).__name__}: {e}"})

    def _collect(self, tgt, seconds=None, stop=None, on_reading=None):
        """Pump the console and collect decoded readings on the port until `seconds` pass or `stop` is set.
        Readings also go to the live stream's sink, if one is running."""
        port, seq_field = tgt["port"], tgt["stream"].get("sequence_field")
        self.con.pump(0.12)  # take in what is already buffered (and any frame damaged by a console print just before)
        dec = self.con.decoder(port); st0 = dict(dec.stats)
        readings, other, seqs = [], [], []
        t0 = time.time(); end = t0 + seconds if seconds else math.inf
        mark = self.con.mark()
        while time.time() < end and not (stop and stop.is_set()):
            self.con.pump(0.05)
            for values in self.con.readings_since(mark, port):
                values = dict(values)
                if seq_field in values: seqs.append(int(values.pop(seq_field)))
                readings.append(values)
                if on_reading: on_reading(values)
                if self.live_sink and self.live_sink is not on_reading: self.live_sink(values)
            other += self.con.lines_since(mark, port)
            mark = self.con.mark()
            if self._run_pending(): mark = self.con.mark()  # a job reads (and forwards) its own data
        elapsed = max(time.time() - t0, 1e-6)
        stats = {k: v - st0.get(k, 0) for k, v in dec.stats.items() if k not in ("readings", "lines")} if dec is self.con.decoder(port) else {}
        return readings, other, seqs, elapsed, stats

    def _summarise(self, readings, other, seqs, elapsed, stats):
        result = {"samples": len(readings), "seconds": round(elapsed, 2), "rate_hz": round(len(readings) / elapsed, 2)}
        if stats: result["decoder"] = stats
        if seqs: result["seq_gaps"] = sum(max(0, b - a - 1) for a, b in zip(seqs, seqs[1:]))
        if other: result["garbled_lines"] = len(other); result["garbled_examples"] = other[:3]
        return result

    def _stream_probe(self, tgt, args):
        """Board helper: listen for valid readings (start/stop the stream around it if the sensor needs that,
        unless a live stream already keeps it running)."""
        try: seconds = min(10.0, max(0.5, float(args.get("seconds", tgt["stream"].get("probe_seconds", 2)))))
        except (TypeError, ValueError): seconds = 2.0
        live = self.live_sink is not None
        start, stop = (None, None) if live else (tgt["stream"].get("start_api"), tgt["stream"].get("stop_api"))
        mark = self.con.mark()
        if start and not (r := self._call(start, {}, tgt))["ok"]:
            return {"ok": False, "error": "could not start the stream", "start": r}
        readings, other, seqs, elapsed, stats = self._collect(tgt, seconds)
        if stop: self._call(stop, {}, tgt)
        result = {"ok": bool(readings), **self._summarise(readings, other, seqs, elapsed, stats)}
        if readings:
            result["last"] = readings[-1]
            damaged = stats.get("checksum_errors", 0) + len(other)
            if damaged > 0.05 * len(readings):
                result["warning"] = (f"{damaged} damaged frames for {len(readings)} good ones: the stream is arriving, but the link or "
                                     "the probe cube's monitor is losing bytes (rate too high for the baud?)")
        else:
            result["error"] = f"no valid data frames from the sensor in {seconds:g} s"
            if stats.get("junk_bytes") or stats.get("checksum_errors") or other:
                result["hint"] = "bytes arrived but none decoded: likely a baud/framing mismatch between the sensor and the port"
        if notes := self.con.notices_since(mark, tgt["port"]): result["notices"] = notes[-3:]
        return result

    def _measure(self, tgt, seconds, on_reading, link):
        """A measurement window: counters cleared before, read after (the long 'ports' print stays outside it)."""
        port = tgt["port"]
        if link: self._command(f"clear {port}", re.compile(rf"^{re.escape(port)} counters cleared$"))
        readings, other, seqs, elapsed, stats = self._collect(tgt, seconds, on_reading=on_reading)
        after = self._ports().get(port) if link else None
        result = {"ok": True, **self._summarise(readings, other, seqs, elapsed, stats), "fields": field_stats(readings)}
        if readings: result["last"] = readings[-1]
        if after:
            result["link"] = {k: after.get(k, 0) for k in LINK_KEYS}
            result["port_config"] = {k: after[k] for k in ("open", "baud", "data", "parity", "stop", "actual_baud")}
        return result

    def sample(self, seconds, tgt=None, on_io=None, on_reading=None):
        """Measure for `seconds`. Runs inside the live stream if one is running, otherwise starts and stops the stream itself."""
        tgt = self._tgt(tgt); link = self.config.get("link_counters", True)
        if self.live_sink is not None:
            fut = self.enqueue_job(lambda: self._measure(tgt, seconds, on_reading, link))
            return fut.result(timeout=seconds + 60)
        s = tgt["stream"]
        with self.lock:
            self.on_io = on_io or (lambda d, line, bad=False: None)
            self.streaming = True  # UI actions are queued and run between polls
            try:
                self._console()
                started = self._call(s["start_api"], {}, tgt) if s.get("start_api") else {"ok": True}
                result = {"ok": False, "error": "could not start streaming", "start": started}
                if started["ok"]:
                    result = self._measure(tgt, seconds, on_reading, link)
                    if s.get("stop_api"): self._call(s["stop_api"], {}, tgt)
            except Exception:
                self._reset(); raise
            finally:
                self.streaming = False
                self._drain_pending()
        return result

    def stream(self, tgt, stop, on_io=None, on_reading=None, max_seconds=3600):
        """Live stream: deliver readings to `on_reading` until `stop` (threading.Event) is set.
        API calls and measurements queued meanwhile (enqueue / sample) run inside it."""
        tgt = self._tgt(tgt); s = tgt["stream"]
        count, last = [0], [None]

        def counted(values):  # every reading reaches the sink, including those taken inside queued measurements
            count[0] += 1; last[0] = values
            if on_reading: on_reading(values)
        t0 = time.time()
        with self.lock:
            self.on_io = on_io or (lambda d, line, bad=False: None)
            self.streaming, self.live_sink = True, counted
            try:
                self._console()
                started = self._call(s["start_api"], {}, tgt) if s.get("start_api") else {"ok": True}
                collected = ([], [], [], 1e-6, {})
                if started["ok"]:
                    collected = self._collect(tgt, max_seconds, stop)
                    if s.get("stop_api"): self._call(s["stop_api"], {}, tgt)
            except Exception:
                self._reset(); raise
            finally:
                self.streaming, self.live_sink = False, None
                self._drain_pending()
        elapsed = max(time.time() - t0, 1e-6)
        result = {"ok": started["ok"], "samples": count[0], "seconds": round(elapsed, 2), "rate_hz": round(count[0] / elapsed, 2)}
        if last[0]: result["last"] = last[0]
        if not started["ok"]: result.update(error="could not start streaming", start=started)
        return result

    def _drain_pending(self):
        while True:
            try: _, fut = self.pending.get_nowait()
            except queue.Empty: return
            fut.set_result({"ok": False, "error": "the live stream ended before the command ran"})
