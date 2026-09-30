"""Offline stand-in for the OpenAI Responses API, so the UI can be tested without an API key.

A rule-based script per phase drives the same tools and gates as the real LLM. Each phase is a
generator: it yields the output items of one "response" and receives the next input, which is
the list of tool results after a turn with function calls, or the user's text after a turn
without any (an empty turn just waits for the user).
"""
import asyncio, itertools, json, operator, re
from types import SimpleNamespace as NS

_ids = itertools.count(1)
DONE = re.compile(r"\b(done|completed?|connected|yes|ok(ay)?|ready|next|finished)\b", re.I)
NO_CHANGE = re.compile(r"\b(no|nope|nothing|looks good|good|fine|ok(ay)?|continue|done|next|proceed|all set)\b", re.I)
OPS = {"<": operator.lt, ">": operator.gt}


def msg(text): return NS(type="message", content=[NS(type="output_text", text=text)])
def call(_tool, **args): return NS(type="function_call", name=_tool, arguments=json.dumps(args), call_id=f"call_{next(_ids)}")


def finish(*items):
    while True:
        res = (yield [*items, call("complete_phase")])[-1]
        if res.get("ok"): return
        yield [msg("I can't finish this phase yet. Still missing: " + ", ".join(res.get("missing", [])) + ".")]
        items = ()


def parse_change(text):
    change, t = {}, text.lower()
    if m := re.search(r"\b(\d{4,7})\b", t): change["baud"] = int(m.group(1))
    if m := re.search(r"\b(even|odd|none|no)\s+parity|parity\s+(?:to\s+)?(even|odd|none|[neo])\b", t):
        change["parity"] = (m.group(1) or m.group(2))[0].upper()
    if m := re.search(r"\b([12])\s*stop", t): change["stop_bits"] = int(m.group(1))
    if m := re.search(r"\b(enable|disable|turn on|turn off)\s+ack|\back\w*\s+(?:to\s+)?(on|off|1|0|enabled?|disabled?)\b", t):
        change["ack"] = 0 if re.match(r"(disable|turn off|off|0)", m.group(1) or m.group(2)) else 1
    return change


class OfflineLLM:
    def __init__(self, session):
        self.session, self.gens = session, {}
        self.responses = self  # mimic client.responses.create(...)

    async def create(self, *, input, previous_response_id=None, **_):
        await asyncio.sleep(0.4)  # let the "thinking" indicator show
        c = self.session.components[-1]
        key = (c.id, c.phase)
        if previous_response_id is None:
            self.gens[key] = getattr(self, c.phase)(c)
            items = next(self.gens[key])
        else:
            outs = [json.loads(i["output"]) for i in input if i.get("type") == "function_call_output"]
            user = next((i["content"] for i in reversed(input) if i.get("role") == "user"), "")
            try: items = self.gens[key].send(outs if outs else user)
            except StopIteration: items = []
        return NS(id=f"offline_{next(_ids)}", output=items)

    # ---- phases ----
    def connectivity(self, c):
        while not c.name:
            name = yield [msg("What would you like to name this sensor?")]
            yield [call("set_component_name", name=name)]
        prompt = "Enter the model for the sensor."
        while True:
            model = yield [msg(prompt)]
            (res,) = yield [call("lookup_sensor", model=model)]
            if res.get("found"): break
            known = ", ".join(s["model"] for s in self.session_sensors())
            prompt = f'I couldn\'t find a sensor matching "{model}". I have data for: {known}. Enter the model for the sensor.'
        s = res["sensor"]
        yield [msg(f"The {s['model']} is a {s['type']} and uses the {s['protocol']} communication protocol. Here is the wiring."),
               call("show_schematic")]
        for w in res["wiring_plan"]:
            yield [call("request_wiring_step", sensor_pin=w["sensor_pin"])]
            reply = yield []
            while not DONE.search(reply):
                reply = yield [msg(f"No problem. Let me know once {w['sensor_pin']} is connected to port {w['cube_port']}.")]
            yield [call("confirm_wiring_step", sensor_pin=w["sensor_pin"])]
        yield from finish(msg("All connections are complete. The sensor is connected."))

    def configuration(self, c):
        settings = dict(c.sensor.get("recommended_settings", {}))
        head = "Let's start configuration. Configuring…"
        while True:
            calls, skipped = self.apply_calls(settings)
            results = (yield [msg(head), *calls]) if calls else []
            failed = [r for r in results if not r.get("ok")]
            if failed:
                why = "; ".join(r.get("error") or " ".join(r.get("replies", [])) or "no reply" for r in failed)
                reply = yield [msg(f"The board did not accept the configuration ({why}). Tell me a different value, or say retry.")]
                settings.update(parse_change(reply)); head = "Retrying…"
                continue
            yield [call("record_configuration", settings=settings)]
            note = f" I could not find a board API for: {', '.join(skipped)}." if skipped else ""
            supported = c.sensor.get("uart", {}).get("supported_baud")
            if supported and settings.get("baud") not in supported:
                note += f" Note: the {c.sensor['model']} datasheet only lists {supported} baud."
            reply = yield [msg("I have set the configuration shown above." + note + " Let me know if you want to change anything.")]
            while not (change := parse_change(reply)) and not NO_CHANGE.search(reply):
                reply = yield [msg("I can change the baud rate, parity, stop bits or ack, for example \"set baud to 115200\". "
                                   "Or say \"looks good\" to continue.")]
            if not change: break
            settings.update(change); head = "Applying the change…"
        yield from finish(msg("We will move onto testing now."))

    def testing(self, c):
        head = [msg("Let's test the sensor.")]
        for t in c.sensor.get("test_plan", []):
            while True:
                res = (yield [*head, call("run_test", name=t["name"], instruction=t["instruction"], seconds=t.get("seconds", 4))])[-1]
                head = []
                passed, reason = self.check(t, res)
                yield [call("record_test_result", name=t["name"], passed=passed, summary=reason)]
                if passed: break
                reply = yield [msg(f"The {t['name']} test failed. Say retry when you're ready to run it again, "
                                   "or tell me a setting to change (for example \"set baud to 9600\").")]
                if change := parse_change(reply):
                    c.config.update(change)
                    calls, _ = self.apply_calls(change)
                    yield [msg("Applying the change…"), *calls, call("record_configuration", settings=c.config)]
                    head = [msg("Running the test again.")]
        yield from finish(msg("Testing is complete now."))

    # ---- helpers ----
    def session_sensors(self):
        from agent import SENSORS
        return SENSORS

    def apply_calls(self, settings):
        apis, calls, skipped = self.session.config["apis"], [], []
        for key, value in settings.items():
            name = f"set_{key}" if f"set_{key}" in apis else next((n for n, a in apis.items() if list(a.get("params", {})) == [key]), None)
            params = list(apis.get(name, {}).get("params", {}))
            if len(params) != 1: skipped.append(key); continue
            calls.append(call("call_board_api", api=name, args={params[0]: value}))
        read_back = next((n for n in ("get_config", "read_config") if n in apis), None)
        if read_back: calls.append(call("call_board_api", api=read_back, args={}))
        return calls, skipped

    def check(self, test, res):
        if not res.get("ok"): return False, f"Could not read the sensor: {res.get('error', 'unknown error')}."
        if not res.get("samples"):
            return False, "No data arrived from the sensor. Check the TX wire and the UART settings (baud, parity, stop bits)."
        rule = test.get("offline_check")
        if not rule: return True, f"Received {res['samples']} samples."
        vals = {k: s[rule["stat"]] for k, s in res["fields"].items() if re.search(rule["fields"], k)}
        if not vals: return False, f"No fields matching {rule['fields']} in the data."
        ok = (all if rule.get("mode", "all") == "all" else any)(OPS[rule["op"]](v, rule["value"]) for v in vals.values())
        shown = ", ".join(f"{k}={v:.2f}" for k, v in vals.items())
        return ok, f"{rule['stat']} {shown} (expected {rule.get('mode', 'all')} {rule['op']} {rule['value']})."
