"""Offline stand-in for the OpenAI Responses API, so the UI can be tested without an API key.

A rule-based script per phase drives the same tools and gates as the real LLM. Each phase is a
generator: it yields the output items of one "response" and receives the next input, which is
the list of tool results after a turn with function calls, or the user's text after a turn
without any (an empty turn just waits for the user).
"""
import asyncio, itertools, json, re
import checks
from types import SimpleNamespace as NS

_ids = itertools.count(1)
DONE = re.compile(r"\b(done|completed?|connected|yes|ok(ay)?|ready|next|finished)\b", re.I)
YES = re.compile(r"\b(yes|yep|yeah|sure|ok(ay)?|apply|do it|go ahead|please)\b", re.I)
NO = re.compile(r"\b(no|nope|don'?t|do not|keep|leave)\b", re.I)
NO_CHANGE = re.compile(r"\b(no|nope|nothing|looks good|good|fine|ok(ay)?|continue|done|next|proceed|all set)\b", re.I)


def msg(text): return NS(type="message", content=[NS(type="output_text", text=text)])
def call(_tool, **args): return NS(type="function_call", name=_tool, arguments=json.dumps(args), call_id=f"call_{next(_ids)}")


def finish(*items):
    while True:
        res = (yield [*items, call("complete_phase")])[-1]
        if res.get("ok"): return
        yield [msg("I can't finish this phase yet. Still missing: " + ", ".join(res.get("missing", [])) + ".")]
        items = ()


NAMES = {"rate_hz": "output rate", "rate_ms": "stream period", "baud": "baud rate", "data_bits": "data bits", "stop_bits": "stop bits"}


def setting_name(key): return NAMES.get(key, key.replace("_", " "))


def show(key, value):
    """A setting value for people: 10 -> '10 Hz' for rate_hz, 1.0 -> '1 Hz', None -> 'unknown'."""
    if value is None: return "unknown"
    num = f"{round(value, 1):g}" if isinstance(value, (int, float)) else str(value)
    return num + (" Hz" if key.endswith("_hz") else " ms" if key.endswith("_ms") else "")


def parse_change(text):
    change, t = {}, text.lower()
    if m := re.search(r"\b(\d+(?:\.\d+)?)\s*hz\b", t):
        change["rate_hz"] = float(m.group(1)); t = t[:m.start()] + t[m.end():]
    if m := re.search(r"\brate\s+(?:to\s+)?(\d+)\s*(?:ms)?|\b(\d+)\s*ms\b", t):
        change["rate_ms"] = int(m.group(1) or m.group(2)); t = t[:m.start()] + t[m.end():]
    if m := re.search(r"\b([789])\s*data\s*bits?|data\s*bits?\s+(?:to\s+)?([789])\b", t):
        change["data_bits"] = int(m.group(1) or m.group(2)); t = t[:m.start()] + t[m.end():]
    if m := re.search(r"\b(\d{3,8})\b", t): change["baud"] = int(m.group(1))
    if m := re.search(r"\b(even|odd|none|no)\s+parity|parity\s+(?:to\s+)?(even|odd|none|[neo])\b", t):
        change["parity"] = (m.group(1) or m.group(2))[0]
    if m := re.search(r"\b(0\.5|1\.5|1|2)\s*stop", t): change["stop_bits"] = m.group(1)
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
            prompt = (f"{res['error']} Enter another sensor model." if res.get("error") else
                      f'I couldn\'t find a sensor matching "{model}". I have data for: {known}. Enter the model for the sensor.')
        s, port = res["sensor"], res["probe_cube_port"]
        yield [msg(f"The {s['model']} is a {s['type']} and uses the {s['protocol']} communication protocol. "
                   f"It goes on probe cube port {port['port']} ({port['pins']}). Here is the wiring."),
               call("show_schematic")]
        for w in res["wiring_plan"]:
            yield [call("request_wiring_step", sensor_pin=w["sensor_pin"])]
            reply = yield []
            while not DONE.search(reply):
                reply = yield [msg(f"No problem. Let me know once {w['sensor_pin']} is connected to port {w['cube_port']}.")]
            yield [call("confirm_wiring_step", sensor_pin=w["sensor_pin"])]
        free = self.session.free_ports()
        nxt = (" Next free ports: " + ", ".join(f"{p['port']} ({p['interface'].upper()}, {self.session.port_pins(p)})" for p in free) + "."
               if free else " All probe cube lab ports are now in use.")
        yield from finish(msg("All connections are complete. The sensor is connected." + nxt))

    PORT_KEYS = ("baud", "data_bits", "parity", "stop_bits")

    def configuration(self, c):
        rec = dict(c.sensor.get("recommended_settings", {}))
        settings = {k: v for k, v in rec.items() if k in self.PORT_KEYS}  # probe cube side: applied directly
        head = "Let's start configuration. Setting up the probe cube port…"
        while True:
            calls, skipped = self.apply_calls(c, settings)
            results = (yield [msg(head), *calls]) if calls else []
            failed = [r for r in results if not r.get("ok")]
            if not failed: break
            why = "; ".join(r.get("error") or " ".join(r.get("replies", [])) or "no reply" for r in failed)
            reply = yield [msg(f"The board did not accept the configuration ({why}). Tell me a different value, or say retry.")]
            settings.update(parse_change(reply)); head = "Retrying…"
        # sensor side: propose the recommended values and only apply them if the user agrees
        current = next(({"rate_hz": r["rate_hz"]} for r in results if "rate_hz" in r and "decoder" in r), {})
        wanted = {k: v for k, v in rec.items() if k not in self.PORT_KEYS
                  and not (k in current and abs(current[k] - v) <= 0.1 * v)}
        settings.update({k: v for k, v in rec.items() if k not in self.PORT_KEYS and k not in wanted})
        if wanted:
            notes = c.sensor.get("recommended_notes", {})
            lines = [f"- {setting_name(k)}: now {show(k, current.get(k))}, recommended {show(k, v)}" + (f". {notes[k][0].upper()}{notes[k][1:]}." if k in notes else "")
                     for k, v in wanted.items()]
            reply = yield [msg("The probe cube port is set and the link works. I'd like to change these sensor settings:\n" + "\n".join(lines) +
                               "\nShall I apply them? (yes / no)")]
            if YES.search(reply) and not NO.search(reply):
                calls, _ = self.apply_calls(c, wanted, verify=False)
                results = yield [msg("Applying…"), *calls]
                for (k, v), r in zip(wanted.items(), results):
                    if r.get("ok"): settings[k] = v
                    elif k in current: settings[k] = current[k]
            else:
                settings.update(current)
                yield [msg("OK, I'll keep the sensor's current settings.")]
        while True:
            yield [call("record_configuration", settings=settings), call("start_live_view")]
            note = f" I could not find a board API for: {', '.join(skipped)}." if skipped else ""
            supported = c.sensor.get("uart", {}).get("supported_baud")
            if supported and settings.get("baud") not in supported:
                note += f" Note: the {c.sensor['model']} datasheet only lists {supported} baud."
            reply = yield [msg("The configuration is shown above and the live view is open, so you can try the sensor now." + note +
                               " Let me know if you want to change anything.")]
            while not (change := parse_change(reply)) and not NO_CHANGE.search(reply):
                reply = yield [msg("I can change the baud rate, data bits, parity, stop bits or the output rate, for example \"set baud to 19200\" or \"rate 20 hz\". "
                                   "Or say \"looks good\" to continue.")]
            if not change: break
            calls, skipped = self.apply_calls(c, change)
            results = yield [msg("Applying the change…"), *calls]
            if failed := [r for r in results if not r.get("ok")]:
                why = "; ".join(r.get("error") or "no reply" for r in failed)
                yield [msg(f"That did not work ({why}). Tell me a different value.")]
            settings.update(change)
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
                    calls, _ = self.apply_calls(c, change)
                    yield [msg("Applying the change…"), *calls, call("record_configuration", settings=c.config)]
                    head = [msg("Running the test again.")]
        yield from finish(msg("Testing is complete now."))

    # ---- helpers ----
    def session_sensors(self):
        from agent import SENSORS
        return SENSORS

    def apply_calls(self, c, settings, verify=True):
        apis, calls, skipped = self.session.apis_for(c), [], []
        for key, value in settings.items():
            name = f"set_{key}" if f"set_{key}" in apis else next((n for n, a in apis.items() if list(a.get("params", {})) == [key]), None)
            params = list(apis.get(name, {}).get("params", {}))
            if len(params) != 1: skipped.append(key); continue
            calls.append(call("call_board_api", api=name, args={params[0]: value}))
        if verify: calls += [call("call_board_api", api=n, args={}) for n in c.sensor.get("verify_apis", ["ports"]) if n in apis or n == "stream_probe"]
        return calls, skipped

    def check(self, test, res):
        if not res.get("ok"):
            start = res.get("start", {})
            why = start.get("error") or res.get("error", "unknown error")
            return False, f"Could not read the sensor: {res.get('error', 'unknown error')} ({why}). Check the sensor's TX wire and the UART settings."
        if not res.get("samples"):
            return False, "No data arrived from the sensor. Check the TX wire and the UART settings (baud, parity, stop bits)."
        cks = res.get("checks") if "checks" in res else checks.evaluate(test, res)
        if not cks: return True, f"Received {res['samples']} samples."
        shown = lambda x: x["check"] + (" (" + ", ".join(f"{k}={v:.2f}" for k, v in x["values"].items()) + ")" if x["values"] else "")
        failed = [x for x in cks if not x["pass"]]
        if failed: return False, "Failed: " + "; ".join(map(shown, failed)) + "."
        return True, "All checks passed: " + "; ".join(map(shown, cks)) + "."
