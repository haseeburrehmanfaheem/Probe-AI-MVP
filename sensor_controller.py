import json,re,time,serial
FREQUENCY_RE=re.compile(r"FREQ,([0-9]+(?:\.[0-9]+)?)",re.I)
class WT61PCController:
    def __init__(self,config_path="config.json"):
        self.config=json.load(open(config_path)); self.serial=None
    @property
    def allowed_rates(self): return set(self.config["allowed_rates_hz"])
    def connect(self):
        if self.serial and self.serial.is_open: return
        self.serial=serial.Serial(port=self.config["port"],baudrate=self.config["baud_rate"],timeout=self.config["timeout_seconds"]); time.sleep(2); self.serial.reset_input_buffer()
    def close(self):
        if self.serial and self.serial.is_open: self.serial.close()
    def _write_line(self,s): self.connect(); self.serial.write((s+"\n").encode()); self.serial.flush()
    def _read_line(self):
        raw=self.serial.readline(); return None if not raw else raw.decode("utf-8",errors="replace").strip()
    def measure_frequency(self):
        self._write_line("MEASURE"); deadline=time.time()+self.config["measurement_timeout_seconds"]; started=False
        while time.time()<deadline:
            line=self._read_line()
            if not line: continue
            print(f"[Arduino] {line}")
            if line=="ACK,MEASURE": started=True; continue
            m=FREQUENCY_RE.search(line)
            if m and started: return float(m.group(1))
        raise TimeoutError("Timed out waiting for FREQ,<hz> from Arduino.")
    def set_output_rate(self,hz):
        if hz not in self.allowed_rates: raise ValueError(f"Unsupported frequency {hz}")
        self._write_line(f"SET_RATE,{hz}"); deadline=time.time()+3
        while time.time()<deadline:
            line=self._read_line()
            if not line: continue
            print(f"[Arduino] {line}")
            if line==f"ACK,SET_RATE,{hz}": return
            if line.startswith("ERR,"): raise RuntimeError(line)
        raise TimeoutError(f"No ACK for SET_RATE,{hz}")
    def verify_frequency(self,target_hz,measured_hz):
        tol=max(0.25,target_hz*self.config["verification_tolerance_fraction"]); return abs(target_hz-measured_hz)<=tol
