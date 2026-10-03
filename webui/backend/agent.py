"""Phase-driven LLM loop: Connectivity -> Configuration -> Testing, one component at a time.

Each phase runs the same tool-calling loop with its own prompt and toolset. The LLM
can only leave a phase through complete_phase, whose gate is enforced here in code.
"""
import asyncio, json, os, re, time, traceback
from dataclasses import dataclass, field
from pathlib import Path
from openai import AsyncOpenAI
from offline_llm import OfflineLLM

HERE = Path(__file__).parent
PHASES = ["connectivity", "configuration", "testing"]
COMMON_PROMPT = (HERE / "prompts" / "common.txt").read_text()
PHASE_PROMPTS = {p: (HERE / "prompts" / f"{p}.txt").read_text() for p in PHASES}
SENSORS = [json.loads(p.read_text()) for p in sorted((HERE / "sensors").glob("*.json"))]
MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini-2026-03-17")
RUNTIME = {"api_key": os.environ.get("OPENAI_API_KEY")}  # a key entered in the UI lives here, in memory only


def norm(s): return re.sub(r"[^a-z0-9]", "", str(s).lower())


def fn(name, description, props=None, required=None):
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": props or {}, "required": required or []}}


STATIC_TOOLS = {
    "set_component_name": fn("set_component_name", "Name the component being added.",
                             {"name": {"type": "string"}}, ["name"]),
    "lookup_sensor": fn("lookup_sensor", "Look up a sensor by model number. Returns its datasheet summary and the wiring plan to the probe cube.",
                        {"model": {"type": "string"}}, ["model"]),
    "show_schematic": fn("show_schematic", "Show the user the wiring schematic between the sensor and the probe cube."),
    "request_wiring_step": fn("request_wiring_step", "Show the user the instruction for connecting one sensor pin.",
                              {"sensor_pin": {"type": "string"}}, ["sensor_pin"]),
    "confirm_wiring_step": fn("confirm_wiring_step", "Mark a sensor pin as connected after the user confirmed it.",
                              {"sensor_pin": {"type": "string"}}, ["sensor_pin"]),
    "record_configuration": fn("record_configuration", "Record and display the configuration that was applied to the board.",
                               {"settings": {"type": "object", "description": "Setting name -> applied value, e.g. baud, parity, stop_bits, ack."}},
                               ["settings"]),
    "run_test": fn("run_test", "Show the user a test instruction, count down, then sample live sensor data. Returns per-field statistics.",
                   {"name": {"type": "string", "description": "Test name from the test plan"},
                    "instruction": {"type": "string"},
                    "seconds": {"type": "number", "description": "Sampling duration, 1-15"}},
                   ["name", "instruction", "seconds"]),
    "record_test_result": fn("record_test_result", "Record whether a test passed.",
                             {"name": {"type": "string"}, "passed": {"type": "boolean"}, "summary": {"type": "string"}},
                             ["name", "passed", "summary"]),
    "complete_phase": fn("complete_phase", "Finish the current phase. Returns ok=false with what is still missing if the phase goal is not met.",
                         {"summary": {"type": "string"}}),
}
PHASE_TOOLS = {
    "connectivity": ["set_component_name", "lookup_sensor", "show_schematic", "request_wiring_step", "confirm_wiring_step", "complete_phase"],
    "configuration": ["call_board_api", "record_configuration", "complete_phase"],
    "testing": ["run_test", "record_test_result", "call_board_api", "complete_phase"],
}


@dataclass
class Component:
    index: int
    name: str = ""
    phase: str = "connectivity"
    sensor: dict | None = None
    wiring: list = field(default_factory=list)
    confirmed: set = field(default_factory=set)
    config: dict = field(default_factory=dict)
    applied: list = field(default_factory=list)
    tests: dict = field(default_factory=dict)
    test_ids: dict = field(default_factory=dict)

    @property
    def id(self): return f"{self.index:03d}"


class Session:
    def __init__(self, ws, config, board):
        self.config, self.board = config, board
        self.loop = asyncio.get_running_loop()
        self.out, self.inbox = asyncio.Queue(), asyncio.Queue()
        self.components, self.task = [], None
        self.writer = asyncio.create_task(self._write(ws))
        self.tools = {**STATIC_TOOLS, "call_board_api": fn(
            "call_board_api", "Call one of the probe cube's board APIs.",
            {"api": {"type": "string", "enum": list(config["apis"])},
             "args": {"type": "object", "description": "Arguments for the API's params; {} if it has none."}}, ["api"])}
        self.emit(None, "hello", board=config["board"], transport=board.transport_type)
        self.use_mode("offline" if os.environ.get("PROBE_LLM") == "offline" or not RUNTIME["api_key"] else "online")

    def use_mode(self, mode):
        if mode == "online":
            self.llm, self.model = AsyncOpenAI(api_key=RUNTIME["api_key"]), MODEL
        else:
            self.llm, self.model = OfflineLLM(self), "offline"
        self.emit(None, "mode", mode=mode, model=MODEL, has_key=bool(RUNTIME["api_key"]))

    async def set_mode(self, mode, api_key=None):
        if self.task and not self.task.done():
            return self.emit(None, "mode_error", text="Switch modes between components, not while one is in progress.")
        if mode == "online":
            key = (api_key or "").strip() or RUNTIME["api_key"]
            if not key:
                return self.emit(None, "mode_error", text="Enter an OpenAI API key to use online mode.")
            try:
                await AsyncOpenAI(api_key=key).models.retrieve(MODEL)
            except Exception as e:
                return self.emit(None, "mode_error", text=f"Could not use {MODEL} with that key: {e}")
            RUNTIME["api_key"] = key
        self.use_mode(mode)

    # ---- outbound events (single writer keeps them ordered) ----
    async def _write(self, ws):
        while True: await ws.send_json(await self.out.get())

    def emit(self, c, type_, **kw):
        msg = {"type": type_, **({"component_id": c.id, "phase": c.phase} if c else {}), **kw}
        self.out.put_nowait(msg)

    def emit_threadsafe(self, c, type_, **kw):
        self.loop.call_soon_threadsafe(lambda: self.emit(c, type_, **kw))

    def io_logger(self, c):
        return lambda d, line, bad=False: self.emit_threadsafe(c, "board_io", direction=d, line=line, error=bad)

    # ---- inbound ----
    async def handle(self, msg):
        kind = msg.get("type")
        if kind == "add_component":
            if self.task and not self.task.done():
                return self.emit(None, "error", text="Finish the current component first.")
            c = Component(index=len(self.components) + 1); self.components.append(c)
            self.emit(c, "component", index=c.index, name=c.name or "New Sensor")
            self.task = asyncio.create_task(self.run_component(c))
        elif kind == "user_message" and str(msg.get("text", "")).strip():
            await self.inbox.put(msg["text"].strip())
        elif kind == "set_mode":
            await self.set_mode(msg.get("mode"), msg.get("api_key"))
        elif kind == "sim_fault":
            self.board.sim_fault = bool(msg.get("on"))

    async def next_user_text(self, c):
        text = await self.inbox.get()
        self.emit(c, "message", role="user", text=text)
        return text

    def close(self):
        for t in (self.task, self.writer):
            if t: t.cancel()

    # ---- phase loop ----
    async def run_component(self, c):
        try:
            while not self.inbox.empty(): self.inbox.get_nowait()
            for phase in PHASES:
                c.phase = phase
                self.emit(c, "phase", status="active")
                await self.run_phase(c)
                self.emit(c, "phase", status="complete")
            self.emit(c, "done")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            traceback.print_exc()
            self.emit(c, "error", text=f"Session error: {e}")

    def instructions(self, c):
        sensor = {k: v for k, v in c.sensor.items() if k != "test_plan"} if c.sensor else None
        apis = {name: {"description": a["description"], "params": a.get("params", {})} for name, a in self.config["apis"].items()}
        facts = dict(board=self.config["board"], notes="\n".join(f"- {n}" for n in self.config.get("notes", [])) or "(none)", phase=c.phase.capitalize(), index=c.id, name=c.name or "(not named yet)",
                     sensor=json.dumps(sensor) if sensor else "(not identified yet)",
                     ports=json.dumps(self.config["probe_cube_ports"]), apis=json.dumps(apis, indent=1),
                     config=json.dumps(c.config) if c.config else "(none)",
                     test_plan=json.dumps((c.sensor or {}).get("test_plan", []), indent=1), tests=json.dumps(c.tests))
        return COMMON_PROMPT.format(**facts) + "\n\n" + PHASE_PROMPTS[c.phase].format(**facts)

    async def run_phase(self, c):
        tools = [self.tools[n] for n in PHASE_TOOLS[c.phase]]
        pending, prev = [{"role": "user", "content": f"Start the {c.phase} phase."}], None
        while True:
            self.emit(c, "thinking", on=True)
            try:
                resp = await self.llm.responses.create(model=self.model, instructions=self.instructions(c),
                                                       input=pending, tools=tools, previous_response_id=prev)
            except Exception as e:
                self.emit(c, "thinking", on=False)
                self.emit(c, "error", text=f"LLM call failed ({e}). Send any message to retry.")
                pending.append({"role": "user", "content": await self.next_user_text(c)})
                continue
            self.emit(c, "thinking", on=False)
            prev, pending, finished = resp.id, [], False
            for item in resp.output:
                if item.type == "message":
                    text = "".join(getattr(part, "text", "") for part in item.content).strip()
                    if text: self.emit(c, "message", role="probe", text=text)
                elif item.type == "function_call":
                    result = await self.call_tool(c, item.name, item.arguments)
                    pending.append({"type": "function_call_output", "call_id": item.call_id, "output": json.dumps(result)})
                    finished |= item.name == "complete_phase" and result.get("ok", False)
            if finished: return
            if not pending:
                pending = [{"role": "user", "content": await self.next_user_text(c)}]

    async def call_tool(self, c, name, arguments):
        if name not in PHASE_TOOLS[c.phase]:
            return {"ok": False, "error": f"tool {name} is not available in the {c.phase} phase"}
        try:
            return await getattr(self, f"tool_{name}")(c, **json.loads(arguments or "{}"))
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ---- connectivity tools ----
    async def tool_set_component_name(self, c, name):
        c.name = name.strip()[:40] or c.name
        self.emit(c, "component", index=c.index, name=c.name)
        return {"ok": True, "name": c.name}

    async def tool_lookup_sensor(self, c, model):
        q = norm(model)
        match = next((s for s in SENSORS if q and any(norm(n) and (norm(n) in q or q in norm(n))
                                                       for n in [s["model"], *s.get("aliases", [])])), None)
        if not match:
            return {"found": False, "query": model}
        ports = self.config["probe_cube_ports"]
        c.sensor, c.confirmed = match, set()
        c.wiring = []
        for pin in match["pins"]:
            port = next((p for p in ports if p["accepts"] == pin["signal"]), None)
            c.wiring.append({"sensor_pin": pin["name"], "signal": pin["signal"],
                             "cube_port": port and port["label"], "mcu_pin": port and port["mcu_pin"],
                             "mcu_function": port and port["mcu_function"],
                             "instruction": f"Connect {pin['name']} from the sensor to the port labeled {port['label']} on the probe cube."
                             if port else f"No probe cube port accepts {pin['signal']}; {pin['name']} cannot be connected."})
        sensor = {k: v for k, v in match.items() if k != "test_plan"}
        return {"found": True, "sensor": sensor, "wiring_plan": c.wiring}

    async def tool_show_schematic(self, c):
        if not c.sensor: return {"ok": False, "error": "look up the sensor first"}
        self.emit(c, "schematic", sensor=c.sensor["model"], board=self.config["board"], connections=c.wiring)
        return {"ok": True}

    def _wire(self, c, sensor_pin):
        return next((w for w in c.wiring if norm(w["sensor_pin"]) == norm(sensor_pin)), None)

    async def tool_request_wiring_step(self, c, sensor_pin):
        w = self._wire(c, sensor_pin)
        if not w: return {"ok": False, "error": f"unknown pin {sensor_pin}; pins are {[x['sensor_pin'] for x in c.wiring]}"}
        if not w["cube_port"]: return {"ok": False, "error": w["instruction"]}
        self.emit(c, "wiring_step", pin=w["sensor_pin"], port=w["cube_port"], instruction=w["instruction"])
        return {"ok": True, "shown": w["instruction"], "next": "Wait for the user to confirm this connection."}

    async def tool_confirm_wiring_step(self, c, sensor_pin):
        w = self._wire(c, sensor_pin)
        if not w: return {"ok": False, "error": f"unknown pin {sensor_pin}"}
        c.confirmed.add(w["sensor_pin"])
        self.emit(c, "wiring_confirmed", pin=w["sensor_pin"])
        return {"ok": True, "remaining": [x["sensor_pin"] for x in c.wiring if x["sensor_pin"] not in c.confirmed]}

    # ---- configuration tools ----
    async def tool_call_board_api(self, c, api, args=None):
        result = await asyncio.to_thread(self.board.call, api, args or {}, self.io_logger(c))
        if result["ok"]: c.applied.append({"api": api, "args": args or {}})
        return result

    async def tool_record_configuration(self, c, settings):
        c.config = settings
        self.emit(c, "config", settings=settings)
        return {"ok": True}

    # ---- testing tools ----
    async def tool_run_test(self, c, name, instruction, seconds):
        seconds = max(1.0, min(15.0, float(seconds)))
        lead = self.config["sampling"].get("lead_seconds", 3)
        test_id = f"{c.id}-{name}-{len(c.test_ids)}"; c.test_ids[name] = test_id
        self.emit(c, "test_step", test_id=test_id, name=name, instruction=instruction, lead=lead, seconds=seconds)
        await asyncio.sleep(lead)
        self.emit(c, "test_status", test_id=test_id, status="sampling")
        latest, last_sent = {}, [0.0]

        def on_reading(values):
            latest.update(values)
            if time.time() - last_sent[0] >= 0.1:
                last_sent[0] = time.time(); self.emit_threadsafe(c, "reading", test_id=test_id, values=dict(latest))

        result = await asyncio.to_thread(self.board.sample, seconds, self.io_logger(c), on_reading)
        self.emit(c, "test_status", test_id=test_id, status="done", samples=result.get("samples", 0))
        return result

    async def tool_record_test_result(self, c, name, passed, summary):
        c.tests[name] = {"passed": bool(passed), "summary": summary}
        self.emit(c, "test_result", test_id=c.test_ids.get(name), name=name, passed=bool(passed), summary=summary)
        return {"ok": True}

    # ---- gate ----
    async def tool_complete_phase(self, c, summary=""):
        missing = []
        if c.phase == "connectivity":
            if not c.name: missing.append("component name (set_component_name)")
            if not c.sensor: missing.append("sensor model (lookup_sensor)")
            missing += [f"wiring for {w['sensor_pin']}" for w in c.wiring if w["sensor_pin"] not in c.confirmed]
        elif c.phase == "configuration":
            if not c.applied: missing.append("at least one successful call_board_api")
            if not c.config: missing.append("record_configuration")
        elif c.phase == "testing":
            missing += [f"passing result for test '{t['name']}'" for t in (c.sensor or {}).get("test_plan", [])
                        if not c.tests.get(t["name"], {}).get("passed")]
        return {"ok": not missing, "missing": missing}
