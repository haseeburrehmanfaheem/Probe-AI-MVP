"""Phase-driven LLM loop: Connectivity -> Configuration -> Testing, one component at a time.

Each phase runs the same tool-calling loop with its own prompt and toolset. The LLM
can only leave a phase through complete_phase, whose gate is enforced here in code.

Each component is given its own probe cube lab port (from the board definition in
boards/*.json) when its sensor is identified; every board API then runs on that port,
with the board's generic port APIs plus the sensor's own APIs from sensors/<id>/sensor.json.
"""
import asyncio, json, os, re, threading, time, traceback
from dataclasses import dataclass, field
from pathlib import Path
from openai import AsyncOpenAI
from offline_llm import OfflineLLM
import checks, projects, sensors as sensor_lib
from llm_trace import Tracer

HERE = Path(__file__).parent
PHASES = ["connectivity", "configuration", "testing"]
COMMON_PROMPT = (HERE / "prompts" / "common.txt").read_text()
PHASE_PROMPTS = {p: (HERE / "prompts" / f"{p}.txt").read_text() for p in PHASES}
SENSORS = []  # filled by load_sensors() at server start (offline_llm imports this list)
MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini-2026-03-17")
RUNTIME = {"api_key": os.environ.get("OPENAI_API_KEY")}  # a key entered in the UI lives here, in memory only


def load_sensors(config): SENSORS[:] = sensor_lib.load(config["apis"])


def norm(s): return re.sub(r"[^a-z0-9]", "", str(s).lower())


def fn(name, description, props=None, required=None):
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": props or {}, "required": required or []}}


STATIC_TOOLS = {
    "set_component_name": fn("set_component_name", "Name the component being added.",
                             {"name": {"type": "string"}}, ["name"]),
    "lookup_sensor": fn("lookup_sensor", "Look up a sensor by model number. Returns its datasheet summary, the probe cube port it is assigned and the wiring plan.",
                        {"model": {"type": "string"}}, ["model"]),
    "show_schematic": fn("show_schematic", "Show the user the wiring schematic between the sensor and the probe cube."),
    "request_wiring_step": fn("request_wiring_step", "Show the user the instruction for connecting one sensor pin.",
                              {"sensor_pin": {"type": "string"}}, ["sensor_pin"]),
    "confirm_wiring_step": fn("confirm_wiring_step", "Mark a sensor pin as connected after the user confirmed it.",
                              {"sensor_pin": {"type": "string"}}, ["sensor_pin"]),
    "record_configuration": fn("record_configuration", "Record and display the configuration that was applied to the board.",
                               {"settings": {"type": "object", "description": "Setting name -> applied value, e.g. baud, parity, stop_bits, rate_hz."}},
                               ["settings"]),
    "start_live_view": fn("start_live_view", "Open the live monitor card: streams the sensor's data into its live views (charts, and for "
                          "motion sensors a 3D orientation model). It keeps running through later board calls and tests, until the user "
                          "presses Stop or the next sensor is added. Calling it again while it runs shows a fresh card further down."),
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
    "configuration": ["call_board_api", "record_configuration", "start_live_view", "complete_phase"],
    "testing": ["run_test", "record_test_result", "call_board_api", "start_live_view", "complete_phase"],
}


@dataclass
class Component:
    index: int
    name: str = ""
    phase: str = "connectivity"
    sensor: dict | None = None
    port: str | None = None
    wiring: list = field(default_factory=list)
    confirmed: set = field(default_factory=set)
    config: dict = field(default_factory=dict)
    applied: list = field(default_factory=list)
    tests: dict = field(default_factory=dict)
    test_ids: dict = field(default_factory=dict)
    test_checks: dict = field(default_factory=dict)  # test name -> checks of its latest run

    @property
    def id(self): return f"{self.index:03d}"


class Session:
    def __init__(self, ws, config, board):
        self.config, self.board = config, board
        self.cube = config.get("cube", {})
        self.loop = asyncio.get_running_loop()
        self.out, self.inbox = asyncio.Queue(), asyncio.Queue()
        self.components, self.task, self.live = [], None, None
        self.project, self.next_index, self.flush_handle = None, 0, None
        self.trace = Tracer()  # LLM requests/responses/tool calls -> data/traces/<project>.jsonl
        self.writer = asyncio.create_task(self._write(ws))
        self.emit(None, "hello", board=config["board"], transport=board.transport_type)
        self.use_mode("offline" if os.environ.get("PROBE_LLM") == "offline" or not RUNTIME["api_key"] else "online")
        self.send_projects()

    def use_mode(self, mode):
        if mode == "online":
            self.llm, self.model = AsyncOpenAI(api_key=RUNTIME["api_key"]), MODEL
        else:
            self.llm, self.model = OfflineLLM(self), "offline"
        self.emit(None, "mode", mode=mode, model=MODEL, has_key=bool(RUNTIME["api_key"]))
        self.trace.write("mode", mode=mode, model=self.model, transport=self.board.transport_type)

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
        msg = {"type": type_, "ts": round(time.time(), 3), **({"component_id": c.id, "phase": c.phase} if c else {}), **kw}
        self.out.put_nowait(msg)
        # live-monitor readings are not saved (only the final snapshot in live_status), so project files stay small
        if self.project and c and type_ in projects.LOGGED and not kw.get("live"): self._log(msg)

    # ---- projects ----
    def _log(self, msg):
        self.project.record(msg)
        if msg["type"] in ("component", "phase", "done"): self.flush(); self.send_projects()
        elif not self.flush_handle: self.flush_handle = self.loop.call_later(1.0, self.flush)

    def flush(self):
        if self.flush_handle: self.flush_handle.cancel(); self.flush_handle = None
        if self.project: self.project.save()

    def send_projects(self):
        self.emit(None, "projects", projects=projects.list_projects(), current=self.project and self.project.id)

    def busy(self):
        if self.task and not self.task.done():
            self.emit(None, "error", text="Finish the current component first."); return True
        return False

    def open_project(self, project):
        self.flush()
        self.project, self.components = project, []
        self.trace.open(project.id)
        self.trace.write("project", id=project.id, name=project.data["name"], board=self.config["board"],
                         transport=self.board.transport_type, mode=self.model)
        self.next_index = len(project.data["components"])
        self.emit(None, "project_opened", project=project.summary())
        for ev in project.data["events"]: self.out.put_nowait({**ev, "replay": True})
        self.emit(None, "replay_done", interrupted=[cid for cid, c in project.data["components"].items() if not c["done"]])
        self.send_projects()

    async def handle_project(self, kind, msg):
        if kind == "new_project":
            if self.busy(): return
            name = str(msg.get("name") or "").strip()[:60] or time.strftime("Bring-up %b %d, %H:%M")
            self.open_project(projects.Project.create(name))
        elif kind == "open_project":
            if self.busy() or (self.project and self.project.id == msg.get("id")): return
            try: self.open_project(projects.Project.load(msg.get("id")))
            except (OSError, ValueError): self.emit(None, "error", text="That project could not be opened.")
        elif kind == "pin_project":
            if self.project and self.project.id == msg.get("id"):
                self.project.data["pinned"] = bool(msg.get("pinned")); self.flush()
            else: projects.set_pinned(msg.get("id"), msg.get("pinned"))
            self.send_projects()
        elif kind == "delete_project":
            if self.project and self.project.id == msg.get("id"):
                if self.busy(): return
                self.project = None; self.components = []
                self.emit(None, "project_closed")
            projects.delete(msg.get("id"))
            self.send_projects()

    def emit_threadsafe(self, c, type_, **kw):
        self.loop.call_soon_threadsafe(lambda: self.emit(c, type_, **kw))

    def io_logger(self, c):
        return lambda d, line, bad=False: self.emit_threadsafe(c, "board_io", direction=d, line=line, error=bad)

    # ---- inbound ----
    async def handle(self, msg):
        kind = msg.get("type")
        if kind in ("sim_motion", "sim_fault", "live_stop", "ui_action") and self.components:
            self.trace.write("ui", self.components[-1], event=kind, **{k: v for k, v in msg.items() if k != "type"})
        if kind in ("new_project", "open_project", "pin_project", "delete_project"):
            await self.handle_project(kind, msg)
        elif kind == "add_component":
            if self.busy(): return
            await self.stop_live()  # the previous sensor's monitor ends when the next one starts
            if not self.project: self.open_project(projects.Project.create(time.strftime("Bring-up %b %d, %H:%M")))
            self.next_index += 1
            c = Component(index=self.next_index); self.components.append(c)
            self.emit(c, "component", index=c.index, name=c.name or "New Sensor")
            self.task = asyncio.create_task(self.run_component(c))
        elif kind == "user_message" and str(msg.get("text", "")).strip():
            await self.inbox.put(msg["text"].strip())
        elif kind == "set_mode":
            await self.set_mode(msg.get("mode"), msg.get("api_key"))
        elif kind == "sim_motion":
            self.board.sim_motion_until = time.time() + min(float(msg.get("seconds", 2.5)), 10)
        elif kind == "sim_fault":
            self.board.sim_fault = bool(msg.get("on"))
        elif kind == "live_stop":
            if self.live and self.live["view_id"] == msg.get("view_id"): await self.stop_live()
        elif kind == "ui_action":
            await self.ui_action(str(msg.get("action", "")))

    async def next_user_text(self, c):
        text = await self.inbox.get()
        self.emit(c, "message", role="user", text=text)
        return text

    def close(self):
        if self.live: self.live["stop"].set()
        self.flush()
        for t in (self.task, self.writer):
            if t: t.cancel()

    # ---- probe cube ports ----
    def lab_port(self, name): return next((p for p in self.cube.get("lab_ports", []) if p["port"] == name), None)

    def used_ports(self, exclude=None):
        """port -> (component id, name, model) for ports taken in this session or by finished components of the project."""
        used = {}
        if self.project:
            for cid, pc in self.project.data["components"].items():
                if pc.get("done") and pc.get("port"): used[pc["port"]] = (cid, pc.get("name", ""), pc.get("model", ""))
        for x in self.components:
            if x is not exclude and x.port: used[x.port] = (x.id, x.name, x.sensor and x.sensor["model"])
        return used

    def free_ports(self, interface=None, exclude=None):
        used = self.used_ports(exclude)
        return [p for p in self.cube.get("lab_ports", []) if p["port"] not in used and (interface is None or p["interface"] == interface)]

    @staticmethod
    def port_pins(p):
        if p["interface"] == "rs485" and p.get("bus"):
            return f"A/B on the RS485 transceiver: DI {p['tx']['pin']}, RO {p['rx']['pin']}, DE {p['de']['pin']}"
        return f"TX {p['tx']['pin']} / RX {p['rx']['pin']}"

    def cube_status(self, c):
        used, lines = self.used_ports(exclude=c), []
        for p in self.cube.get("lab_ports", []):
            state = ("assigned to this component" if p["port"] == c.port else
                     f"used by {used[p['port']][0]} \"{used[p['port']][1]}\" ({used[p['port']][2]})" if p["port"] in used else "free")
            lines.append(f"- {p['port']} ({p['interface'].upper()}, {p['level']}): {self.port_pins(p)}, header {p['tx'].get('header', '?')} -> {state}")
        lines.append("- power: " + ", ".join(f"{p['label']} ({p['header']})" for p in self.cube.get("power_pins", [])))
        return "\n".join(lines) or "(no board definition)"

    def wiring_for(self, sensor, lp):
        power = {p["signal"]: p for p in self.cube.get("power_pins", [])}
        plan = []
        for pin in sensor["pins"]:
            sig, dest = pin["signal"], None
            if sig == "UART_TX": dest = (lp["rx"]["pin"], lp["rx"]["function"], lp["rx"].get("header"))
            elif sig == "UART_RX": dest = (lp["tx"]["pin"], lp["tx"]["function"], lp["tx"].get("header"))
            elif sig in ("RS485_A", "RS485_B") and lp.get("bus"):
                dest = (lp["bus"]["a" if sig == "RS485_A" else "b"], f"{lp['port']} RS485 transceiver", None)
            elif sig in power:
                pw = power[sig]; dest = (pw["label"], None, pw.get("header"))  # a power pin needs no function name
            label, func, header = dest or (None, None, None)
            where = ", ".join(x for x in (func, f"header {header}" if header else None) if x)
            plan.append({"sensor_pin": pin["name"], "signal": sig, "cube_port": label, "mcu_pin": label, "mcu_function": func, "header": header,
                         "instruction": f"Connect {pin['name']} on the sensor to {label} on the probe cube{f' ({where})' if where else ''}." if dest
                         else f"No probe cube pin on {lp['port']} accepts {sig}; {pin['name']} cannot be connected."})
        return plan

    def apis_for(self, c): return {**self.config["apis"], **((c.sensor or {}).get("apis", {}))}

    def tgt(self, c): return {"port": c.port, "apis": self.apis_for(c), "stream": (c.sensor or {}).get("stream", {})}

    def tool_def(self, c, name):
        if name != "call_board_api": return STATIC_TOOLS[name]
        return fn("call_board_api", f"Call one of the probe cube's board APIs on this component's port ({c.port}).",
                  {"api": {"type": "string", "enum": list(self.apis_for(c))},
                   "args": {"type": "object", "description": "Arguments for the API's params; {} if it has none."}}, ["api"])

    # ---- phase loop ----
    async def run_component(self, c):
        try:
            while not self.inbox.empty(): self.inbox.get_nowait()
            for phase in PHASES:
                c.phase = phase
                self.emit(c, "phase", status="active")
                self.trace.write("phase", c, status="active", port=c.port, sensor=c.sensor and c.sensor["model"])
                await self.run_phase(c)
                self.emit(c, "phase", status="complete")
            # Bring-up is done: show the live monitor once more (it keeps streaming until Stop or the next sensor).
            await self.tool_start_live_view(c, note="Bring-up complete. The sensor keeps streaming here until you press Stop or add the next sensor.")
            nxt = [{"port": p["port"], "interface": p["interface"], "pins": self.port_pins(p)} for p in self.free_ports()]
            self.emit(c, "done", port=c.port, next_ports=nxt)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            traceback.print_exc()
            self.trace.error(c, "session", e)
            self.emit(c, "error", text=f"Session error: {e}")

    def instructions(self, c):
        sensor = sensor_lib.public(c.sensor) if c.sensor else None
        apis = {name: {"description": a["description"],
                       "params": {p: {k: v for k, v in spec.items() if k != "map"} for p, spec in a.get("params", {}).items()},
                       **({"changes_sensor": True} if a.get("changes_sensor") else {})}
                for name, a in self.apis_for(c).items()}
        facts = dict(board=self.config["board"], notes="\n".join(f"- {n}" for n in self.config.get("notes", [])) or "(none)",
                     phase=c.phase.capitalize(), index=c.id, name=c.name or "(not named yet)",
                     sensor=json.dumps(sensor) if sensor else "(not identified yet)",
                     knowledge=(c.sensor or {}).get("_knowledge") or "(identify the sensor first)",
                     port=c.port or "(assigned when the sensor is identified)", cube_status=self.cube_status(c),
                     failures="\n".join(f"- {f}" for f in self.cube.get("failure_signatures", [])) or "(none)",
                     apis=json.dumps(apis, indent=1), config=json.dumps(c.config) if c.config else "(none)",
                     verify_apis=", ".join((c.sensor or {}).get("verify_apis", ["ports"])),
                     live="running" if self.live and self.live["c"] is c else "not running",
                     test_plan=json.dumps((c.sensor or {}).get("test_plan", []), indent=1), tests=json.dumps(c.tests))
        return COMMON_PROMPT.format(**facts) + "\n\n" + PHASE_PROMPTS[c.phase].format(**facts)

    async def run_phase(self, c):
        pending, prev = [{"role": "user", "content": f"Start the {c.phase} phase."}], None
        while True:
            tools = [self.tool_def(c, n) for n in PHASE_TOOLS[c.phase]]
            self.emit(c, "thinking", on=True)
            instructions = self.instructions(c)
            t0 = self.trace.request(c, self.model, instructions, pending, tools, prev)
            try:
                resp = await self.llm.responses.create(model=self.model, instructions=instructions,
                                                       input=pending, tools=tools, previous_response_id=prev)
                self.trace.response(c, t0, resp)
            except Exception as e:
                self.trace.error(c, "llm", e)
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
        t0 = time.time()
        if name not in PHASE_TOOLS[c.phase]:
            result = {"ok": False, "error": f"tool {name} is not available in the {c.phase} phase"}
        else:
            try:
                result = await getattr(self, f"tool_{name}")(c, **json.loads(arguments or "{}"))
            except Exception as e:
                traceback.print_exc()
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        self.trace.tool(c, name, arguments, result, t0)
        return result

    # ---- connectivity tools ----
    async def tool_set_component_name(self, c, name):
        c.name = name.strip()[:40] or c.name
        self.emit(c, "component", index=c.index, name=c.name)
        return {"ok": True, "name": c.name}

    async def tool_lookup_sensor(self, c, model):
        q = norm(model)

        def score(s):  # exact name > longest name inside the query > query inside a name
            best = 0
            for n in map(norm, [s["model"], *s.get("aliases", [])]):
                if n and q == n: best = max(best, 3)
                elif n and n in q: best = max(best, 2 + len(n) / 1000)
                elif n and q in n: best = max(best, 1)
            return best
        ranked = sorted(SENSORS, key=score, reverse=True)
        match = ranked[0] if q and ranked and score(ranked[0]) > 0 else None
        if not match:
            return {"found": False, "query": model}
        free = self.free_ports(match["interface"], exclude=c)
        if not free:
            return {"found": False, "query": model, "model": match["model"],
                    "error": f"The {match['model']} needs a free {match['interface'].upper()} port on the probe cube and none is left.",
                    "probe_cube_status": self.cube_status(c)}
        lp = free[0]
        c.sensor, c.port, c.confirmed = match, lp["port"], set()
        await asyncio.to_thread(self.board.attach, c.port, match)
        c.wiring = self.wiring_for(match, lp)
        self.emit(c, "port_assigned", port=c.port, model=match["model"], interface=lp["interface"], pins=self.port_pins(lp))
        return {"found": True, "sensor": sensor_lib.public(match),
                "probe_cube_port": {"port": lp["port"], "interface": lp["interface"], "pins": self.port_pins(lp), "notes": lp.get("notes", "")},
                "wiring_plan": c.wiring}

    async def tool_show_schematic(self, c):
        if not c.sensor: return {"ok": False, "error": "look up the sensor first"}
        self.emit(c, "schematic", sensor=c.sensor["model"], board=self.config["board"], port=c.port, connections=c.wiring)
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
    async def board_call(self, c, api, args=None):
        """Run a board API: inside the live stream if one is running (it keeps streaming), otherwise directly."""
        if self.board.streaming:
            return await asyncio.wrap_future(self.board.enqueue(api, args or {}, self.tgt(c)))
        return await asyncio.to_thread(self.board.call, api, args or {}, self.io_logger(c), self.tgt(c))

    async def tool_call_board_api(self, c, api, args=None):
        result = await self.board_call(c, api, args)
        if result["ok"]: c.applied.append({"api": api, "args": args or {}})
        return result

    async def tool_record_configuration(self, c, settings):
        c.config = settings
        self.emit(c, "config", settings=settings, port=c.port)
        return {"ok": True}

    def view_spec(self, c):
        s = c.sensor or {}
        return dict(units=sensor_lib.units(s), views=s.get("views"), sim=self.board.transport_type == "sim",
                    live_hint=s.get("live_hint"), live_window=s.get("live_window", 10),
                    sim_action=s.get("sim_action"), ui_actions={k: a["label"] for k, a in s.get("ui_actions", {}).items()})

    def reading_sink(self, c, ids, live=False):
        """Throttled reading events for the UI. `ids` is a dict read on every send, so a live stream can move to a new card."""
        latest, last_sent = {}, [0.0]
        period = 1 / max(1.0, float((c.sensor or {}).get("ui_rate_hz", 10)))

        def on_reading(values):
            latest.update(values)
            if time.time() - last_sent[0] >= period:
                last_sent[0] = time.time()
                self.emit_threadsafe(c, "reading", values=dict(latest), **ids, **({"live": True} if live else {}))
        return on_reading

    async def tool_start_live_view(self, c, note=None):
        if not c.sensor or not c.port: return {"ok": False, "error": "no sensor on a port yet"}
        view_id = f"{c.id}-live-{int(time.time() * 1000) % 10**8}"
        live = self.live if self.live and self.live["c"] is c and not self.live["task"].done() else None
        if self.live and not live: await self.stop_live()
        self.emit(c, "live_view", view_id=view_id, title=c.sensor["model"], port=c.port, note=note, **self.view_spec(c))
        if live:  # already streaming: move it to the new card
            old, live["ids"]["view_id"], live["view_id"] = live["view_id"], view_id, view_id
            self.emit(c, "live_status", view_id=old, status="moved", last=live["last"].get("values"))
            return {"ok": True, "view_id": view_id, "note": "The live monitor was already streaming; it now shows in a new card."}
        stop, ids, last = threading.Event(), {"view_id": view_id}, {}
        ui = self.reading_sink(c, ids, live=True)

        def sink(values):
            last["values"] = values; ui(values)

        async def run():
            try:
                res = await asyncio.to_thread(self.board.stream, self.tgt(c), stop, self.io_logger(c), sink)
            except Exception as e:
                res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            self.emit(c, "live_status", view_id=ids["view_id"], status="stopped", samples=res.get("samples", 0),
                      rate_hz=res.get("rate_hz"), last=res.get("last"), error=res.get("error"))
            return res
        self.live = {"c": c, "view_id": view_id, "ids": ids, "last": last, "stop": stop, "task": asyncio.create_task(run())}
        await asyncio.sleep(0.3)
        return {"ok": True, "view_id": view_id, "note": "The live monitor is streaming. It keeps running through board calls and tests, "
                                                        "until the user presses Stop or the next sensor is added."}

    async def stop_live(self):
        live, self.live = self.live, None
        if not live: return None
        live["stop"].set()
        try: return await live["task"]
        except Exception: return None

    async def ui_action(self, action):
        c = self.live["c"] if self.live else (self.components[-1] if self.components else None)
        spec = ((c and c.sensor) or {}).get("ui_actions", {}).get(action)
        if not spec: return self.emit(None, "error", text=f"Unknown action {action!r}.")
        result = await self.board_call(c, spec["api"])
        self.emit(c, "ui_action_result", action=action, label=spec["label"], ok=result.get("ok"), error=result.get("error"))

    # ---- testing tools ----
    async def tool_run_test(self, c, name, instruction, seconds):
        seconds = max(1.0, min(15.0, float(seconds)))
        lead = self.config.get("lead_seconds", 3)
        test_id = f"{c.id}-{name}-{len(c.test_ids)}"; c.test_ids[name] = test_id
        self.emit(c, "test_step", test_id=test_id, name=name, instruction=instruction, lead=lead, seconds=seconds, **self.view_spec(c))
        await asyncio.sleep(lead)
        self.emit(c, "test_status", test_id=test_id, status="sampling")
        result = await asyncio.to_thread(self.board.sample, seconds, self.tgt(c), self.io_logger(c), self.reading_sink(c, {"test_id": test_id}))
        self.emit(c, "test_status", test_id=test_id, status="done", samples=result.get("samples", 0))
        test = next((t for t in (c.sensor or {}).get("test_plan", []) if t["name"] == name), None)
        if test and checks.rules_for(test) and result.get("ok"):
            result["checks"] = c.test_checks[name] = checks.evaluate(test, result)
            result["all_checks_pass"] = all(x["pass"] for x in result["checks"])
        return result

    async def tool_record_test_result(self, c, name, passed, summary):
        failed = [x["check"] for x in c.test_checks.get(name, []) if not x["pass"]]
        if passed and failed:
            return {"ok": False, "error": f"Not recorded: these checks failed in the latest run: {failed}. "
                                          "Record passed=false (quoting them), or rerun the test."}
        if passed and name not in c.test_checks and checks.rules_for(next((t for t in (c.sensor or {}).get("test_plan", []) if t["name"] == name), {})):
            return {"ok": False, "error": f"Not recorded: run_test has not produced a measurement for '{name}' yet."}
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
